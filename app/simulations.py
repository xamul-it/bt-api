"""Authenticated online simulation endpoints. No trading modes are exposed."""
import json
from io import BytesIO
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, current_app
from werkzeug.exceptions import HTTPException

import simulation_studio as studio
from app.scheduler import simulation_service as service
from app.service.calculation_job_service import CalculationConflict
from app.service.simulation_service import benchmarks, benchmark_series, save_benchmark, update_benchmark

sim_bp = Blueprint('simulations', __name__)


@sim_bp.errorhandler(ValueError)
def invalid(exc):
    return jsonify(error=str(exc)), 400


@sim_bp.errorhandler(CalculationConflict)
def conflict(exc):
    return jsonify(error=str(exc)), 409


@sim_bp.errorhandler(LookupError)
@sim_bp.errorhandler(FileNotFoundError)
def missing(exc):
    return jsonify(error=str(exc)), 404


@sim_bp.errorhandler(Exception)
def failed(exc):
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description), exc.code
    current_app.logger.exception('Simulation request failed')
    return jsonify(error='Operazione non riuscita. Consultare i log API o riprovare.'), 500


@sim_bp.get('/catalog')
def catalog():
    from profile_baseline import SCHEDULED_DIR
    return jsonify(strategies=studio.strategies(), ticker_lists=studio.ticker_lists(),
                   providers=list(studio.PROVIDERS), timeframes=list(studio.TIMEFRAMES),
                   commissions=['alpaca', 'none', 'fineco', 'lfineco'], benchmarks=benchmarks(),
                   profiles=[p.stem for p in sorted(SCHEDULED_DIR.glob('*.env'))])


@sim_bp.get('/ticker-lists')
def ticker_lists():
    return jsonify(service.ticker_lists(request.args.get('symbol', ''), request.args.get('archived') == '1'))


@sim_bp.post('/ticker-lists')
def ticker_list_save():
    if request.is_json:
        return jsonify(service.save_ticker_list(request.get_json() or {}))
    payload = json.loads(request.form.get('configuration', '{}'))
    file = request.files.get('file')
    content = file.read(1024 * 1024 + 1) if file else None
    if content is not None and len(content) > 1024 * 1024:
        raise ValueError('File troppo grande: massimo 1 MB')
    return jsonify(service.save_ticker_list(payload, content, file.filename if file else ''))


@sim_bp.post('/ticker-lists/refresh')
def ticker_lists_refresh():
    return jsonify(service.refresh_ticker_lists(ticker=(request.get_json() or {}).get('ticker'))), 202


@sim_bp.post('/ticker-lists/<ticker>/archive')
def ticker_list_archive(ticker):
    return jsonify(service.archive_ticker_list(ticker))


@sim_bp.post('/ticker-lists/<ticker>/restore')
def ticker_list_restore(ticker):
    return jsonify(service.restore_ticker_list(ticker))


@sim_bp.get('/strategy')
def strategy():
    return jsonify(studio.strategy_schema(request.args.get('name', '')))


@sim_bp.get('/preset/<profile>')
def preset(profile):
    from profile_baseline import load_profile_env, SCHEDULED_DIR
    from btmain import parse_strategy_args
    if not (SCHEDULED_DIR / studio.name(profile, suffix='.env')).is_file():
        raise LookupError('Profilo non trovato')
    env = load_profile_env(profile)
    params = parse_strategy_args(env['STRATARGS'])
    params = {k:v for k,v in params.items() if k not in studio.RESERVED_PARAMS}
    return jsonify(strategy=env['STRAT'], parameters=params, ticker=env['TICKER'],
        provider=env['DATA_PROVIDER'], alpaca_feed=env.get('ALPACA_FEED','sip'),
        commission=env.get('COMMISSION','alpaca'),
        broker=dict(margin_leverage=float(env.get('MARGIN_LEVERAGE') or 1)),
        timeframe='daily', mode='backtest', label=profile)


