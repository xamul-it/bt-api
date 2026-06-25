import os
import json
import subprocess
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from flask import Blueprint, Response, jsonify, request

from app.scheduler import scheduler
from app.service.main_service import repo


obs_bp = Blueprint("watchtower", __name__)
_alpaca_sync_jobs = {}
_alpaca_sync_jobs_lock = threading.Lock()
_baseline_jobs = {}
_baseline_jobs_lock = threading.Lock()


DEFAULT_SYSTEMD_SERVICES = [
    "bt-live-event-writer.service",
    "bt-watchtower.service",
    "parallel-sim.service",
    "parallel-sim-eod.service",
    "pa2.service",
    "pa2-shadow.service",
    "zmq-proxy.service",
    "zmq-logger.service",
]
SERVICES_CONFIG_PATH = os.environ.get(
    "BT_OBSERVABILITY_SERVICES_FILE",
    "/home/htpc/backtrader/bt-api/config/watchtower_services.json",
)


def _normalize_service_name(name):
    value = str(name or "").strip()
    if not value:
        return ""
    if "." not in value:
        value = f"{value}.service"
    return value


def _load_services_config():
    if not os.path.exists(SERVICES_CONFIG_PATH):
        return None
    try:
        with open(SERVICES_CONFIG_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, list):
        return None
    normalized = []
    for item in services:
        service = _normalize_service_name(item)
        if service and service not in normalized:
            normalized.append(service)
    return normalized


def _save_services_config(services):
    os.makedirs(os.path.dirname(SERVICES_CONFIG_PATH), exist_ok=True)
    payload = {"services": services}
    with open(SERVICES_CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _configured_services():
    configured = _load_services_config()
    if configured is not None:
        return configured
    raw = os.environ.get("BT_SYSTEMD_SERVICES", "")
    if raw.strip():
        values = []
        for item in raw.split(","):
            service = _normalize_service_name(item)
            if service and service not in values:
                values.append(service)
        return values
    return list(DEFAULT_SYSTEMD_SERVICES)


def _require_repo():
    if not repo.available():
        return jsonify({"error": "Postgres DSN not configured"}), 503
    return None


def _watchtower_window_args():
    window_open = request.args.get("window_open", "").strip()
    if not window_open:
        return None, None, None
    try:
        window = repo._resolve_watchtower_window(window_open)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), None, None
    return None, window.get("opened_at"), window.get("closed_at")


def _watchtower_context_args():
    return (
        str(request.args.get("portfolio_key_id") or "").strip() or None,
        str(request.args.get("chain_run_id") or "").strip() or None,
    )


def _feed_monitor_dates_from_request(payload=None):
    source = payload if isinstance(payload, dict) else request.args
    start_raw = str(source.get("start_date") or source.get("date") or "").strip()
    end_raw = str(source.get("end_date") or start_raw or "").strip()
    if not start_raw:
        raise ValueError("start_date is required")
    try:
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    except ValueError as exc:
        raise ValueError("invalid_date_range") from exc
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")
    return start_date, end_date


def _feed_monitor_symbols_from_payload(payload):
    raw = payload.get("symbols")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("symbols must be a list")
    values = []
    for item in raw:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in values:
            values.append(symbol)
    return values


def _alpaca_sync_job_snapshot(job_id):
    with _alpaca_sync_jobs_lock:
        job = _alpaca_sync_jobs.get(job_id)
        return dict(job) if job else None


def _alpaca_sync_job_update(job_id, **updates):
    with _alpaca_sync_jobs_lock:
        job = _alpaca_sync_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _baseline_job_snapshot(job_id):
    with _baseline_jobs_lock:
        job = _baseline_jobs.get(job_id)
        return dict(job) if job else None


