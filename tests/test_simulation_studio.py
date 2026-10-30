import pytest
from flask import Flask
from io import BytesIO
from zipfile import ZipFile


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    from app.service import simulation_service as srv
    import simulation_studio as studio
    monkeypatch.setattr(srv,'REGISTRY_DIR',tmp_path/'registry')
    monkeypatch.setattr(studio,'SHARED',tmp_path/'shared')
    return srv,studio


def test_benchmark_import_normalizes_return_series_and_preserves_origin(isolated):
    srv,studio=isolated
    saved=srv.save_benchmark('Test',b'date,return\n2026-01-01,0.01\n2026-01-02,-0.02\n',source_job_id='job')
    assert saved['source_job_id']=='job'
    curve=srv.benchmark_series(saved['name'])
    assert curve['observations']==2
    assert curve['points'][-1]['value']==pytest.approx(1.01*.98)
    assert srv.benchmarks()[0]['label']=='Test'


@pytest.mark.parametrize('csv',[
    b'date,return\n2026-01-02,0.01\n2026-01-01,0.02\n',
    b'date,return\n2026-01-01,0.01\n2026-01-01,0.02\n',
    b'date,return\nwrong,0.01\n', b'date,return\n2026-01-01,inf\n',
    b'date,return\n2026-01-01,-2\n',
])
def test_invalid_benchmark_is_not_saved(isolated,csv):
    srv,studio=isolated
    with pytest.raises(ValueError):srv.save_benchmark('Test',csv)
    assert not list((studio.SHARED/'benchmark').glob('*.csv'))


def test_daily_simulation_schedule_survives_reload_and_can_be_disabled(isolated,monkeypatch):
    srv,studio=isolated
    monkeypatch.setattr(studio,'configuration',lambda p:dict(p))
    class Scheduler:
        def __init__(self):self.jobs={}
        def get_job(self,id):return self.jobs.get(id)
        def remove_job(self,id):self.jobs.pop(id)
        def add_job(self,fn,trigger,**kwargs):self.jobs[kwargs['id']]=(fn,trigger,kwargs)
    scheduler=Scheduler();service=srv.SimulationService(None,scheduler,None)
    row=service.save_schedule(dict(label='Daily',hour=21,minute=15,
          configuration=dict(label='Test',fromdate='2026-01-01',todate='2026-02-01')))
    assert row['rolling_days']==31
    assert len(scheduler.jobs)==1
    scheduler.jobs={};service.restore_schedules()
    assert len(scheduler.jobs)==1
    service.save_schedule({**row,'enabled':False},row['id'])
    assert not scheduler.jobs


