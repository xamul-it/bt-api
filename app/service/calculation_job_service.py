"""Persistent dispatch for operator-triggered Watchtower calculations."""
from __future__ import annotations

import atexit
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.triggers.date import DateTrigger

import profile_baseline as pbl
import watchtower_runtime as wr
from run_provenance import PROVENANCE_SCHEMA_VERSION, _artifact_manifest, git_provenance


CALCULATION_JOB_PREFIX = "calculation:"
ALLOWED_KINDS = {"baseline_build", "backtest_rebuild", "simulation", "market_data_refresh"}
ACTIVE_STATUSES = ("queued", "running")
WORKSPACE_ROOT = Path(os.environ.get("BT_WORKSPACE_ROOT", Path(__file__).resolve().parents[3])).resolve()
CORE_PYTHON = Path(os.environ.get("BT_CORE_PYTHON", WORKSPACE_ROOT / "bt-core" / ".venv" / "bin" / "python"))
LOG_DIR = Path(os.environ.get("BT_CALCULATION_LOG_DIR", WORKSPACE_ROOT / "out" / "watchtower-calculations"))
LOCK_DIR = Path(os.environ.get("BT_CALCULATION_LOCK_DIR", f"/run/user/{os.getuid()}/backtrader-calculations"))


class CalculationConflict(ValueError):
    pass


class CalculationUnavailable(RuntimeError):
    pass


def _safe_profile_name(profile: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in profile)[:100]


def _git_identity(code_root: Path) -> dict[str, Any]:
    code = git_provenance(code_root, ignore_submodules=True)
    core = git_provenance(code_root / "bt-core")
    return {
        "code_commit": code.get("commit"), "core_commit": core.get("commit"),
        "code_dirty": code.get("dirty"), "core_dirty": core.get("dirty"),
        "git_available": bool(code.get("available") and core.get("available")),
    }


def settled_session_dates(window: int, *, now: datetime | None = None) -> list[str]:
    """Last N entry sessions with a concluded following session for exits."""
    if int(window) != 25:
        raise ValueError("window must be 25")
    now = now or datetime.now(timezone.utc)
    concluded = []
    for row in wr._load_cached_market_windows():
        closed = row.get("closed_at")
        if closed is not None and closed <= now:
            concluded.append(str(row["day"]))
    eligible = concluded[:-1]  # the following concluded bar is required for a closed round trip
    days = eligible[-window:]
    if len(days) != window:
        raise ValueError("calendar_has_insufficient_settled_sessions")
    return days


