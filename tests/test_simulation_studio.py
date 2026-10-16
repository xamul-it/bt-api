import pytest
from flask import Flask


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
    assert snapshot['configuration']['symbols'] == ['A','B']
    assert snapshot['benchmark_definition'] == chosen['id']
    assert calc.calls[0]['kind'] == 'market_data_refresh'
    scheduler.jobs = {}; service.restore_schedules()
    assert len(scheduler.jobs) == 1
    assert service.benchmark_definitions()[0]['job']['status'] == 'completed'
    service.save_benchmark_definition({**chosen,'enabled':False})
    assert not scheduler.jobs
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
