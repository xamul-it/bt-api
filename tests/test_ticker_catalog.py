"""Catalog integration: persistent flags, shared source plans and partial downloads."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    import simulation_studio as studio
    from app.service import simulation_service as srv
    monkeypatch.setattr(studio, 'SHARED', tmp_path / 'shared')
    monkeypatch.setattr(srv, 'REGISTRY_DIR', tmp_path / 'registry')

    class Scheduler:
        def __init__(self): self.jobs = {}
        def get_job(self, identity): return self.jobs.get(identity)
        def remove_job(self, identity): self.jobs.pop(identity)
        def add_job(self, fn, trigger, **kw): self.jobs[kw['id']] = (fn, trigger, kw)

    class Calculations:
        def __init__(self): self.calls = []
        def create_job(self, **kw):
            self.calls.append(kw)
            return {'id': 'test-job'}

    class Repo:
        def get_calculation_job(self, identity): return None

    service = srv.SimulationService(Calculations(), Scheduler(), Repo())
    return service, srv, studio


def save(service, ticker, symbols, **overrides):
    payload = dict(ticker=ticker, label=ticker, providers=['yahoo_adj'],
                   download_enabled=True, benchmark_enabled=False,
                   fromdate='2026-01-01', hour=1, minute=15)
    payload.update(overrides)
    return service.save_ticker_list(payload, json.dumps(symbols).encode(), 'list.json')


def test_import_membership_flags_restart_and_reversible_archive(catalog):
    service, srv, studio = catalog
    payload = dict(ticker='tech', label='Technology', providers=['yahoo_adj', 'alpaca'],
                   download_enabled=True, benchmark_enabled=True)
    row = service.save_ticker_list(payload, b'symbol,description\naapl,Apple\nMSFT,Microsoft\naapl,dup\n', 'file.csv')
    assert row['ticker'] == 'tech.json' and row['count'] == 2
    assert row['symbols'] == ['AAPL', 'MSFT']
    assert len(service.scheduler.jobs) == 1
    assert len(service.benchmark_definitions()) == 2
    assert len(service.ticker_lists('aapl')) == 1
    assert service.ticker_lists('AAP') == []
    service.scheduler.jobs = {}
    service.restore_schedules()
    assert len(service.scheduler.jobs) == 1
    service.save_ticker_list(dict(payload, download_enabled=True, benchmark_enabled=False))
    row = service.ticker_lists()[0]
    assert row['download_enabled'] and not row['benchmark_enabled']
    service.save_ticker_list(dict(payload, download_enabled=False, benchmark_enabled=False))
    assert not service.scheduler.jobs
    with pytest.raises(ValueError, match='richiede'):
        service.save_ticker_list(dict(payload, download_enabled=False))
    contents = (studio.SHARED / 'tickers/tech.json').read_bytes()
    service.archive_ticker_list('tech.json')
    assert service.ticker_lists() == [] and studio.ticker_lists() == []
    assert service.ticker_lists(archived=True)[0]['ticker'] == 'tech.json'
    service.restore_ticker_list('tech.json')
    assert (studio.SHARED / 'tickers/tech.json').read_bytes() == contents
    assert not service.ticker_lists()[0]['download_enabled']
    assert {s['provider'] for s in service.ticker_lists()[0]['sources']} == {'yahoo_adj', 'alpaca'}


@pytest.mark.parametrize('filename,content', [
    ('bad.json', b'{"symbols":["A"]}'), ('bad.json', b'["A,B"]'),
    ('bad.txt', b'../secret\n'), ('bad.csv', b'name,value\nA,1\n'),
    ('bad.txt', b'\xff'), ('empty.txt', b''), ('bad.csv', b'name,symbol\nname,\n'),
])
def test_invalid_imports_do_not_create_files(catalog, filename, content):
    service, _, studio = catalog
    with pytest.raises(ValueError):
        service.save_ticker_list(dict(ticker='bad', label='Bad', providers=['yahoo']), content, filename)
    assert not (studio.SHARED / 'tickers/bad.json').exists()


def test_source_union_and_frozen_membership(catalog):
    service, srv, _ = catalog
    from ticker_catalog import build_plan
    save(service, 'one', ['A', 'B'], providers=['yahoo', 'yahoo_adj'])
    save(service, 'two', ['B', 'C'], providers=['yahoo_adj', 'alpaca'], alpaca_feed='iex')
    plan = build_plan(srv.metadata(), hour=1, minute=15)
    assert len(plan['groups']) == 2
    assert {g['provider']: g['alpaca_feed'] for g in plan['groups']} == {'yahoo': 'sip', 'alpaca': 'iex'}
    assert all(g['symbols'] == ['A', 'B', 'C'] if g['provider'] == 'yahoo'
               else g['symbols'] == ['B', 'C'] for g in plan['groups'])
    assert len(service.scheduler.jobs) == 1
    job = service.refresh_ticker_lists()
    assert job['id'] == 'test-job'
    snapshot = service.calculations.calls[0]['snapshot']['ticker_batch']
    save(service, 'one', ['D'], providers=['yahoo'])
    assert snapshot['lists'][0]['symbols'] == ['A', 'B']


def test_busy_scheduled_slot_retries_instead_of_losing_its_daily_run(catalog, monkeypatch):
    service, _, _ = catalog
    from app.service.calculation_job_service import CalculationConflict
    save(service, 'one', ['A'])
    def busy(**kwargs): raise CalculationConflict('active_calculation_exists:ticker-catalog-daily')
    monkeypatch.setattr(service.calculations, 'create_job', busy)
    service.run_ticker_schedule(1, 15)
    assert 'calculation:ticker-retry:01:15' in service.scheduler.jobs


def test_batch_download_failure_isolated_and_previous_benchmark_kept(catalog):
    service, srv, studio = catalog
    from ticker_catalog import execute_plan
    save(service, 'bad', ['A', 'B'], benchmark_enabled=True)
    save(service, 'good', ['A'], benchmark_enabled=True)
    service.refresh_ticker_lists()
    plan = service.calculations.calls[0]['snapshot']['ticker_batch']
    identities = {d['ticker']: d['id'] for d in service.benchmark_definitions()}
    folder = studio.SHARED / 'benchmark'
    folder.mkdir()
    previous = folder / (identities['bad.json'] + '.csv')
    previous.write_text('date,return\n2026-01-01,0\n2026-01-02,0.123\n')
    before = previous.read_bytes()
    calls = []

    def download(command, **kwargs):
        calls.append(command)
        raw = studio.SHARED / 'data/d/yahoo_adj'
        raw.mkdir(parents=True)
        for symbol in ('A', 'B'):
            # B has an old, valid file: mere existence must not certify today's download.
            (raw / f'{symbol}.csv').write_text('Date,Close\n2026-01-01,100\n2026-01-02,110\n')
        receipt = Path(command[command.index('--receipt') + 1])
        receipt.write_text(json.dumps(dict(outcomes={'A': True, 'B': False},
            adjusted_outcomes={'A': True, 'B': False})))

    phases = []
    result = execute_plan(plan, 'test-job', phases.append, run=download)
    assert len(calls) == 1
    assert calls[0][calls[0].index('--ticker') + 1] == 'A,B'
    assert phases == ['download_market_data', 'benchmark_buyhold']
    assert result['failed'] == 1
    assert previous.read_bytes() == before
    assert (folder / (identities['good.json'] + '.csv')).is_file()
    rows = {r['ticker']: r for r in service.ticker_lists()}
    assert rows['bad.json']['last_batch']['status'] == 'partial'
    assert rows['bad.json']['last_success_at'] is None
    assert rows['good.json']['last_success_at']


def test_legacy_benchmarks_join_batch_and_can_keep_download_only(catalog):
    service, srv, studio = catalog
    path = studio.SHARED / 'tickers'
    path.mkdir(parents=True)
    (path / 'old.json').write_text('["A"]')
    row = service.save_benchmark_definition(dict(ticker='old.json', provider='yahoo_adj'))
    assert service.ticker_lists()[0]['benchmark_enabled']
    service.save_benchmark_definition(dict(row, enabled=False))
    result = service.ticker_lists()[0]
    assert result['download_enabled'] and not result['benchmark_enabled']
    assert len(service.scheduler.jobs) == 1


def test_http_catalog_upload_and_invalid_path(catalog, monkeypatch):
    from flask import Flask
    from io import BytesIO
    import app.simulations as views
    service, _, _ = catalog
    monkeypatch.setattr(views, 'service', service)
    app = Flask(__name__)
    app.register_blueprint(views.sim_bp, url_prefix='/dyn/simulations')
    client = app.test_client()
    response = client.post('/dyn/simulations/ticker-lists', data={
        'configuration': json.dumps(dict(ticker='test', label='Test', providers=['yahoo'])),
        'file': (BytesIO(b'A\nB\nA\n'), 'list.txt')})
    assert response.status_code == 200
    assert response.json['count'] == 2
    assert client.get('/dyn/simulations/ticker-lists?symbol=A').json[0]['ticker'] == 'test.json'
    assert client.post('/dyn/simulations/ticker-lists', json={
        'ticker': '../bad', 'label': 'Bad', 'providers': ['yahoo']}).status_code == 400