def _baseline_job_update(job_id, **updates):
    with _baseline_jobs_lock:
        job = _baseline_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _baseline_request_context(payload):
    run_id = str(payload.get("run_id") or "").strip()
    if run_id:
        run = repo.fetch_run(run_id)
        if not run:
            raise ValueError("run_not_found")
        return {
            "run_id": run_id,
            "strategy": str(run.get("strategy") or "").strip(),
            "strategy_fingerprint": str(run.get("strategy_fingerprint") or "").strip(),
            "params": run.get("params") or {},
        }

    strategy = str(payload.get("strategy") or "").strip()
    if not strategy:
        raise ValueError("strategy is required")
    fingerprint = str(payload.get("strategy_fingerprint") or payload.get("statVersion") or "").strip()
    if not fingerprint:
        raise ValueError("strategy_fingerprint is required")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return {
        "run_id": None,
        "strategy": strategy,
        "strategy_fingerprint": fingerprint,
        "params": params,
    }


def _baseline_source_paths(payload):
    raw_paths = payload.get("source_paths")
    if raw_paths is None:
        single = str(payload.get("source_path") or "").strip()
        raw_paths = [single] if single else []
    if not isinstance(raw_paths, list):
        raise ValueError("source_paths must be a list")
    paths = []
    for item in raw_paths:
        value = str(item or "").strip()
        if value and value not in paths:
            paths.append(value)
    if not paths:
        raise ValueError("source_paths is required")
    return paths


def _baseline_context_from_files(source_paths, source_root=None):
    candidates = []
    root = str(source_root or "").strip()
    if root:
        candidates.append(Path(root).expanduser())
    for item in source_paths or []:
        value = str(item or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        candidates.append(path if path.is_dir() else path.parent)

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        results_path = resolved / "results.json"
        if not results_path.exists():
            continue
        try:
            payload = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records = list(payload.values()) if isinstance(payload, dict) else []
        records = [row for row in records if isinstance(row, dict)]
        if not records:
            continue
        record = records[-1]
        strategy = str(record.get("strategy") or record.get("input_strat") or "").strip()
        fingerprint = str(
            record.get("strategy_fingerprint")
            or record.get("statVersion")
            or record.get("input_strategy_fingerprint")
            or record.get("input_statVersion")
            or os.environ.get("BT_STRATEGY_STAT_VERSION_DEFAULT", "")
        ).strip()
        params = {}
        for key, value in record.items():
            if key.startswith("param_"):
                params[key[6:]] = value
        if strategy:
            return {
                "run_id": None,
                "strategy": strategy,
                "strategy_fingerprint": fingerprint,
                "params": params,
            }
    raise ValueError("baseline_context_not_derivable_from_files")


def _start_baseline_job(payload):
    source_paths = _baseline_source_paths(payload)
    source_root = str(payload.get("source_root") or "").strip() or None
    try:
        ctx = _baseline_request_context(payload)
    except ValueError:
        ctx = _baseline_context_from_files(source_paths, source_root=source_root)
    notes = str(payload.get("notes") or "").strip() or None
    generator_schema_version = str(
        payload.get("generator_schema_version") or "watchtower-baseline-v1"
    ).strip() or "watchtower-baseline-v1"
    job_id = uuid.uuid4().hex
    with _baseline_jobs_lock:
        _baseline_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "done": False,
            "run_id": ctx.get("run_id"),
            "strategy": ctx["strategy"],
            "strategy_fingerprint": ctx["strategy_fingerprint"],
            "source_paths": source_paths,
            "source_root": source_root,
            "created_at": time.time(),
            "updated_at": time.time(),
            "error": None,
        }

    def _runner():
        _baseline_job_update(job_id, status="running")
        try:
            result = repo.compute_baseline_from_sources(
                strategy=ctx["strategy"],
                fingerprint=ctx["strategy_fingerprint"],
                params_data=ctx["params"],
                source_paths=source_paths,
                source_root=source_root,
                generator_schema_version=generator_schema_version,
                notes=notes,
            )
            _baseline_job_update(
                job_id,
                status="completed",
                done=True,
                result=result,
            )
        except Exception as exc:
            _baseline_job_update(
                job_id,
                status="failed",
                done=True,
                error=str(exc),
            )

    thread = threading.Thread(target=_runner, name=f"baseline-{job_id[:8]}", daemon=True)
    thread.start()
    return _baseline_job_snapshot(job_id)