@sim_bp.post('/validate')
def validate():
    config = studio.configuration(request.get_json() or {})
    coverage = studio.data_inventory(config['ticker'], config['provider'], config['timeframe'], config['alpaca_feed'])
    return jsonify(configuration=config, coverage=coverage)


@sim_bp.get('/jobs')
def jobs():
    return jsonify(service.jobs())


@sim_bp.post('/jobs')
def submit():
    return jsonify(service.submit(request.get_json() or {})), 202


@sim_bp.get('/jobs/<job_id>')
def job(job_id):
    return jsonify(service.job(job_id))


@sim_bp.patch('/jobs/<job_id>')
def archive(job_id):
    return jsonify(service.archive_run(job_id, request.get_json() or {}))


@sim_bp.get('/jobs/<job_id>/log')
def log(job_id):
    service.job(job_id)
    return jsonify(output=service.calculations.read_log(job_id, limit=16000))


@sim_bp.delete('/jobs/<job_id>')
def delete_run(job_id):
    return jsonify(service.delete_run(job_id))


@sim_bp.get('/jobs/<job_id>/artifacts/<filename>')
def artifact(job_id, filename):
    return send_file(service.artifact(job_id, filename), conditional=True,
                     as_attachment=request.args.get('download') == '1')


@sim_bp.get('/jobs/<job_id>/trades')
def trades(job_id):
    path = service.artifact(job_id, 'trades.json')
    # An explicit download is available for the complete dataset.
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('Tabella molto grande: scaricare trades.json dagli artefatti')
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        data = list(data.values()) if isinstance(data, dict) else []
    symbol = request.args.get('symbol')
    if symbol:
        data = [row for row in data if (row.get('asset') or row.get('symbol')) == symbol]
    page = max(1, int(request.args.get('page', 1)))
    size = max(1, min(200, int(request.args.get('limit', 50))))
    return jsonify(total=len(data), page=page, rows=data[(page-1)*size:page*size])


@sim_bp.get('/jobs/<job_id>/diagnostics')
def diagnostics(job_id):
    from app.service.simulation_diagnostics import build_diagnostics
    history_days = max(1, min(1000, int(request.args.get('history_days', 25))))
    # Use the same authorization/path validation as the artifact endpoints.
    service.job(job_id)
    return jsonify(build_diagnostics(
        lambda filename: service.artifact(job_id, filename), history_days=history_days,
    ))


@sim_bp.get('/jobs/<job_id>/trades.xlsx')
def trades_excel(job_id):
    content = service.export_trades_excel(job_id)
    return send_file(
        BytesIO(content),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'trades-{job_id}.xlsx',
    )