def test_scheduled_simulation_keeps_start_date_and_moves_only_end_to_today(isolated, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    srv, studio = isolated
    monkeypatch.setattr(studio, 'configuration', lambda payload: dict(payload))

    class Scheduler:
        def __init__(self): self.jobs = {}
        def get_job(self, identity): return self.jobs.get(identity)
        def remove_job(self, identity): self.jobs.pop(identity)
        def add_job(self, fn, trigger, **kwargs): self.jobs[kwargs['id']] = (fn, trigger, kwargs)

    service = srv.SimulationService(None, Scheduler(), None)
    row = service.save_schedule(dict(
        label='challenger', hour=21, minute=15,
        configuration=dict(label='challenger', fromdate='2000-01-01', todate='2026-01-01'),
    ))
    captured = {}
    monkeypatch.setattr(service, 'submit', lambda config, schedule_id=None: captured.update(
        configuration=config, schedule_id=schedule_id,
    ) or {'id': 'replacement-run'})

    service.run_schedule(row['id'])

    assert captured['configuration']['fromdate'] == '2000-01-01'
    assert captured['configuration']['todate'] == datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
    assert captured['configuration']['label'] == 'challenger'
    assert captured['schedule_id'] == row['id']
    refreshed = service.schedules()[0]
    assert refreshed['last_job_id'] == 'replacement-run'


def test_simulation_schedule_can_be_deleted(isolated, monkeypatch):
    srv, studio = isolated
    monkeypatch.setattr(studio, 'configuration', lambda payload: dict(payload))

    class Scheduler:
        def __init__(self): self.jobs = {}
        def get_job(self, identity): return self.jobs.get(identity)
        def remove_job(self, identity): self.jobs.pop(identity)
        def add_job(self, fn, trigger, **kwargs): self.jobs[kwargs['id']] = (fn, trigger, kwargs)

    scheduler = Scheduler()
    service = srv.SimulationService(None, scheduler, None)
    row = service.save_schedule(dict(
        label='challenger', hour=21, minute=15,
        configuration=dict(label='challenger', fromdate='2000-01-01', todate='2026-01-01'),
    ))
    assert service.delete_schedule(row['id'])['deleted'] is True
    assert service.schedules() == []
    assert scheduler.jobs == {}


def test_http_rejects_non_backtest_mode_before_queueing(monkeypatch):
    import app.simulations as views
    class Service:
        def submit(self,payload):
            import simulation_studio as studio
            return studio.configuration(payload)
    monkeypatch.setattr(views,'service',Service())
    app=Flask(__name__);app.register_blueprint(views.sim_bp,url_prefix='/dyn/simulations')
    response=app.test_client().post('/dyn/simulations/jobs',json={'mode':'live'})
    assert response.status_code==400
    assert 'backtest' in response.get_json()['error']


def test_trade_excel_endpoint_returns_downloadable_workbook(monkeypatch):
    import app.simulations as views
    class Service:
        def export_trades_excel(self, job_id):
            assert job_id == 'job-test'
            return b'PK-test-workbook'
    monkeypatch.setattr(views, 'service', Service())
    app = Flask(__name__); app.register_blueprint(views.sim_bp, url_prefix='/dyn/simulations')
    response = app.test_client().get('/dyn/simulations/jobs/job-test/trades.xlsx')
    assert response.status_code == 200
    assert response.data == b'PK-test-workbook'
    assert response.mimetype == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert 'trades-job-test.xlsx' in response.headers['Content-Disposition']


def test_equity_endpoint_returns_strategy_benchmark_and_drawdown(monkeypatch, tmp_path):
    import app.simulations as views
    returns = tmp_path / 'returns.csv'
    returns.write_text('date,return\n2026-01-01,0\n2026-01-02,0.1\n2026-01-03,-0.1\n')
    benchmark = tmp_path / 'benchmark.csv'
    benchmark.write_text('date,return\n2000-01-01,0.9\n2026-01-01,0\n2026-01-02,0.05\n2026-01-03,0\n2026-01-04,0.8\n')

    class Service:
        def artifact(self, job_id, filename):
            assert job_id == 'job-test'
            return {'returns.csv': returns, 'benchmark.csv': benchmark}[filename]

    monkeypatch.setattr(views, 'service', Service())
    app = Flask(__name__)
    app.register_blueprint(views.sim_bp, url_prefix='/dyn/simulations')
    payload = app.test_client().get('/dyn/simulations/jobs/job-test/equity').get_json()

    assert payload['points'][-1]['value'] == pytest.approx(0.99)
    assert payload['benchmark_points'][-1]['value'] == pytest.approx(1.05)
    assert payload['benchmark_points'][0]['date'] == '2026-01-01'
    assert payload['benchmark_points'][-1]['date'] == '2026-01-03'
    assert payload['drawdown_points'][-1]['value'] == pytest.approx(-0.1)


def test_benchmark_types_and_obsolescence_use_content_not_file_mtime(isolated):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    srv, studio = isolated
    today = datetime.now(ZoneInfo('Europe/Rome')).date()
    csv = b'date,return\n2026-01-01,0\n2026-01-02,0.02\n'
    saved = srv.save_benchmark('CSV', csv)
    run = srv.save_benchmark('Run', csv, source_job_id='job')
    # Internal immutable snapshots must never clutter the library.
    (studio.SHARED/'benchmark/__run_benchmark_hidden.csv').write_bytes(csv)
    rows = {r['name']:r for r in srv.benchmarks()}
    assert len(rows) == 2
    assert rows[saved['name']]['source_type'] == 'csv'
    assert rows[run['name']]['source_type'] == 'run'
    from datetime import date
    assert rows[saved['name']]['stale_days'] == (today-date(2026,1,2)).days
    assert rows[saved['name']]['last_date'] == '2026-01-02'


def test_only_selected_lists_are_scheduled_and_download_then_calculate(isolated):
    import json
    srv, studio = isolated
    lists = studio.SHARED/'tickers'; lists.mkdir(parents=True)
    (lists/'chosen.json').write_text(json.dumps(['A','B']))
    (lists/'other.json').write_text(json.dumps(['C']))
    class Scheduler:
        def __init__(self): self.jobs = {}
        def get_job(self, id): return self.jobs.get(id)
        def remove_job(self, id): self.jobs.pop(id)
        def add_job(self, fn, trigger, **kw): self.jobs[kw['id']] = (fn, trigger, kw)
    class Calculations:
        calls = []
        def create_job(self, **kw): self.calls.append(kw); return {'id':'job-test'}
    class Repo:
        def get_calculation_job(self, id): return {'id':id,'status':'completed','done':True}
    scheduler = Scheduler(); calc = Calculations(); service = srv.SimulationService(calc,scheduler,Repo())
    chosen = service.save_benchmark_definition({'ticker':'chosen.json','provider':'yahoo_adj'})
    assert len(scheduler.jobs) == 1
    assert len(service.benchmark_definitions()) == 1
    assert srv.benchmarks()[0]['available'] is False
    assert srv.benchmarks()[0]['stale_days'] is None
    service.run_benchmark_schedule(chosen['id'])
    snapshot = calc.calls[0]['snapshot']
    assert snapshot['ticker_batch']['lists'][0]['symbols'] == ['A','B']
    assert snapshot['ticker_batch']['lists'][0]['sources'][0]['definition_id'] == chosen['id']
    assert calc.calls[0]['kind'] == 'market_data_refresh'
    scheduler.jobs = {}; service.restore_schedules()
    assert len(scheduler.jobs) == 1
    assert service.benchmark_definitions()[0]['job']['status'] == 'completed'
    updated = service.save_benchmark_definition({**chosen, 'fromdate': '2010-03-04'})
    assert updated['id'] == chosen['id']
    assert service.benchmark_definitions()[0]['fromdate'] == '2010-03-04'
    service.refresh_benchmark(chosen['id'])
    assert calc.calls[-1]['snapshot']['ticker_batch']['lists'][0]['sources'][0]['fromdate'] == '2010-03-04'
    calc.calls.pop()
    service.save_benchmark_definition({**chosen,'enabled':False})
    assert len(scheduler.jobs) == 1  # disabling a benchmark preserves its download
    service.run_benchmark_schedule(chosen['id'])
    assert len(calc.calls) == 1
    with pytest.raises(ValueError): service.save_benchmark_definition({'ticker':'A,B'})
    with pytest.raises(ValueError): service.save_benchmark_definition({'ticker':'chosen.json','enabled':'yes'})


def test_pinned_old_run_remains_in_job_catalog_and_archive_is_reversible(isolated):
    srv, studio = isolated
    old = dict(id='old',kind='simulation',done=True,request_snapshot={},result={})
    class Repo:
        def list_calculation_jobs(self, **kw): return []
        def get_calculation_job(self, id): return old if id == 'old' else None
    service = srv.SimulationService(None,None,Repo())
    service.archive_run('old', {'pinned':True})
    assert service.jobs()[0]['id'] == 'old'
    service.archive_run('old', {'hidden':True})
    assert service.jobs()[0]['archive']['hidden']
    service.archive_run('old', {'hidden':False})
    assert not service.jobs()[0]['archive']['hidden']


def test_trade_excel_export_uses_display_order_and_readable_duration(isolated, tmp_path):
    import json
    srv, _ = isolated
    trades = tmp_path/'trades.json'
    trades.write_text(json.dumps([{
        'asset':'NVDA', 'open_datetime':'2026-10-14 00:00:00',
        'close_datetime':'2026-10-15 09:31:00', 'duration_bars':65,
        'pnl_pct':0.8289285, 'entry_side':'long', 'price':181.57470703125,
    }]))
    service = srv.SimulationService(None, None, None)
    service.job = lambda *_: {'request_snapshot': {'configuration': {'timeframe': 'minutes'}}}
    service.artifact = lambda *_: trades
    content = service.export_trades_excel('job')
    assert content.startswith(b'PK')
    with ZipFile(BytesIO(content)) as workbook:
        xml = '\n'.join(
            workbook.read(name).decode('utf-8')
            for name in workbook.namelist() if name.endswith('.xml')
        )
    assert xml.index('Ticker') < xml.index('Direzione') < xml.index('Data ingresso')
    assert '01:05 (65 barre)' in xml
    assert 'dd/mm/yyyy hh:mm' in xml


def test_benchmark_worker_downloads_before_publishing_and_preserves_old_on_failure(isolated):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    import subprocess
    import pandas as pd
    srv, studio = isolated
    worker = Path(__file__).resolve().parents[2]/'bin/watchtower_calculation_worker.py'
    node = next(n for n in ast.parse(worker.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='run_market_refresh')
    calls, phases = [], []
    folder = studio.SHARED/'data/d/yahoo_adj'; folder.mkdir(parents=True)
    def download(command, **kw):
        calls.append(command)
        pd.DataFrame({'Date':['2026-01-01','2026-01-02'], 'Close':[100,110]}).to_csv(folder/'A.csv',index=False)
        pd.DataFrame({'Date':['2026-01-01','2026-01-02'], 'Close':[100,100]}).to_csv(folder/'B.csv',index=False)
    namespace = {'subprocess':SimpleNamespace(run=download),'BT_CORE':studio.CORE}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(worker),'exec'),namespace)
    class Repo:
        def update_calculation_progress(self,*a,**kw): phases.append(kw['phase'])
    config = dict(ticker='changed-on-disk.json',symbols=['A','B'],provider='yahoo_adj',alpaca_feed='sip',timeframe='daily',fromdate='2026-01-01',todate='2026-01-02')
    snapshot = dict(configuration=config,benchmark_definition='buyhold_test')
    result = namespace['run_market_refresh'](Repo(),{'id':'job','worker_token':'token'},snapshot)
    assert calls[0][calls[0].index('--ticker')+1] == 'A,B'
    assert phases == ['download_market_data','benchmark_buyhold']
    assert result['benchmark']['observations'] == 2
    curve = srv.benchmark_series('buyhold_test')
    assert curve['points'][-1]['value'] == pytest.approx(1.05)
    before = (studio.SHARED/'benchmark/buyhold_test.csv').read_text()
    def fail(*a,**kw):raise subprocess.CalledProcessError(1,'download')
    namespace['subprocess'].run = fail
    with pytest.raises(subprocess.CalledProcessError):
        namespace['run_market_refresh'](Repo(),{'id':'job','worker_token':'token'},snapshot)
    assert (studio.SHARED/'benchmark/buyhold_test.csv').read_text() == before