def _start_alpaca_sync_job(window_open, window_start, window_end, portfolio_key_id=None, chain_run_id=None):
    estimate = repo.estimate_alpaca_sync_total(
        window_start=window_start,
        window_end=window_end,
        portfolio_key_id=portfolio_key_id,
        chain_run_id=chain_run_id,
    )
    estimated_total = int(estimate.get("estimated_total", 0) or 0)
    job_id = uuid.uuid4().hex
    with _alpaca_sync_jobs_lock:
        _alpaca_sync_jobs[job_id] = {
            "job_id": job_id,
            "window_open": window_open,
            "status": "queued",
            "estimated_total": estimated_total,
            "processed": 0,
            "progress": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "run_id": estimate.get("run_id"),
            "observed_mode": estimate.get("observed_mode"),
            "portfolio_key_id": estimate.get("portfolio_key_id") or portfolio_key_id,
            "error": None,
            "done": False,
        }

    def _runner():
        _alpaca_sync_job_update(job_id, status="running")

        def _progress(payload):
            processed = int(payload.get("upserted", 0) or 0)
            total = int(payload.get("estimated_total", estimated_total) or 0)
            progress = 0
            if total > 0:
                progress = min(100, int((processed / total) * 100))
            _alpaca_sync_job_update(
                job_id,
                processed=processed,
                estimated_total=total,
                progress=progress,
                current_order_id=payload.get("current_order_id"),
                current_symbol=payload.get("symbol"),
            )

        try:
            result = repo._sync_alpaca_order_cache(
                window_start=window_start,
                window_end=window_end,
                portfolio_key_id=portfolio_key_id,
                force=True,
                progress_callback=_progress,
                estimated_total=estimated_total,
            )
            final_processed = int(result.get("upserted", 0) or 0)
            final_total = max(estimated_total, final_processed)
            final_progress = 100 if final_total >= 0 else 0
            _alpaca_sync_job_update(
                job_id,
                status="completed",
                processed=final_processed,
                estimated_total=final_total,
                progress=final_progress,
                done=True,
                result=result,
            )
        except Exception as exc:
            _alpaca_sync_job_update(
                job_id,
                status="failed",
                error=str(exc),
                done=True,
            )

    thread = threading.Thread(target=_runner, name=f"alpaca-sync-{job_id[:8]}", daemon=True)
    thread.start()
    return _alpaca_sync_job_snapshot(job_id)


def _start_feed_monitor_sync_job(start_date, end_date, symbols=None):
    coverage = repo.scan_feed_monitor_coverage(start_date, end_date, symbols=symbols)
    requested_symbols = coverage.get("missing_historical_symbols") or []
    if symbols:
        wanted = {str(item).strip().upper() for item in symbols if str(item).strip()}
        requested_symbols = [item for item in requested_symbols if item in wanted]
    job_id = uuid.uuid4().hex
    snapshot = repo.create_feed_monitor_job(
        job_id=job_id,
        job_type="feed_monitor_sync",
        start_date=start_date,
        end_date=end_date,
        total_symbols=len(requested_symbols),
        filters={"symbols": requested_symbols},
    )

    def _runner():
        repo.update_feed_monitor_job(job_id, status="running")

        def _progress(payload):
            total = int(payload.get("total_symbols", len(requested_symbols)) or 0)
            completed = int(payload.get("completed_symbols", 0) or 0)
            progress = float(payload.get("progress", 0) or 0)
            repo.update_feed_monitor_job(
                job_id,
                total_symbols=total,
                completed_symbols=completed,
                current_symbol=payload.get("current_symbol"),
                progress=progress,
            )

        try:
            result = repo.sync_feed_monitor_historical(
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                progress_callback=_progress,
            )
            repo.update_feed_monitor_job(
                job_id,
                status="completed",
                total_symbols=len(requested_symbols),
                completed_symbols=len(requested_symbols),
                current_symbol=None,
                progress=1.0,
                result=result,
                done=True,
            )
        except Exception as exc:
            repo.update_feed_monitor_job(
                job_id,
                status="failed",
                error=str(exc),
                done=True,
            )

    thread = threading.Thread(target=_runner, name=f"feed-sync-{job_id[:8]}", daemon=True)
    thread.start()
    return snapshot


