from unittest.mock import MagicMock
from flask import Flask


def test_followers_only_start_after_success_and_are_persisted(monkeypatch, tmp_path):
    import app.scheduler as sc
    monkeypatch.setattr(sc, 'SCHEDULER_STATE_PATH', tmp_path / 'state.json')
    scheduler = MagicMock()
    scheduler.state = sc.STATE_RUNNING
    jobs = {}
    for name in ['Yahoo', 'Backtest']:
        job = MagicMock()
        job.id, job.func, job.args, job.kwargs = name, lambda: None, (), {}
        jobs[name] = job
    scheduler.get_job.side_effect = jobs.get
    monkeypatch.setattr(sc, 'scheduler', scheduler)
    app = Flask(__name__)
    app.register_blueprint(sc.sc_bp)
    result = app.test_client().post('/update_job', json={
        'id': 'Backtest', 'after_job': 'Yahoo',
        'schedule': {'frequency': 'daily', 'hour': 15, 'minute': 50},
    })
    assert result.status_code == 200
    assert sc._load_scheduler_state()['jobs']['Backtest']['after_job'] == 'Yahoo'
    jobs['Backtest'].pause.assert_called_once()
    scheduler.add_job.assert_not_called()
    event = MagicMock(job_id='Yahoo', code=sc.EVENT_JOB_ERROR, exception=RuntimeError('download failed'))
    sc.job_listener(event)
    scheduler.add_job.assert_not_called()
    event = MagicMock(job_id='Yahoo', code=sc.EVENT_JOB_EXECUTED, retval={})
    sc.job_listener(event)
    assert scheduler.add_job.call_args.kwargs['id'] == 'Backtest_immediate'
    scheduler.add_job.reset_mock()
    sc._set_job_enabled('Backtest', False)
    sc._launch_followers('Yahoo')
    scheduler.add_job.assert_not_called()


def test_reject_cycle_and_restore_timer_when_chain_removed(monkeypatch, tmp_path):
    import app.scheduler as sc
    monkeypatch.setattr(sc, 'SCHEDULER_STATE_PATH', tmp_path / 'state.json')
    scheduler = MagicMock()
    jobs = {name: MagicMock(id=name, func=lambda: None) for name in ['A', 'B']}
    scheduler.get_job.side_effect = jobs.get
    monkeypatch.setattr(sc, 'scheduler', scheduler)
    app = Flask(__name__)
    app.register_blueprint(sc.sc_bp)
    client = app.test_client()
    schedule = {'frequency': 'daily', 'hour': 15, 'minute': 50}
    assert client.post('/update_job', json={'id': 'B', 'after_job': 'A', 'schedule': schedule}).status_code == 200
    assert client.post('/update_job', json={'id': 'A', 'after_job': 'B', 'schedule': schedule}).status_code == 400
    assert client.post('/update_job', json={'id': 'A', 'after_job': 'A', 'schedule': schedule}).status_code == 400
    jobs['B'].pause.reset_mock()
    assert client.post('/update_job', json={'id': 'B', 'after_job': None, 'schedule': schedule}).status_code == 200
    assert sc._load_scheduler_state()['jobs']['B']['after_job'] is None
    jobs['B'].pause.assert_not_called()