@sim_bp.get('/jobs/<job_id>/equity')
def equity(job_id):
    import pandas as pd
    path = service.artifact(job_id, 'returns.csv')
    frame = pd.read_csv(path)
    curve = (1 + pd.to_numeric(frame.iloc[:, -1])).cumprod()
    stride = max(1, len(frame)//1000)
    indices = sorted(set(range(0,len(frame),stride)) | ({len(frame)-1} if len(frame) else set()))
    points = [dict(date=str(frame.iloc[i,0]), value=float(curve.iloc[i])) for i in indices]
    drawdown = curve / curve.cummax() - 1
    drawdown_points = [dict(date=str(frame.iloc[i,0]), value=float(drawdown.iloc[i])) for i in indices]

    benchmark_points = []
    try:
        benchmark_frame = pd.read_csv(service.artifact(job_id, 'benchmark.csv'))
        run_dates = pd.to_datetime(frame.iloc[:, 0], utc=True).dt.normalize()
        benchmark_dates = pd.to_datetime(benchmark_frame.iloc[:, 0], utc=True).dt.normalize()
        benchmark_frame = benchmark_frame.loc[
            benchmark_dates.between(run_dates.min(), run_dates.max())
        ].reset_index(drop=True)
        benchmark_returns = pd.to_numeric(benchmark_frame['return'], errors='raise')
        benchmark_curve = (1 + benchmark_returns).cumprod()
        benchmark_stride = max(1, len(benchmark_frame)//1000)
        benchmark_indices = sorted(
            set(range(0, len(benchmark_frame), benchmark_stride)) |
            ({len(benchmark_frame)-1} if len(benchmark_frame) else set())
        )
        benchmark_points = [
            dict(date=str(benchmark_frame.iloc[i, 0]), value=float(benchmark_curve.iloc[i]))
            for i in benchmark_indices
        ]
    except (FileNotFoundError, KeyError, ValueError):
        # Older runs may not have an immutable benchmark artifact.
        pass

    return jsonify(points=points, benchmark_points=benchmark_points, drawdown_points=drawdown_points)


@sim_bp.get('/benchmarks')
def benchmark_list():
    return jsonify(benchmarks())


@sim_bp.get('/benchmarks/<benchmark>')
def benchmark(benchmark):
    return jsonify(benchmark_series(benchmark))


@sim_bp.patch('/benchmarks/<benchmark>')
def benchmark_update(benchmark):
    return jsonify(update_benchmark(benchmark, request.get_json() or {}))


@sim_bp.get('/benchmarks/<benchmark>/download')
def benchmark_download(benchmark):
    path = studio.SHARED / 'benchmark' / studio.name(benchmark, suffix='.csv')
    if not path.is_file():
        raise FileNotFoundError('Benchmark non trovato')
    return send_file(path, as_attachment=True)


@sim_bp.post('/benchmarks')
def benchmark_save():
    if request.is_json:
        body = request.get_json()
        path = service.artifact(body.get('job_id',''), 'returns.csv')
        result = save_benchmark(body.get('label'), path.read_bytes(), source_job_id=body['job_id'])
    else:
        file = request.files.get('file')
        if not file:
            raise ValueError('Selezionare un CSV')
        result = save_benchmark(request.form.get('label'), file.read(10*1024*1024+1))
    return jsonify(result), 201


@sim_bp.get('/data')
def inventory():
    return jsonify(studio.data_inventory(request.args.get('ticker',''), request.args.get('provider','yahoo_adj'),
                                        request.args.get('timeframe','daily'), request.args.get('feed','sip')))


@sim_bp.get('/benchmark-definitions')
def benchmark_definitions():
    return jsonify(service.benchmark_definitions())


@sim_bp.post('/benchmark-definitions')
def benchmark_definition_save():
    return jsonify(service.save_benchmark_definition(request.get_json() or {})), 201


@sim_bp.post('/benchmark-definitions/<identity>/refresh')
def benchmark_definition_refresh(identity):
    return jsonify(service.refresh_benchmark(identity)), 202


@sim_bp.get('/data/<symbol>/bars')
def bars(symbol):
    return jsonify(studio.ticker_bars(symbol, request.args.get('provider','yahoo_adj'),
                request.args.get('timeframe','daily'), request.args.get('feed','sip'), int(request.args.get('limit',200))))


@sim_bp.post('/data/refresh')
def refresh():
    return jsonify(service.refresh_data(request.get_json() or {})), 202


@sim_bp.get('/schedules')
def schedules():
    return jsonify(service.schedules())


@sim_bp.post('/schedules')
def schedule_create():
    return jsonify(service.save_schedule(request.get_json() or {})), 201


@sim_bp.patch('/schedules/<schedule_id>')
def schedule_update(schedule_id):
    row = next((row for row in service.schedules() if row['id'] == schedule_id), None)
    if not row:
        raise LookupError('Pianificazione non trovata')
    patch = request.get_json() or {}
    if set(patch) - {'enabled', 'hour', 'minute', 'label'}:
        raise ValueError('Campo pianificazione non modificabile')
    return jsonify(service.save_schedule({**row, **patch}, schedule_id))


@sim_bp.delete('/schedules/<schedule_id>')
def schedule_delete(schedule_id):
    return jsonify(service.delete_schedule(schedule_id))
