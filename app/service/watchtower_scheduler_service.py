"""Idempotent, Scheduler-owned maintenance for Watchtower cron profiles.

These jobs only read broker state, classify missed telemetry, or replay local
history.  They must never submit, cancel, or modify broker orders.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import fcntl


WORKSPACE_ROOT = Path(os.environ.get("BT_WORKSPACE_ROOT", Path(__file__).resolve().parents[3]))
CORE_PYTHON = Path(os.environ.get("BT_CORE_PYTHON", WORKSPACE_ROOT / "bt-core" / ".venv" / "bin" / "python"))
_MAINTENANCE_LOCK = threading.Lock()
LOCK_PATH = Path(os.environ.get(
    "BT_WATCHTOWER_MAINTENANCE_LOCK",
    f"/run/user/{os.getuid()}/backtrader-watchtower-maintenance.lock",
))


@contextmanager
def _maintenance_lock():
    """Serialize the pipeline across threads *and* API processes."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _MAINTENANCE_LOCK, LOCK_PATH.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run(script: str, *args: str) -> dict:
    """Run one read-only Watchtower worker and surface its failure to APScheduler."""
    command = [str(CORE_PYTHON if CORE_PYTHON.exists() else Path(sys.executable)), str(WORKSPACE_ROOT / "bin" / script), *args]
    # Poll, watchdog and replay form one data pipeline.  A shared lock makes
    # startup recovery and normal timer invocations serialize rather than
    # letting a replay observe a half-written broker cache.
    with _maintenance_lock():
        completed = subprocess.run(
            command, cwd=WORKSPACE_ROOT, text=True, capture_output=True, check=False,
            timeout=int(os.environ.get("BT_WATCHTOWER_JOB_TIMEOUT_SECONDS", "1800")),
        )
    if completed.returncode:
        raise RuntimeError(f"{script} exited {completed.returncode}: {(completed.stderr or completed.stdout)[-2000:]}")
    return {"script": script, "output": completed.stdout[-2000:]}


def poll_alpaca_orders() -> dict:
    """Refresh recent Alpaca facts; the worker rereads the prior ten days."""
    return _run("watchtower_poll_alpaca_orders.py", "--days", "10")


def detect_missed_scheduled_runs() -> dict:
    """Record missed/stale strategy receipts for recent market sessions."""
    return _run("watchtower_scheduled_watchdog.py", "--lookback-days", "10")


def replay_reconciliation_catchup() -> dict:
    """Replay all closed pending days; this never accesses the trading API."""
    return _run("watchtower_replay_reconcile.py", "--catch-up")
