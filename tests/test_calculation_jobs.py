from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
import subprocess

import pytest
from flask import Flask


class MemoryRepo:
    def __init__(self):
        self.jobs = {}
        self.lock = Lock()
        self.interrupted = []

    def available(self):
        return True

    def create_calculation_job(self, **values):
        with self.lock:
            if any(j["profile"] == values["profile"] and j["status"] in {"queued", "running"} for j in self.jobs.values()):
                raise ValueError(f"active_calculation_exists:{values['profile']}")
            row = {**values, "id": values["job_id"], "status": "queued", "done": False,
                   "created_at": datetime.now(timezone.utc).isoformat(), "result": {}}
            self.jobs[row["id"]] = row
            return dict(row)

    def list_calculation_jobs(self, **kwargs):
        statuses = kwargs.get('statuses')
        return [dict(row) for row in self.jobs.values() if not statuses or row['status'] in statuses]

    def get_calculation_job(self, job_id):
        row = self.jobs.get(job_id)
        return dict(row) if row else None

    def claim_calculation_job(self, job_id, token, pid):
        with self.lock:
            row = self.jobs[job_id]
            if row["status"] != "queued":
                return None
            row.update(status="running", worker_token=token, worker_pid=pid)
            return dict(row)

    def interrupt_calculation_job(self, job_id, reason):
        self.jobs[job_id].update(status="interrupted", error=reason, done=True)
        self.interrupted.append(job_id)

    def heartbeat_calculation_job(self, job_id, token, **updates):
        row = self.jobs[job_id]
        if row.get("worker_token") != token:
            return False
        row.update(updates)
        return True

    def finish_calculation_job(self, job_id, token, **updates):
        row = self.jobs[job_id]
        if row.get("worker_token") != token:
            return None
        row.update(updates, done=True)
        return dict(row)

    def set_calculation_output_tail(self, job_id, output):
        self.jobs[job_id]["output_tail"] = output
        return True


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def add_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_active_profile_collision_and_atomic_claim(tmp_path, monkeypatch):
    from app.service import calculation_job_service as calc

    monkeypatch.setattr(calc, "LOG_DIR", tmp_path)
    repo, scheduler = MemoryRepo(), FakeScheduler()
    service = calc.CalculationJobService(scheduler, repo)
    first = service.create_job(kind="baseline_build", profile="development", snapshot={"kind": "baseline_build"})
    with pytest.raises(calc.CalculationConflict):
        service.create_job(kind="backtest_rebuild", profile="development", snapshot={"kind": "backtest_rebuild"})

    claimed = []
    threads = [Thread(target=lambda token=t: claimed.append(repo.claim_calculation_job(first["id"], token, 1))) for t in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(row is not None for row in claimed) == 1
    assert scheduler.calls[0][1]["executor"] == "calculations"


def test_duplicate_baseline_label_is_rejected_before_backtest(monkeypatch):
    from app.service import calculation_job_service as calc

    class Repo:
        def list_profile_baselines(self, profile):
            assert profile == "live"
            return [{"label": "2026-10-13"}]

    service = calc.CalculationJobService(FakeScheduler(), Repo())
    monkeypatch.setattr(
        calc.pbl, "load_profile_env",
        lambda _profile: (_ for _ in ()).throw(AssertionError("must reject before loading the run")),
    )

    with pytest.raises(ValueError, match=r"duplicate_baseline_label:live/2026-10-13"):
        service.baseline_snapshot("live", {
            "label": "2026-10-13", "window_start": "2000-01-01", "window_end": "2026-01-01",
        })


def test_recovery_schedules_queued_once_and_interrupts_abandoned(monkeypatch):
    from app.service import calculation_job_service as calc

    repo, scheduler = MemoryRepo(), FakeScheduler()
    now = datetime.now(timezone.utc)
    repo.jobs = {
        "queued": {"id": "queued", "profile": "a", "status": "queued", "created_at": now.isoformat()},
        "dead": {"id": "dead", "profile": "b", "status": "running", "worker_pid": 99999999,
                 "heartbeat_at": (now - timedelta(hours=1)).isoformat(), "created_at": now.isoformat()},
    }
    service = calc.CalculationJobService(scheduler, repo)
    monkeypatch.setattr(service, "_running_job_is_alive", lambda row: row["id"] != "dead")
    service.configure_recovery()
    service.configure_recovery()
    assert [call[1]["id"] for call in scheduler.calls] == ["calculation:recovery", "calculation:queued"]
    assert repo.interrupted == ["dead"]


def test_restart_interrupts_dead_worker_despite_recent_heartbeat_and_existing_pid(tmp_path, monkeypatch):
    import os
    from app.service import calculation_job_service as calc

    monkeypatch.setattr(calc, 'LOCK_DIR', tmp_path)
    repo, scheduler = MemoryRepo(), FakeScheduler()
    repo.jobs['dead'] = {
        'id': 'dead', 'profile': 'development', 'status': 'running',
        'worker_pid': os.getpid(),  # An existing unrelated/reused PID is insufficient.
        'heartbeat_at': datetime.now(timezone.utc).isoformat(),
    }
    service = calc.CalculationJobService(scheduler, repo)
    # An unrelated calculation's global lock must not preserve this dead job.
    global_lock = service._try_lock(tmp_path / 'global.lock')
    other_lock = service._try_lock(tmp_path / 'profile-mirror.lock')
    try:
        service.configure_recovery()
        assert repo.jobs['dead']['status'] == 'interrupted'
        assert repo.interrupted == ['dead']
    finally:
        service._unlock(other_lock)
        service._unlock(global_lock)


def test_live_worker_survives_restart_and_is_reconciled_after_later_death(tmp_path, monkeypatch):
    from app.service import calculation_job_service as calc

    monkeypatch.setattr(calc, 'LOCK_DIR', tmp_path)
    repo, scheduler = MemoryRepo(), FakeScheduler()
    repo.jobs['live'] = {
        'id': 'live', 'profile': 'development', 'status': 'running',
        'worker_pid': 99999999,
        'heartbeat_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    service = calc.CalculationJobService(scheduler, repo)
    profile_lock = service._try_lock(tmp_path / 'profile-development.lock')
    try:
        service.configure_recovery()
        assert repo.jobs['live']['status'] == 'running'
        recovery_call = next(call for call in scheduler.calls if call[1]['id'] == 'calculation:recovery')
        assert recovery_call[1]['executor'] == 'default'
        recovery_call[0][0]()
        assert not repo.interrupted
    finally:
        service._unlock(profile_lock)
    recovery_call[0][0]()
    assert repo.jobs['live']['status'] == 'interrupted'
    recovery_call[0][0]()
    assert repo.interrupted == ['live']
    assert not any(call[1]['id'] == 'calculation:live' for call in scheduler.calls)


def test_rebuild_contract_confirmation_cross_profile_and_collision(monkeypatch):
    import app.watchtower as watchtower
    from app.service.calculation_job_service import CalculationConflict

    class Repo:
        def available(self): return True

    class Service:
        calls = []

        def enqueue_rebuild(self, profile, payload):
            self.calls.append((profile, payload))
            if payload.get("baseline_id") == 2:
                raise LookupError("baseline_not_found")
            if payload.get("baseline_id") == 3:
                raise CalculationConflict("active_calculation_exists:development")
            if payload.get("replace_backtest") is not True:
                raise ValueError("replace_backtest=true is required")
            return {"id": "job-1", "kind": "backtest_rebuild", "status": "queued"}

    service = Service()
    monkeypatch.setattr(watchtower, "repo", Repo())
    monkeypatch.setattr(watchtower, "calculation_service", service)
    app = Flask(__name__)
    app.register_blueprint(watchtower.obs_bp, url_prefix="/dyn/obs")
    client = app.test_client()

    path = "/dyn/obs/watchtower/cron/development/backtest-rebuild"
    assert client.post(path, json={"baseline_id": 1}).status_code == 400
    assert client.post(path, json={"baseline_id": 2, "replace_backtest": True}).status_code == 404
    assert client.post(path, json={"baseline_id": 3, "replace_backtest": True}).status_code == 409
    response = client.post(path, json={"baseline_id": 1, "window": 25, "replace_backtest": True})
    assert response.status_code == 202
    assert response.get_json()["id"] == "job-1"


def test_rebuild_snapshot_uses_baseline_configuration_and_rejects_legacy_or_code_mismatch(tmp_path, monkeypatch):
    from app.service import calculation_job_service as calc

    baseline = {
        "id": 85, "profile": "development", "label": "frozen",
        "strategy": "overnight_ah.OvernightAH", "params": {"period": 17},
        "effective_params": {"period": 17, "defaulted": True},
        "ticker": "frozen.json", "provider": "yahoo", "provenance_version": calc.PROVENANCE_SCHEMA_VERSION,
        "code_commit": "code", "core_commit": "core", "configuration_hash": "hash",
        "artifact_manifest": [{"path": "frozen.json", "sha256": "abc"}],
        "run_config": {"alpaca_feed": "iex", "margin_leverage": 2, "commission": "none", "timeframe": "daily"},
    }

    class Repo:
        def get_profile_baseline(self, baseline_id):
            return dict(baseline) if baseline_id == 85 else None

    service = calc.CalculationJobService(FakeScheduler(), Repo())
    monkeypatch.setattr(calc.pbl, "load_profile_env", lambda _profile: {
        "CODE_ROOT": str(tmp_path), "STRATARGS": "period=999", "TRADING_MODE": "paper",
    })
    monkeypatch.setattr(calc.pbl, "read_env_file", lambda _path: {"ACCOUNT_ENV": "account.env"})
    monkeypatch.setattr(calc, "_git_identity", lambda _root: {
        "git_available": True, "code_commit": "code", "core_commit": "core",
        "code_dirty": False, "core_dirty": False,
    })
    monkeypatch.setattr(calc, "settled_session_dates", lambda window: [f"2026-09-{day:02d}" for day in range(1, 26)])
    monkeypatch.setattr(calc, "_artifact_manifest", lambda *_args: [{"path": "current", "sha256": "def"}])

    snapshot = service.rebuild_snapshot("development", 85, 25)
    assert snapshot["configuration"]["stratargs"] == {"period": 17}
    assert snapshot["historical_equity"]["account_env"] == "account.env"
    assert len(snapshot["dates"]) == 25
    with pytest.raises(LookupError):
        service.rebuild_snapshot("mirror", 85, 25)

    baseline["provenance_version"] = None
    with pytest.raises(ValueError, match="baseline_configuration_incomplete"):
        service.rebuild_snapshot("development", 85, 25)
    baseline["provenance_version"] = calc.PROVENANCE_SCHEMA_VERSION
    monkeypatch.setattr(calc, "_git_identity", lambda _root: {
        "git_available": True, "code_commit": "other", "core_commit": "core",
        "code_dirty": False, "core_dirty": False,
    })
    with pytest.raises(calc.CalculationConflict, match="baseline_checkout_incompatible"):
        service.rebuild_snapshot("development", 85, 25)


def test_runner_timeout_terminates_group_and_persists_failure(tmp_path, monkeypatch):
    from app.service import calculation_job_service as calc

    repo, scheduler = MemoryRepo(), FakeScheduler()
    monkeypatch.setattr(calc, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(calc, "LOCK_DIR", tmp_path / "locks")
    service = calc.CalculationJobService(scheduler, repo)
    row = service.create_job(
        kind="backtest_rebuild", profile="development",
        snapshot={"kind": "backtest_rebuild", "profile": "development"},
    )
    waits = []

    class Process:
        pid = 12345

        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("worker", timeout)
            return -15

        def poll(self): return None

    monkeypatch.setattr(calc.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    killed = []
    monkeypatch.setattr(calc.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setenv("BT_REBUILD_CALCULATION_TIMEOUT_SECONDS", "1")

    result = service.run_job(row["id"])
    final = repo.get_calculation_job(row["id"])
    assert result["action"] == "timeout"
    assert final["status"] == "failed"
    assert final["error"] == "calculation_timeout_after_1_seconds"
    assert killed and killed[0][0] == 12345


def test_settled_sessions_require_a_concluded_exit_bar(monkeypatch):
    from app.service import calculation_job_service as calc

    now = datetime.now(timezone.utc)
    rows = [
        {"day": f"2026-09-{day:02d}", "closed_at": now - timedelta(days=30 - day)}
        for day in range(1, 26)
    ]
    monkeypatch.setattr(calc.wr, "_load_cached_market_windows", lambda: rows)
    with pytest.raises(ValueError, match="insufficient_settled_sessions"):
        calc.settled_session_dates(25, now=now)
    rows.append({"day": "2026-09-26", "closed_at": now - timedelta(days=1)})
    assert calc.settled_session_dates(25, now=now)[-1] == "2026-09-25"
