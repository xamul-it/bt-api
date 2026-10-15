"""Backtesting studio: durable jobs, benchmark library and daily simulations."""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

import simulation_studio as studio
from app.paths import CONFIG_PATH

REGISTRY_DIR = Path(os.environ.get('BT_SIMULATION_REGISTRY', Path(CONFIG_PATH) / 'backtesting'))
ARTIFACTS = {'stats.html', 'results.json', 'returns.csv', 'trades.json', 'orders.json',
             'orderhistory.json', 'positions.json', 'positions.csv', 'today.json'}


@contextmanager
def registry():
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    with (REGISTRY_DIR / 'registry.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = REGISTRY_DIR / 'registry.json'
        state = json.loads(path.read_text()) if path.is_file() else {}
        try:
            yield state
        except Exception:
            raise
        else:
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, indent=2))
            temp.replace(path)


def metadata():
    path = REGISTRY_DIR / 'registry.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def benchmarks():
    saved = metadata().get('benchmarks', {})
    rows = []
    for path in sorted((studio.SHARED / 'benchmark').glob('*.csv')):
        rows.append(dict(name=path.stem, label=saved.get(path.stem, {}).get('label', path.stem),
                         size_bytes=path.stat().st_size,
                         updated_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                         **{k:v for k,v in saved.get(path.stem, {}).items() if k != 'label'}))
    return rows


def update_benchmark(benchmark, payload):
    identity = studio.name(benchmark)
    if not (studio.SHARED / 'benchmark' / f'{identity}.csv').is_file():
        raise FileNotFoundError('Benchmark non trovato')
    if not isinstance(payload, dict) or set(payload) - {'label', 'notes', 'hidden'}:
        raise ValueError('Campo benchmark non supportato')
    changes = {}
    for key in ('label', 'notes'):
        if key in payload:
            value = str(payload[key]).strip()
            if key == 'label' and not value:
                raise ValueError('Nome benchmark richiesto')
            changes[key] = value[:160 if key == 'label' else 2000]
    if 'hidden' in payload:
        if not isinstance(payload['hidden'], bool):
            raise ValueError('hidden deve essere booleano')
        changes['hidden'] = payload['hidden']
    with registry() as state:
        state.setdefault('benchmarks', {}).setdefault(identity, {}).update(changes)
    return next(row for row in benchmarks() if row['name'] == identity)