def test_invalid_legacy_csv_is_visible_but_not_offered_as_valid_benchmark(isolated):
    srv, studio = isolated
    folder=studio.SHARED/'benchmark';folder.mkdir(parents=True)
    (folder/'broken.csv').write_text('index,return\n')
    row=srv.benchmarks()[0]
    assert not row['available'] and row['coverage_error']
    assert row['stale_days'] is None


def test_archive_hides_run_but_delete_removes_it_from_all_lists(isolated):
    srv, _ = isolated
    rows = [dict(id='run', kind='simulation', done=True, status='completed'),
            dict(id='busy', kind='simulation', done=False, status='running'),
            dict(id='data', kind='market_data_refresh', done=True, status='completed')]

    class Repo:
        def get_calculation_job(self, identity):
            return next((dict(row) for row in rows if row['id'] == identity), None)
        def list_calculation_jobs(self, **kwargs):
            return [dict(row) for row in rows]

    service = srv.SimulationService(None, None, Repo())
    service.archive_run('run', {'hidden': True})
    assert service.job('run')['archive']['hidden'] is True
    assert 'run' in {row['id'] for row in service.jobs()}
    assert service.delete_run('run')['deleted'] is True
    assert 'run' not in {row['id'] for row in service.jobs()}
    with pytest.raises(LookupError):
        service.job('run')
    with pytest.raises(ValueError):
        service.delete_run('busy')
    with pytest.raises(ValueError):
        service.delete_run('data')
    assert srv.metadata()['runs']['run']['deleted_at']