class CalculationJobService:
    def __init__(self, scheduler, repo):
        self.scheduler = scheduler
        self.repo = repo
        self._configured = False
        self._configure_lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._processes_lock = threading.Lock()
        atexit.register(self.shutdown)

    def _log_path(self, job_id: str) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR / f"{job_id}.log"

    def _schedule(self, job_id: str, *, delay_seconds: float = 0.0) -> None:
        run_at = datetime.now().astimezone()
        if delay_seconds:
            run_at = datetime.fromtimestamp(time.time() + delay_seconds).astimezone()
        self.scheduler.add_job(
            self.run_job, DateTrigger(run_date=run_at), args=[job_id],
            id=f"{CALCULATION_JOB_PREFIX}{job_id}", executor="calculations",
            replace_existing=True, max_instances=1, misfire_grace_time=86400,
        )

    def create_job(
        self, *, kind: str, profile: str, snapshot: dict[str, Any],
        baseline_id: int | None = None, progress_total: int = 0,
    ) -> dict[str, Any]:
        if kind not in ALLOWED_KINDS:
            raise ValueError("unsupported_calculation_kind")
        job_id = str(uuid.uuid4())
        try:
            row = self.repo.create_calculation_job(
                job_id=job_id, kind=kind, profile=profile,
                request_snapshot=snapshot, baseline_id=baseline_id,
                progress_total=progress_total, log_path=str(self._log_path(job_id)),
            )
        except ValueError as exc:
            if str(exc).startswith("active_calculation_exists:"):
                raise CalculationConflict(str(exc)) from exc
            raise
        self._schedule(job_id)
        return row

    def baseline_snapshot(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = str(payload.get("label") or "").strip()
        if not label:
            raise ValueError("label is required")
        if any(str(row.get("label") or "") == label for row in self.repo.list_profile_baselines(profile)):
            raise ValueError(f"duplicate_baseline_label:{profile}/{label}")
        try:
            window_start = date.fromisoformat(str(payload.get("window_start") or ""))
            window_end = date.fromisoformat(str(payload.get("window_end") or ""))
        except ValueError as exc:
            raise ValueError("window_start and window_end must be YYYY-MM-DD") from exc
        if payload.get("as_of_date"):
            raise ValueError("as_of_date is no longer supported; baselines use the current configuration")
        if window_end < window_start:
            raise ValueError("window_end must be on or after window_start")
        env = pbl.load_profile_env(profile)
        ctx = pbl.resolve_baseline_context(self.repo, profile, env, date.today())
        code_root = Path(env.get("CODE_ROOT") or WORKSPACE_ROOT).resolve()
        checkout = pbl.preflight_baseline_checkout(code_root, code_root / "bt-core")
        safe_keys = (
            "ROLE", "TRADING_MODE", "CODE_ROOT", "STRAT", "STRATARGS", "TICKER",
            "DATA_PROVIDER", "ALPACA_FEED", "MARGIN_LEVERAGE", "COMMISSION", "FROM_DAYS",
        )
        return {
            "schema_version": 1, "kind": "baseline_build", "profile": profile,
            "label": label, "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "profile_env": {key: env[key] for key in safe_keys if key in env},
            "resolved_configuration": ctx, "checkout": checkout,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

    def rebuild_snapshot(self, profile: str, baseline_id: int, window: int = 25) -> dict[str, Any]:
        baseline = self.repo.get_profile_baseline(int(baseline_id))
        if not baseline or baseline.get("profile") != profile:
            raise LookupError("baseline_not_found")
        required = {
            "code_commit": baseline.get("code_commit"),
            "core_commit": baseline.get("core_commit"),
            "configuration_hash": baseline.get("configuration_hash"),
            "params": baseline.get("params"),
            "effective_params": baseline.get("effective_params"),
            "run_config": baseline.get("run_config"),
            "artifact_manifest": baseline.get("artifact_manifest"),
        }
        missing = [key for key, value in required.items() if value in (None, {}, [])]
        if baseline.get("provenance_version") != PROVENANCE_SCHEMA_VERSION or missing:
            raise ValueError("baseline_configuration_incomplete:" + ",".join(missing or ["provenance_version"]))

        env = pbl.load_profile_env(profile)
        code_root = Path(env.get("CODE_ROOT") or WORKSPACE_ROOT).resolve()
        current = _git_identity(code_root)
        if not current["git_available"]:
            raise CalculationConflict("checkout_identity_unavailable")
        differences = [
            key for key in ("code_commit", "core_commit")
            if baseline.get(key) != current.get(key)
        ]
        if differences:
            raise CalculationConflict("baseline_checkout_incompatible:" + ",".join(differences))
        days = settled_session_dates(window)
        run_config = dict(baseline.get("run_config") or {})
        config = {
            "strategy": baseline["strategy"],
            "stratargs": dict(baseline.get("params") or {}),
            "effective_params": dict(baseline.get("effective_params") or {}),
            "ticker": baseline.get("ticker") or run_config.get("ticker"),
            "provider": baseline.get("provider") or run_config.get("provider"),
            "alpaca_feed": run_config.get("alpaca_feed") or pbl.DEFAULT_ALPACA_FEED,
            "margin_leverage": run_config.get("margin_leverage") or pbl.DEFAULT_MARGIN_LEVERAGE,
            "commission": run_config.get("commission") or pbl.DEFAULT_COMMISSION,
            "timeframe": run_config.get("timeframe") or "daily",
            "from_days": 420,
        }
        missing_config = [key for key in ("strategy", "stratargs", "ticker", "provider") if not config.get(key)]
        if missing_config:
            raise ValueError("baseline_configuration_incomplete:" + ",".join(missing_config))
        current_manifest = _artifact_manifest(
            {"ticker": config["ticker"]}, config["effective_params"], code_root,
        )
        raw_profile_env = pbl.read_env_file(pbl.SCHEDULED_DIR / f"{profile}.env")
        return {
            "schema_version": 1, "kind": "backtest_rebuild", "profile": profile,
            "baseline_id": int(baseline_id), "baseline_label": baseline.get("label"),
            "dates": days, "window": int(window), "configuration": config,
            "checkout": current, "baseline_configuration_hash": baseline["configuration_hash"],
            "baseline_artifact_manifest": baseline["artifact_manifest"],
            "current_dataset_manifest": current_manifest,
            "code_root": str(code_root),
            "historical_equity": {
                "source": "alpaca_portfolio_history",
                "account_env": raw_profile_env.get("ACCOUNT_ENV"),
                "trading_mode": env.get("TRADING_MODE"),
            },
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

    def preview_rebuild(self, profile: str, baseline_id: int, window: int = 25) -> dict[str, Any]:
        snapshot = self.rebuild_snapshot(profile, baseline_id, window)
        return {
            "profile": profile, "baseline_id": int(baseline_id),
            "baseline_label": snapshot.get("baseline_label"), "window": window,
            "dates": snapshot["dates"], "will_replace": "backtest_only",
        }

    def enqueue_baseline(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.create_job(
            kind="baseline_build", profile=profile,
            snapshot=self.baseline_snapshot(profile, payload), progress_total=0,
        )

    def enqueue_rebuild(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("replace_backtest") is not True:
            raise ValueError("replace_backtest=true is required")
        try:
            baseline_id = int(payload.get("baseline_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("baseline_id is required") from exc
        window = int(payload.get("window", 25))
        snapshot = self.rebuild_snapshot(profile, baseline_id, window)
        preview_dates = payload.get("preview_dates")
        if preview_dates is not None and preview_dates != snapshot["dates"]:
            raise CalculationConflict("preview_expired_dates_changed")
        return self.create_job(
            kind="backtest_rebuild", profile=profile, snapshot=snapshot,
            baseline_id=baseline_id, progress_total=len(snapshot["dates"]),
        )

    def configure_recovery(self) -> None:
        with self._configure_lock:
            if self._configured or not self.repo.available():
                return
            self._configured = True
        try:
            active = self.repo.list_calculation_jobs(limit=200, statuses=ACTIVE_STATUSES)
        except Exception:
            with self._configure_lock:
                self._configured = False
            raise
        # Recovery cannot be startup-only: a worker that survived the API
        # restart can still die afterwards without an owner to reap it.
        self.scheduler.add_job(
            self.reconcile_running_jobs, 'interval', seconds=30,
            id=f'{CALCULATION_JOB_PREFIX}recovery', executor='default',
            replace_existing=True, max_instances=1, coalesce=True,
        )
        for row in reversed(active):
            if row["status"] == "queued":
                self._schedule(row["id"])
                continue
            if self._running_job_is_alive(row):
                continue
            self.repo.interrupt_calculation_job(row["id"], "worker_abandoned_during_api_restart")

    def reconcile_running_jobs(self) -> None:
        for row in self.repo.list_calculation_jobs(limit=200, statuses=("running",)):
            if not self._running_job_is_alive(row):
                self.repo.interrupt_calculation_job(row["id"], "worker_abandoned")

    def _running_job_is_alive(self, row: dict[str, Any]) -> bool:
        # The runner acquires this before claiming the DB row and passes it
        # to the child. The OS releases it when the last owner exits. A recent
        # heartbeat or reused PID cannot establish that this worker is alive;
        # the global lock may also belong to a different profile entirely.
        handle = self._try_lock(LOCK_DIR / f"profile-{_safe_profile_name(row['profile'])}.lock")
        if handle is None:
            return True
        self._unlock(handle)
        return False

    @staticmethod
    def _try_lock(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    @staticmethod
    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def run_job(self, job_id: str) -> dict[str, Any]:
        row = self.repo.get_calculation_job(job_id)
        if not row or row.get("status") != "queued":
            return {"calculation_job": job_id, "action": "not_queued"}
        global_lock = self._try_lock(LOCK_DIR / "global.lock")
        profile_lock = self._try_lock(LOCK_DIR / f"profile-{_safe_profile_name(row['profile'])}.lock")
        if global_lock is None or profile_lock is None:
            if global_lock is not None:
                self._unlock(global_lock)
            if profile_lock is not None:
                self._unlock(profile_lock)
            self._schedule(job_id, delay_seconds=5)
            return {"calculation_job": job_id, "action": "requeued_lock_busy"}

        worker_token = uuid.uuid4().hex
        claimed = self.repo.claim_calculation_job(job_id, worker_token, os.getpid())
        if not claimed:
            self._unlock(profile_lock)
            self._unlock(global_lock)
            return {"calculation_job": job_id, "action": "claim_lost"}

        command = [
            str(CORE_PYTHON if CORE_PYTHON.exists() else Path(sys.executable)),
            str(WORKSPACE_ROOT / "bin" / "watchtower_calculation_worker.py"),
            "--job-id", job_id, "--worker-token", worker_token,
        ]
        timeout = int(os.environ.get(
            "BT_BASELINE_CALCULATION_TIMEOUT_SECONDS" if row["kind"] == "baseline_build"
            else "BT_REBUILD_CALCULATION_TIMEOUT_SECONDS",
            "14400" if row["kind"] == "baseline_build" else "21600",
        ))
        log_path = Path(row["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process = None
        try:
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, cwd=WORKSPACE_ROOT, stdout=log, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True,
                    pass_fds=(global_lock.fileno(), profile_lock.fileno()),
                )
                with self._processes_lock:
                    self._processes[job_id] = process
                self.repo.heartbeat_calculation_job(
                    job_id, worker_token, worker_pid=process.pid,
                )
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)
                    tail = self.read_log(job_id, limit=8000)
                    self.repo.finish_calculation_job(
                        job_id, worker_token, status="failed",
                        error=f"calculation_timeout_after_{timeout}_seconds", output_tail=tail,
                    )
                    return {"calculation_job": job_id, "action": "timeout"}
            final = self.repo.get_calculation_job(job_id)
            if final and final.get("status") == "running":
                tail = self.read_log(job_id, limit=8000)
                self.repo.finish_calculation_job(
                    job_id, worker_token, status="failed",
                    error=f"worker_exited_{returncode}_without_terminal_state", output_tail=tail,
                )
            else:
                self.repo.set_calculation_output_tail(job_id, self.read_log(job_id, limit=8000))
            return {"calculation_job": job_id, "action": "finished", "returncode": returncode}
        except Exception as exc:
            tail = self.read_log(job_id, limit=8000)
            try:
                self.repo.finish_calculation_job(
                    job_id, worker_token, status="failed",
                    error=f"calculation_runner_error:{exc}", output_tail=tail,
                )
            except Exception:
                pass
            return {"calculation_job": job_id, "action": "runner_error", "error": str(exc)}
        finally:
            with self._processes_lock:
                self._processes.pop(job_id, None)
            self._unlock(profile_lock)
            self._unlock(global_lock)

    def shutdown(self) -> None:
        """Bounded cleanup: no calculation child may outlive a clean API stop."""
        with self._processes_lock:
            processes = list(self._processes.items())
        for job_id, process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                self.repo.interrupt_calculation_job(job_id, "api_shutdown")
            except Exception:
                pass

    def read_log(self, job_id: str, *, limit: int = 8000) -> str:
        row = self.repo.get_calculation_job(job_id)
        if not row:
            raise LookupError("job_not_found")
        path = Path(row["log_path"])
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max(1, min(50000, int(limit)))))
                return handle.read().decode("utf-8", errors="replace")[-limit:]
        except FileNotFoundError:
            return row.get("output_tail") or ""
