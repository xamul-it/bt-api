import json
from flask import Flask


def test_job_enabled_state_survives_reload(monkeypatch, tmp_path):
    import app.scheduler as scheduler_module

    state_path = tmp_path / "scheduler-state.json"
    monkeypatch.setattr(scheduler_module, "SCHEDULER_STATE_PATH", state_path)

    assert scheduler_module._job_enabled("drift") is True
    scheduler_module._set_job_enabled("drift", False)
    assert scheduler_module._job_enabled("drift") is False
    assert json.loads(state_path.read_text(encoding="utf-8"))["jobs"]["drift"] == {"enabled": False}


def test_restart_restores_disabled_job_as_paused(monkeypatch, tmp_path):
    import app.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "SCHEDULER_STATE_PATH", tmp_path / "scheduler-state.json")
    scheduler_module._set_job_enabled("disabled-job", False)

    class Job:
        id = "disabled-job"
        paused = False

        def pause(self):
            self.paused = True

    job = Job()
    assert scheduler_module._restore_job_state(job) is job
    assert job.paused is True


def test_global_scheduler_state_survives_reload(monkeypatch, tmp_path):
    import app.scheduler as scheduler_module

    state_path = tmp_path / "scheduler-state.json"
    monkeypatch.setattr(scheduler_module, "SCHEDULER_STATE_PATH", state_path)
    scheduler_module._set_scheduler_enabled(False)
    assert scheduler_module._load_scheduler_state()["scheduler_enabled"] is False
    scheduler_module._set_scheduler_enabled(True)
    assert scheduler_module._load_scheduler_state()["scheduler_enabled"] is True


def test_job_runtime_state_survives_reload(monkeypatch, tmp_path):
    import app.scheduler as scheduler_module

    state_path = tmp_path / "scheduler-state.json"
    monkeypatch.setattr(scheduler_module, "SCHEDULER_STATE_PATH", state_path)
    scheduler_module._set_job_runtime("Watchtower — poll Alpaca", "errore", "network unavailable")
    saved = scheduler_module._load_scheduler_state()["jobs"]["Watchtower — poll Alpaca"]
    assert saved["last_status"] == "errore"
    assert saved["last_error"] == "network unavailable"
    assert saved["last_finished_at"]


def test_pause_and_resume_routes_persist_job_state(monkeypatch, tmp_path):
    import app.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "SCHEDULER_STATE_PATH", tmp_path / "scheduler-state.json")

    class Job:
        def pause(self):
            pass

        def resume(self):
            pass

    class Scheduler:
        def get_job(self, job_id):
            return Job() if job_id == "drift" else None

    monkeypatch.setattr(scheduler_module, "scheduler", Scheduler())
    app = Flask(__name__)
    app.register_blueprint(scheduler_module.sc_bp, url_prefix="/dyn/sc")
    client = app.test_client()

    assert client.post("/dyn/sc/pause_job/drift").status_code == 200
    assert scheduler_module._job_enabled("drift") is False
    assert client.post("/dyn/sc/resume_job/drift").status_code == 200
    assert scheduler_module._job_enabled("drift") is True


def test_hourly_profile_baseline_drift_job_is_registered():
    import app.scheduler as scheduler_module

    job = scheduler_module.scheduler.get_job("Controllo drift baseline profili")
    assert job is not None
    assert "minute='0'" in str(job.trigger)
    assert job.max_instances == 1


def test_watchtower_recovery_jobs_are_registered_and_never_call_runstrat():
    import app.scheduler as scheduler_module

    expected = {
        "Watchtower — poll Alpaca",
        "Watchtower — watchdog profili",
        "Watchtower — replay riconciliazioni",
    }
    for job_id in expected:
        job = scheduler_module.scheduler.get_job(job_id)
        assert job is not None
        assert "runstrat" not in getattr(job.func, "__name__", "")
        assert job.max_instances == 1


def test_update_managed_schedule_validates_and_persists(monkeypatch, tmp_path):
    import app.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "SCHEDULER_STATE_PATH", tmp_path / "scheduler-state.json")

    class Job:
        def __init__(self):
            self.trigger = scheduler_module.CronTrigger(hour=16, minute=5)

        def reschedule(self, trigger):
            self.trigger = trigger

    job = Job()

    class Scheduler:
        def get_job(self, job_id):
            return job if job_id == "Watchtower — poll Alpaca" else None

    monkeypatch.setattr(scheduler_module, "scheduler", Scheduler())
    app = Flask(__name__)
    app.register_blueprint(scheduler_module.sc_bp, url_prefix="/dyn/sc")
    response = app.test_client().post("/dyn/sc/update_job", json={
        "id": "Watchtower — poll Alpaca",
        "schedule": {"frequency": "daily", "hour": 17, "minute": 30},
    })
    assert response.status_code == 200
    assert "hour='17'" in str(job.trigger)
    assert scheduler_module._schedule_payload("Watchtower — poll Alpaca")["minute"] == 30
