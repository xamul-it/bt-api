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