def test_delete_run_endpoint(monkeypatch):
    import app.simulations as views
    class Service:
        def delete_run(self, identity):
            assert identity == 'test-run'
            return dict(id=identity, deleted=True)
    monkeypatch.setattr(views, 'service', Service())
    app = Flask(__name__)
    app.register_blueprint(views.sim_bp, url_prefix='/dyn/simulations')
    response = app.test_client().delete('/dyn/simulations/jobs/test-run')
    assert response.status_code == 200
    assert response.get_json()['deleted'] is True


def test_run_metrics_backfill_from_log_preserves_original_summary(isolated, tmp_path):
    srv, _ = isolated
    log = tmp_path / 'run.log'
    log.write_text('INFO TRADE_RETURN_STATS {"mean_pct": 1.234567, "n_trades": 2}\n'
                   'INFO Sharpe:-0.456;\n')
    row = dict(kind='simulation', done=True, log_path=str(log),
               result={'performance': {'total_return_pct': 12}})
    result = srv.enrich_performance(row)
    assert result['performance'] == dict(total_return_pct=12, sharpe=None, sortino=None, volatility_pct=None,
                                         average_trade_return_pct=1.234567)
    assert row['result']['performance'] == {'total_return_pct': 12}
    log.write_text('INFO Sharpe:0.000;\nINFO TRADE_RETURN_STATS {"mean_pct": 0}\n')
    assert srv.enrich_performance(row)['performance']['sharpe'] is None
    assert srv.enrich_performance(row)['performance']['average_trade_return_pct'] == 0