def _start_feed_monitor_match_job(start_date, end_date, symbols=None, force=False):
    coverage = repo.scan_feed_monitor_coverage(start_date, end_date, symbols=symbols)
    symbol_days = [row for row in coverage.get("symbol_days", []) if row.get("historical_status") == "available"]
    job_id = uuid.uuid4().hex
    snapshot = repo.create_feed_monitor_job(
        job_id=job_id,
        job_type="feed_monitor_match",
        start_date=start_date,
        end_date=end_date,
        total_symbols=len(symbol_days),
        filters={"symbols": symbols or [], "force": bool(force)},
    )

    def _runner():
        repo.update_feed_monitor_job(job_id, status="running")

        def _progress(payload):
            total = int(payload.get("total_symbols", len(symbol_days)) or 0)
            completed = int(payload.get("completed_symbols", 0) or 0)
            progress = float(payload.get("progress", 0) or 0)
            repo.update_feed_monitor_job(
                job_id,
                total_symbols=total,
                completed_symbols=completed,
                current_symbol=payload.get("current_symbol"),
                progress=progress,
            )

        try:
            result = repo.compute_feed_monitor_matches(
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                progress_callback=_progress,
                force=bool(force),
            )
            repo.update_feed_monitor_job(
                job_id,
                status="completed",
                total_symbols=len(symbol_days),
                completed_symbols=len(symbol_days),
                current_symbol=None,
                progress=1.0,
                result=result,
                done=True,
            )
        except Exception as exc:
            repo.update_feed_monitor_job(
                job_id,
                status="failed",
                error=str(exc),
                done=True,
            )

    thread = threading.Thread(target=_runner, name=f"feed-match-{job_id[:8]}", daemon=True)
    thread.start()
    return snapshot


