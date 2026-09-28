"""Hourly, dashboard-managed profile baseline drift monitoring."""

import logging

import profile_baseline as pbl
import watchtower_runtime as wr


logger = logging.getLogger(__name__)
ADVISORY_LOCK_ID = 0x42544452494654  # stable integer: "BTDRIFT"


def _check_profile(repo, profile, recent_window_days):
    current = pbl.current_baseline_identity(repo, profile)
    baselines = pbl.annotate_baselines_with_compatibility(
        repo.list_profile_baselines(profile), current,
    )
    baseline = next(
        (row for row in baselines if row.get("compatibility", {}).get("is_default")),
        None,
    )
    if baseline is None:
        verdict = {"status": "no_compatible_baseline", "reason": "no_compatible_baseline"}
        stored = repo.record_profile_baseline_drift_check(
            profile, None, verdict["status"], None, verdict,
        )
        return {
            "profile": profile, "baseline_id": None,
            "status": verdict["status"], "check_id": stored["id"],
        }

    verdict = pbl.compute_baseline_drift(
        repo, baseline, recent_window_days=recent_window_days,
    )
    verdict["compatibility"] = baseline["compatibility"]
    stored = repo.record_profile_baseline_drift_check(
        profile, baseline["id"], verdict["status"], verdict.get("score"), verdict,
    )
    return {
        "profile": profile,
        "baseline_id": baseline["id"],
        "status": verdict["status"],
        "check_id": stored["id"],
    }


def _run_checks(repo, recent_window_days):
    results = []
    for profile in repo.list_known_profiles():
        try:
            results.append(_check_profile(repo, profile, recent_window_days))
        except Exception as exc:  # profiles are independent
            logger.exception("Profile baseline drift failed for %s", profile)
            verdict = {"status": "error", "error": str(exc)}
            try:
                stored = repo.record_profile_baseline_drift_check(
                    profile, None, "error", None, verdict,
                )
                verdict["check_id"] = stored["id"]
            except Exception:
                logger.exception("Could not persist baseline drift failure for %s", profile)
            results.append({"profile": profile, **verdict})
    return results


def run_profile_baseline_drift(recent_window_days=pbl.RECENT_WINDOW_DEFAULT):
    """Run once per API deployment even if multiple scheduler workers fire."""
    repo = wr.WatchtowerRepository()
    if not repo.available():
        logger.error("Profile baseline drift skipped: Postgres DSN missing")
        return [{"status": "error", "error": "postgres_dsn_missing"}]

    with repo.connect() as lock_connection:
        with lock_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            logger.info("Profile baseline drift skipped: another instance owns the lock")
            return [{"status": "skipped", "reason": "already_running"}]
        try:
            results = _run_checks(repo, int(recent_window_days))
            pruned = repo.prune_profile_baseline_drift_checks()
            logger.info("Profile baseline drift history pruned: %s rows", pruned)
            logger.info("Profile baseline drift completed: %s", results)
            return results
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