def test_run_metrics_missing_output_stays_unknown(isolated):
    srv, _ = isolated
    result = srv.enrich_performance(dict(kind='simulation', done=True))
    assert result['performance'] == {'sharpe': None, 'sortino': None, 'volatility_pct': None, 'average_trade_return_pct': None}


def test_historical_run_metrics_fallback_to_saved_artifacts(isolated, monkeypatch, tmp_path):
    srv, studio = isolated
    monkeypatch.setattr(studio, 'CORE', tmp_path)
    output = tmp_path / 'out' / 'weekly' / 'RMAStrategy' / 'simulation_test'
    output.mkdir(parents=True)
    (output / 'results.json').write_text('{"0":{"Sharpe":1.789}}')
    (output / 'trades.json').write_text('[{"pnl_pct":3},{"pnl_pct":-1},{"pnl_pct":null}]')
    row = dict(kind='simulation', done=True, result=dict(strategy='weekly.RMAStrategy', run_id='simulation_test'))
    assert srv.enrich_performance(row)['performance'] == dict(sharpe=None, sortino=None, volatility_pct=None, average_trade_return_pct=1)


@pytest.mark.parametrize('existing', [None, 1.42])
def test_historical_sharpe_is_replaced_with_daily_full_history(isolated, monkeypatch, tmp_path, existing):
    import pandas as pd
    srv, studio = isolated
    monkeypatch.setattr(studio, 'CORE', tmp_path)
    output = tmp_path / 'out' / 'weekly' / 'RMAStrategy' / 'simulation_daily'
    output.mkdir(parents=True)
    returns = pd.Series([0, .01, -.02, .03, .005])
    path = output / 'returns.csv'
    returns.to_csv(path, index=True)
    row = dict(kind='simulation', done=True, result=dict(strategy='weekly.RMAStrategy',
        run_id='simulation_daily', performance={'sharpe': existing}, results={'Sharpe': 1.42}))
    result = srv.enrich_performance(row)
    expected = returns.mean() / returns.std(ddof=1) * 252 ** .5
    assert result['performance']['sharpe'] == pytest.approx(expected)
    assert result['performance']['sortino'] == pytest.approx(returns.mean() / (returns.clip(upper=0).pow(2).mean() ** .5) * 252 ** .5)
    assert result['performance']['volatility_pct'] == pytest.approx(returns.std(ddof=1) * 252 ** .5 * 100)
    assert result['results']['Sharpe'] == round(expected, 3)
    assert row['result']['results']['Sharpe'] == 1.42
    returns[:] = 0
    returns.to_csv(path, index=True)
    assert srv.enrich_performance(row)['performance']['sharpe'] is None


