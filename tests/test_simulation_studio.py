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