def _systemctl(*args):
    cmd = ["systemctl", "--user", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _systemctl_show(service):
    cmd = [
        "systemctl",
        "--user",
        "show",
        service,
        "--property=Id,LoadState,ActiveState,SubState,UnitFileState,Description",
        "--no-pager",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    payload = {
        "name": service,
        "load": "unknown",
        "active": "unknown",
        "sub": "unknown",
        "enabled": "unknown",
        "description": "",
        "exists": False,
        "active_ok": False,
        "raw_error": proc.stderr.strip(),
    }
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if key == "Id" and value:
            payload["name"] = value
        elif key == "LoadState":
            payload["load"] = value or "unknown"
        elif key == "ActiveState":
            payload["active"] = value or "unknown"
        elif key == "SubState":
            payload["sub"] = value or "unknown"
        elif key == "UnitFileState":
            payload["enabled"] = value or "unknown"
        elif key == "Description":
            payload["description"] = value
    payload["exists"] = payload["load"] != "not-found"
    payload["active_ok"] = payload["active"] == "active"
    return payload


def _list_available_services():
    cmd = [
        "systemctl",
        "--user",
        "list-unit-files",
        "--type=service",
        "--no-pager",
        "--no-legend",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    rows = []
    seen = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0].strip()
        if not unit.endswith(".service") or unit in seen:
            continue
        seen.add(unit)
        snapshot = _systemctl_show(unit)
        rows.append(snapshot)
    rows.sort(key=lambda item: item["name"])
    return rows


@obs_bp.route("/runs", methods=["GET"])
def runs():
    missing = _require_repo()
    if missing:
        return missing
    limit = int(request.args.get("limit", "200"))
    include_deleted = request.args.get("include_deleted", "false").lower() == "true"
    return jsonify(repo.fetch_runs(include_deleted=include_deleted, limit=limit))


@obs_bp.route("/runs/<run_id>", methods=["GET"])
def run_detail(run_id):
    missing = _require_repo()
    if missing:
        return missing
    payload = repo.fetch_run(run_id)
    if not payload:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(payload)


@obs_bp.route("/watchtower", methods=["GET"])
def watchtower_reports():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    portfolio_key_id, chain_run_id = _watchtower_context_args()
    limit = int(request.args.get("limit", "50"))
    return jsonify(repo.latest_watchtower_reports(
        limit=limit,
        window_start=window_start,
        window_end=window_end,
        portfolio_key_id=portfolio_key_id,
        chain_run_id=chain_run_id,
    ))


@obs_bp.route("/watchtower/windows", methods=["GET"])
def watchtower_windows():
    missing = _require_repo()
    if missing:
        return missing
    limit = int(request.args.get("limit", "30"))
    return jsonify(repo.list_watchtower_windows(limit=limit))


@obs_bp.route("/watchtower/portfolio-contexts", methods=["GET"])
def watchtower_portfolio_contexts():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    return jsonify(repo.list_portfolio_contexts(window_start=window_start, window_end=window_end))


@obs_bp.route("/watchtower/portfolio-session", methods=["GET"])
def watchtower_portfolio_session():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    portfolio_key_id, chain_run_id = _watchtower_context_args()
    if not portfolio_key_id:
        return jsonify({"error": "portfolio_key_id is required"}), 400
    payload = repo.portfolio_window_session(
        window_start=window_start,
        window_end=window_end,
        portfolio_key_id=portfolio_key_id,
        chain_run_id=chain_run_id,
    )
    return jsonify(payload or {"window_open": None, "portfolio_key_id": portfolio_key_id, "chain_run_ids": []})


@obs_bp.route("/watchtower/current-window", methods=["GET"])
def watchtower_current_window():
    missing = _require_repo()
    if missing:
        return missing
    current = repo.current_window_open()
    if not current:
        return jsonify({"window_open": None})
    window = repo._resolve_watchtower_window(current)
    return jsonify(window)


@obs_bp.route("/watchtower/rebuild", methods=["POST"])
def watchtower_rebuild():
    missing = _require_repo()
    if missing:
        return missing
    payload = request.get_json(silent=True) or {}
    window_open = str(payload.get("window_open") or "").strip()
    sync = bool(payload.get("sync", True))
    if not window_open:
        return jsonify({"error": "window_open is required"}), 400
    try:
        window = repo._resolve_watchtower_window(window_open)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    repo.request_window_rebuild(window_open)
    processed = None
    if sync:
        from reconciliation_watchdog import run_once
        conn = None
        try:
            conn = repo.connect()
            processed = run_once(conn, run_id=None, repo=repo, window_open=window_open)
        finally:
            if conn is not None:
                conn.close()
    return jsonify({
        "status": "rebuilt" if sync else "rebuild_requested",
        "window": window,
        "processed_discrepancies": processed,
    })


@obs_bp.route("/watchtower/alpaca-sync", methods=["POST"])
def watchtower_alpaca_sync():
    missing = _require_repo()
    if missing:
        return missing
    payload = request.get_json(silent=True) or {}
    window_open = str(payload.get("window_open") or "").strip()
    if not window_open:
        return jsonify({"error": "window_open is required"}), 400
    try:
        window = repo._resolve_watchtower_window(window_open)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    portfolio_key_id = str(payload.get("portfolio_key_id") or "").strip() or None
    chain_run_id = str(payload.get("chain_run_id") or "").strip() or None
    job = _start_alpaca_sync_job(window_open, window.get("opened_at"), window.get("closed_at"), portfolio_key_id=portfolio_key_id, chain_run_id=chain_run_id)
    return jsonify(job), 202


@obs_bp.route("/watchtower/alpaca-sync/<job_id>", methods=["GET"])
def watchtower_alpaca_sync_status(job_id):
    job = _alpaca_sync_job_snapshot(job_id)
    if not job:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify(job)


@obs_bp.route("/watchtower/feed-monitoring/meta", methods=["GET"])
def watchtower_feed_monitor_meta():
    missing = _require_repo()
    if missing:
        return missing
    limit = int(request.args.get("limit", "90"))
    return jsonify(repo.feed_monitor_metadata(limit=limit))


@obs_bp.route("/watchtower/feed-monitoring/coverage", methods=["GET"])
def watchtower_feed_monitor_coverage():
    missing = _require_repo()
    if missing:
        return missing
    try:
        start_date, end_date = _feed_monitor_dates_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    symbols = request.args.getlist("symbol")
    if not symbols:
        raw_symbols = str(request.args.get("symbols") or "").strip()
        if raw_symbols:
            symbols = [item.strip().upper() for item in raw_symbols.split(",") if item.strip()]
    payload = repo.scan_feed_monitor_coverage(start_date, end_date, symbols=symbols or None)
    return jsonify(payload)


@obs_bp.route("/watchtower/feed-monitoring/sync", methods=["POST"])
def watchtower_feed_monitor_sync():
    missing = _require_repo()
    if missing:
        return missing
    payload = request.get_json(silent=True) or {}
    try:
        start_date, end_date = _feed_monitor_dates_from_request(payload)
        symbols = _feed_monitor_symbols_from_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    job = _start_feed_monitor_sync_job(start_date, end_date, symbols=symbols)
    return jsonify(job), 202


@obs_bp.route("/watchtower/feed-monitoring/sync/<job_id>", methods=["GET"])
def watchtower_feed_monitor_sync_status(job_id):
    missing = _require_repo()
    if missing:
        return missing
    payload = repo.feed_monitor_job(job_id)
    if not payload or payload.get("job_type") != "feed_monitor_sync":
        return jsonify({"error": "job_not_found"}), 404
    return jsonify(payload)


@obs_bp.route("/watchtower/feed-monitoring/match", methods=["POST"])
def watchtower_feed_monitor_match():
    missing = _require_repo()
    if missing:
        return missing
    payload = request.get_json(silent=True) or {}
    try:
        start_date, end_date = _feed_monitor_dates_from_request(payload)
        symbols = _feed_monitor_symbols_from_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    force = bool(payload.get("force", False))
    job = _start_feed_monitor_match_job(start_date, end_date, symbols=symbols, force=force)
    return jsonify(job), 202


@obs_bp.route("/watchtower/feed-monitoring/match/<job_id>", methods=["GET"])
def watchtower_feed_monitor_match_status(job_id):
    missing = _require_repo()
    if missing:
        return missing
    payload = repo.feed_monitor_job(job_id)
    if not payload or payload.get("job_type") != "feed_monitor_match":
        return jsonify({"error": "job_not_found"}), 404
    return jsonify(payload)


@obs_bp.route("/watchtower/feed-monitoring/summary", methods=["GET"])
def watchtower_feed_monitor_summary():
    missing = _require_repo()
    if missing:
        return missing
    try:
        start_date, end_date = _feed_monitor_dates_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    symbol = str(request.args.get("symbol") or "").strip().upper() or None
    return jsonify(repo.feed_monitor_summaries(start_date, end_date, symbol=symbol))


@obs_bp.route("/watchtower/feed-monitoring/field-pivot", methods=["GET"])
def watchtower_feed_monitor_field_pivot():
    missing = _require_repo()
    if missing:
        return missing
    try:
        start_date, end_date = _feed_monitor_dates_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    symbol = str(request.args.get("symbol") or "").strip().upper() or None
    return jsonify(repo.feed_monitor_field_mismatch_pivot(start_date, end_date, symbol=symbol))


@obs_bp.route("/watchtower/feed-monitoring/discrepancies", methods=["GET"])
def watchtower_feed_monitor_discrepancies():
    missing = _require_repo()
    if missing:
        return missing
    try:
        start_date, end_date = _feed_monitor_dates_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    symbol = str(request.args.get("symbol") or "").strip().upper() or None
    discrepancy_type = str(request.args.get("discrepancy_type") or "").strip() or None
    limit = int(request.args.get("limit", "500"))
    offset = int(request.args.get("offset", "0"))
    payload = repo.feed_monitor_discrepancies(
        start_date,
        end_date,
        symbol=symbol,
        discrepancy_type=discrepancy_type,
        limit=limit,
        offset=offset,
    )
    rows = payload.get("rows") or []
    counts_by_type = {
        "missing_live": 0,
        "missing_historical": 0,
        "field_mismatch": 0,
    }
    for row in rows:
        key = row.get("discrepancy_type")
        if key in counts_by_type:
            counts_by_type[key] += 1
    payload["counts_by_type"] = counts_by_type
    payload["page_count"] = len(rows)
    return jsonify(payload)


@obs_bp.route("/watchtower/feed-monitoring/export", methods=["GET"])
def watchtower_feed_monitor_export():
    missing = _require_repo()
    if missing:
        return missing
    try:
        start_date, end_date = _feed_monitor_dates_from_request()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    symbol = str(request.args.get("symbol") or "").strip().upper() or None
    discrepancy_type = str(request.args.get("discrepancy_type") or "").strip() or None
    content = repo.feed_monitor_discrepancies_csv(
        start_date=start_date,
        end_date=end_date,
        symbol=symbol,
        discrepancy_type=discrepancy_type,
    )
    filename = f"feed-monitor-{start_date.isoformat()}-{end_date.isoformat()}.csv"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@obs_bp.route("/watchtower/overview", methods=["GET"])
def watchtower_overview():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    portfolio_key_id, chain_run_id = _watchtower_context_args()
    limit = int(request.args.get("limit", "20"))
    return jsonify(repo.watchtower_integrity_overview(
        limit=limit,
        window_start=window_start,
        window_end=window_end,
        portfolio_key_id=portfolio_key_id,
        chain_run_id=chain_run_id,
    ))


@obs_bp.route("/watchtower/order-matching", methods=["GET"])
def watchtower_order_matching():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    limit = int(request.args.get("limit", "200"))
    run_id = request.args.get("run_id")
    return jsonify(repo.watchtower_order_intent_matching(
        window_start=window_start,
        window_end=window_end,
        run_id=run_id,
        limit=limit,
    ))


@obs_bp.route("/watchtower/export-bars", methods=["GET"])
def watchtower_export_bars():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    portfolio_key_id, chain_run_id = _watchtower_context_args()
    symbols = request.args.getlist("symbol")
    if not symbols:
        raw_symbols = str(request.args.get("symbols") or "").strip()
        if raw_symbols:
            symbols = [item.strip().upper() for item in raw_symbols.split(",") if item.strip()]
    try:
        content = repo.export_watchtower_bars_xlsx(
            window_start=window_start,
            window_end=window_end,
            portfolio_key_id=portfolio_key_id,
            chain_run_id=chain_run_id,
            symbols=symbols or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    filename = f"watchtower-bars-{window_start.strftime('%Y%m%dT%H%M%S')}-{window_end.strftime('%Y%m%dT%H%M%S')}.xlsx"
    return Response(
        content,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@obs_bp.route("/watchtower/factsheet", methods=["GET"])
def watchtower_factsheet():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    portfolio_key_id, chain_run_id = _watchtower_context_args()
    return jsonify(repo.watchtower_sampling_factsheet(
        window_start=window_start,
        window_end=window_end,
        portfolio_key_id=portfolio_key_id,
        chain_run_id=chain_run_id,
    ))


@obs_bp.route("/watchtower/coherence-summary", methods=["GET"])
def watchtower_coherence_summary():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    portfolio_key_id, chain_run_id = _watchtower_context_args()
    return jsonify(repo.watchtower_coherence_summary(
        window_start=window_start,
        window_end=window_end,
        portfolio_key_id=portfolio_key_id,
        chain_run_id=chain_run_id,
    ))


@obs_bp.route("/watchdog", methods=["GET"])
def watchdog_checks():
    missing = _require_repo()
    if missing:
        return missing
    invalid, window_start, window_end = _watchtower_window_args()
    if invalid:
        return invalid, 400
    limit = int(request.args.get("limit", "50"))
    return jsonify(repo.latest_stat_checks(limit=limit, window_start=window_start, window_end=window_end))


@obs_bp.route("/watchtower/baselines", methods=["GET"])
def watchtower_baselines():
    missing = _require_repo()
    if missing:
        return missing
    strategy = request.args.get("strategy")
    fingerprint = request.args.get("strategy_fingerprint")
    limit = int(request.args.get("limit", "50"))
    return jsonify(repo.list_baselines(strategy=strategy, fingerprint=fingerprint, limit=limit))


@obs_bp.route("/watchtower/baselines/by-run/<run_id>", methods=["GET"])
def watchtower_baseline_by_run(run_id):
    missing = _require_repo()
    if missing:
        return missing
    payload = repo.baseline_status_for_run(run_id)
    status = 200
    if payload.get("status") == "run_not_found":
        status = 404
    return jsonify(payload), status


@obs_bp.route("/watchtower/baselines/recompute", methods=["POST"])
def watchtower_baseline_recompute():
    missing = _require_repo()
    if missing:
        return missing
    payload = request.get_json(silent=True) or {}
    try:
        job = _start_baseline_job(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@obs_bp.route("/watchtower/baselines/jobs/<job_id>", methods=["GET"])
def watchtower_baseline_job_status(job_id):
    job = _baseline_job_snapshot(job_id)
    if not job:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify(job)


@obs_bp.route("/services", methods=["GET"])
def services():
    return jsonify([_systemctl_show(service) for service in _configured_services()])


@obs_bp.route("/services-config", methods=["GET"])
def services_config():
    return jsonify(
        {
            "services": _configured_services(),
            "config_path": SERVICES_CONFIG_PATH,
            "available": _list_available_services(),
        }
    )


@obs_bp.route("/services-config", methods=["PUT"])
def update_services_config():
    payload = request.get_json(silent=True) or {}
    raw_services = payload.get("services")
    if not isinstance(raw_services, list):
        return jsonify({"error": "services must be a list"}), 400
    services = []
    for item in raw_services:
        service = _normalize_service_name(item)
        if service and service not in services:
            services.append(service)
    _save_services_config(services)
    return jsonify(
        {
            "services": services,
            "config_path": SERVICES_CONFIG_PATH,
        }
    )


@obs_bp.route("/services/<service>/<action>", methods=["POST"])
def service_action(service, action):
    if service not in _configured_services():
        return jsonify({"error": "Unknown service"}), 404
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "Unsupported action"}), 400
    result = _systemctl(action, service)
    payload = {
        "service": service,
        "action": action,
        **result,
    }
    status = 200 if result["returncode"] == 0 else 500
    return jsonify(payload), status


@obs_bp.route("/scheduler", methods=["GET"])
def scheduler_status():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
                "function": f"{job.func.__module__}.{job.func.__name__}",
            }
        )
    return jsonify(
        {
            "running": scheduler.running,
            "state": scheduler.state,
            "jobs": jobs,
        }
    )