def test_historical_risk_metrics_use_stats_strategy_column(isolated, monkeypatch, tmp_path):
    srv, studio = isolated
    monkeypatch.setattr(studio, 'CORE', tmp_path)
    output = tmp_path / 'out' / 'weekly' / 'RMAStrategy' / 'simulation_stats'
    output.mkdir(parents=True)
    (output / 'stats.html').write_text(
        '<table><tr><th>Metric</th><th>Benchmark</th><th>Strategy</th></tr>'
        '<tr><td>Sortino</td><td>1.78</td><td><b>0.9</b></td></tr>'
        '<tr><td>Smart Sortino</td><td>1.69</td><td>0.85</td></tr>'
        '<tr><td>Volatility (ann.)</td><td>21.85%</td><td>40.7%</td></tr></table>')
    row = dict(kind='simulation', done=True, result=dict(
        strategy='weekly.RMAStrategy', run_id='simulation_stats'))
    metrics = srv.enrich_performance(row)['performance']
    assert metrics['sortino'] == .9
    assert metrics['volatility_pct'] == 40.7


def test_stats_risk_metrics_unknown_values(isolated, tmp_path):
    srv, _ = isolated
    path = tmp_path / 'stats.html'
    path.write_text('<table><tr><th>Metric</th><th>Strategy</th></tr>'
                    '<tr><td>Sortino</td><td>nan</td></tr>'
                    '<tr><td>Volatility (ann.)</td><td>0%</td></tr></table>')
    stat = path.stat()
    assert srv._stats_risk_metrics(str(path), stat.st_mtime_ns, stat.st_size) == {'volatility_pct': 0}


def test_trade_endpoint_filters_symbol_before_pagination(tmp_path, monkeypatch):
    import json
    import app.simulations as views
    path = tmp_path / 'trades.json'
    path.write_text(json.dumps([{'asset': 'MSFT', 'price': 10}, {'asset': 'AAPL', 'price': 20}, {'symbol': 'AAPL', 'price': 30}]))
    class Service:
        def artifact(self, *_): return path
    monkeypatch.setattr(views, 'service', Service())
    app = Flask(__name__)
    app.register_blueprint(views.sim_bp, url_prefix='/dyn/simulations')
    client = app.test_client()
    data = client.get('/dyn/simulations/jobs/run/trades?symbol=AAPL&limit=1&page=2').json
    assert data['total'] == 2
    assert data['rows'] == [{'symbol': 'AAPL', 'price': 30}]
    assert client.get('/dyn/simulations/jobs/run/trades').json['total'] == 3