def benchmark_series(benchmark, limit=2000):
    import pandas as pd
    path = studio.SHARED / 'benchmark' / studio.name(benchmark, suffix='.csv')
    if not path.is_file():
        raise FileNotFoundError('Benchmark non trovato')
    frame = pd.read_csv(path)
    if 'return' not in frame.columns or len(frame.columns) < 2:
        raise ValueError('Benchmark senza colonna return')
    dates = pd.to_datetime(frame.iloc[:, 0], utc=True, errors='coerce')
    values = pd.to_numeric(frame['return'], errors='coerce')
    if dates.isna().any() or values.isna().any():
        raise ValueError('Benchmark contiene date o rendimenti non validi')
    curve = (1 + values).cumprod()
    stride = max(1, len(frame) // max(1, min(2000, limit)))
    indexes = sorted(set(range(0, len(frame), stride)) | ({len(frame)-1} if len(frame) else set()))
    return dict(name=path.stem, observations=len(frame),
                points=[dict(date=dates.iloc[i].isoformat(), value=float(curve.iloc[i]),
                             daily_return=float(values.iloc[i])) for i in indexes])


def save_benchmark(label, csv_bytes, *, source_job_id=None):
    import io
    import numpy as np
    import pandas as pd
    label = str(label or '').strip()
    if not label or len(label) > 160:
        raise ValueError('Nome benchmark richiesto (massimo 160 caratteri)')
    if not csv_bytes or len(csv_bytes) > 10 * 1024 * 1024:
        raise ValueError('CSV richiesto, massimo 10 MB')
    frame = pd.read_csv(io.BytesIO(csv_bytes))
    if len(frame.columns) != 2 or frame.empty:
        raise ValueError('CSV con due colonne richiesto: date e return (rendimento frazionario, es. 0.01 = 1%)')
    dates = pd.to_datetime(frame.iloc[:, 0], utc=True, errors='coerce')
    values = pd.to_numeric(frame.iloc[:, 1], errors='coerce')
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError('Date benchmark non valide, duplicate o non ordinate')
    if values.isna().any() or not np.isfinite(values).all() or (values < -1).any():
        raise ValueError('Rendimenti benchmark non validi: usare frazioni finite, minimo -1')
    identity = 'saved_' + uuid.uuid4().hex[:16]
    path = studio.SHARED / 'benchmark' / f'{identity}.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as handle:
        pd.DataFrame({'date': dates.dt.strftime('%Y-%m-%d'), 'return': values}).to_csv(handle, index=False)
    row = dict(label=label, source_job_id=source_job_id, observations=len(frame),
               created_at=datetime.now(timezone.utc).isoformat())
    with registry() as state:
        state.setdefault('benchmarks', {})[identity] = row
    return dict(name=identity, **row)


class SimulationService:
    def __init__(self, calculations, scheduler, repo):
        self.calculations, self.scheduler, self.repo = calculations, scheduler, repo

    def submit(self, payload, *, schedule_id=None):
        config = studio.configuration(payload)
        coverage = studio.data_inventory(config['ticker'], config['provider'], config['timeframe'], config['alpaca_feed'])
        if not any(row['available'] and not row.get('error') for row in coverage):
            raise ValueError('Nessun dato locale per la selezione. Aggiornare lo storico prima di simulare.')
        snapshot = dict(kind='simulation', profile='simulation-' + (schedule_id or uuid.uuid4().hex),
                        configuration=config, schedule_id=schedule_id,
                        data_coverage=coverage, requested_at=datetime.now(timezone.utc).isoformat())
        return self.calculations.create_job(kind='simulation', profile=snapshot['profile'], snapshot=snapshot)

    def jobs(self):
        saved = metadata().get('runs', {})
        rows = self.repo.list_calculation_jobs(limit=200, kinds=('simulation', 'market_data_refresh'))
        # Polling lists carry small summaries. Full engine receipts, defaults
        # and coverage are fetched only when the operator opens a specific run.
        result = []
        for row in rows:
            snapshot = row.get('request_snapshot') or {}
            receipt = row.get('result') or {}
            result.append(dict(row, archive=saved.get(row['id'], {}),
                request_snapshot={k:v for k,v in snapshot.items() if k != 'data_coverage'},
                result={k:v for k,v in receipt.items() if k in ('run_id','strategy','performance','artifacts')}))
        return result

    def job(self, job_id):
        row = self.repo.get_calculation_job(job_id)
        if not row or row['kind'] not in ('simulation', 'market_data_refresh'):
            raise LookupError('Simulazione non trovata')
        return dict(row, archive=metadata().get('runs', {}).get(job_id, {}))

    def artifact(self, job_id, filename):
        if filename not in ARTIFACTS:
            raise ValueError('Artefatto non ammesso')
        row = self.job(job_id)
        result = row.get('result') or {}
        strategy = str(result.get('strategy') or '')
        run_id = str(result.get('run_id') or '')
        if not strategy or not run_id:
            raise FileNotFoundError('Risultato non ancora disponibile')
        from watchtower_runtime import build_outpath
        root = (studio.CORE / 'out').resolve()
        path = (Path(build_outpath(str(root), strategy, run_id)) / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise FileNotFoundError('Artefatto non disponibile per questo run')
        return path

    def archive_run(self, job_id, payload):
        row = self.job(job_id)
        if not row.get('done'):
            raise ValueError('Attendere il completamento prima di archiviare')
        allowed = {'label', 'notes', 'pinned', 'hidden'}
        if set(payload) - allowed:
            raise ValueError('Campo archivio non supportato')
        updates = {}
        for key in ('label', 'notes'):
            if key in payload:
                updates[key] = str(payload[key])[:(160 if key == 'label' else 2000)]
        for key in ('pinned', 'hidden'):
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise ValueError(f'{key}: valore booleano richiesto')
                updates[key] = payload[key]
        with registry() as state:
            saved = state.setdefault('runs', {}).setdefault(job_id, {})
            saved.update(updates)
        return self.job(job_id)

    def refresh_data(self, payload):
        # Use the modern core loader. Never clear/truncate existing files.
        config = dict(ticker=str(payload.get('ticker') or ''), provider=str(payload.get('provider') or 'yahoo_adj'),
                      timeframe=str(payload.get('timeframe') or 'daily'), alpaca_feed=str(payload.get('alpaca_feed') or 'sip'),
                      fromdate=str(payload.get('fromdate') or '2000-01-01'), todate=str(date.today()))
        studio.resolve_symbols(config['ticker'])
        studio.data_folder(config['provider'], config['timeframe'], config['alpaca_feed'])
        if config['timeframe'] not in ('daily', 'minutes'):
            raise ValueError('Scaricare dati daily o minutes; weekly/monthly vengono ricampionati dal motore')
        if date.fromisoformat(config['fromdate']) > date.today():
            raise ValueError('Data inizio futura')
        return self.calculations.create_job(kind='market_data_refresh', profile='historical-data-refresh',
            snapshot=dict(kind='market_data_refresh', profile='historical-data-refresh', configuration=config))

    def schedules(self):
        return list(metadata().get('schedules', {}).values())

    def install_schedule(self, row):
        identity = 'calculation:simulation-schedule:' + row['id']
        if self.scheduler.get_job(identity):
            self.scheduler.remove_job(identity)
        if row['enabled']:
            self.scheduler.add_job(self.run_schedule,
                CronTrigger(hour=row['hour'], minute=row['minute'], timezone='Europe/Rome'),
                args=[row['id']], id=identity, replace_existing=True, max_instances=1, coalesce=True,
                misfire_grace_time=3600, executor='default')

    def save_schedule(self, payload, schedule_id=None):
        config = studio.configuration(payload.get('configuration') or {})
        hour = studio.numeric(payload.get('hour', 20), 'Ora', 0, 23)
        minute = studio.numeric(payload.get('minute', 0), 'Minuti', 0, 59)
        if not hour.is_integer() or not minute.is_integer():
            raise ValueError('Ora e minuti devono essere interi')
        enabled = payload.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('enabled deve essere booleano')
        row = dict(id=schedule_id or uuid.uuid4().hex, label=str(payload.get('label') or config['label'])[:160],
                   configuration=config, hour=int(hour), minute=int(minute), enabled=enabled,
                   rolling_days=max(1,(date.fromisoformat(config['todate'])-date.fromisoformat(config['fromdate'])).days),
                   timezone='Europe/Rome')
        with registry() as state:
            state.setdefault('schedules', {})[row['id']] = row
        self.install_schedule(row)
        return row

    def run_schedule(self, schedule_id):
        row = metadata().get('schedules', {}).get(schedule_id)
        if not row or not row['enabled']:
            return
        config = dict(row['configuration'])
        config.update(todate=str(date.today()), fromdate=str(date.today()-timedelta(days=row['rolling_days'])))
        try:
            job = self.submit(config, schedule_id=schedule_id)
            output = dict(last_job_id=job['id'], last_error=None)
        except Exception as exc:
            output = dict(last_error=str(exc))
        with registry() as state:
            target = state.get('schedules', {}).get(schedule_id)
            if target:
                target.update(output, last_triggered_at=datetime.now(timezone.utc).isoformat())

    def restore_schedules(self):
        for row in self.schedules():
            self.install_schedule(row)
