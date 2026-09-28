from flask import Flask
import pytest


def test_baseline_list_marks_newest_compatible_default(monkeypatch):
    import app.watchtower as watchtower

    class Repo:
        def available(self):
            return True

        def list_profile_baselines(self, profile):
            assert profile == "development"
            return [
                {"id": 2, "provenance_version": 1, "created_at": "2026-09-02"},
                {"id": 1, "provenance_version": 1, "created_at": "2026-09-01"},
            ]

    monkeypatch.setattr(watchtower, "repo", Repo())
    monkeypatch.setattr(watchtower._pbl, "current_baseline_identity", lambda *_args: {"identity": "current"})
    monkeypatch.setattr(
        watchtower._pbl,
        "annotate_baselines_with_compatibility",
        lambda rows, _current: [
            {**rows[0], "compatibility": {"status": "compatible", "is_default": True, "differences": []}},
            {**rows[1], "compatibility": {"status": "compatible", "is_default": False, "differences": []}},
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(watchtower.obs_bp, url_prefix="/dyn/obs")
    response = app.test_client().get("/dyn/obs/watchtower/cron/development/baselines")

    assert response.status_code == 200
    rows = response.get_json()
    assert rows[0]["compatibility"]["is_default"] is True


def test_baseline_list_preserves_rows_when_current_context_is_unavailable(monkeypatch):
    import app.watchtower as watchtower

    class Repo:
        def available(self):
            return True

        def list_profile_baselines(self, _profile):
            return [{"id": 7, "provenance_version": 1}]

    monkeypatch.setattr(watchtower, "repo", Repo())
    monkeypatch.setattr(watchtower._pbl, "current_baseline_identity", lambda *_args: (_ for _ in ()).throw(ValueError("missing")))

    app = Flask(__name__)
    app.register_blueprint(watchtower.obs_bp, url_prefix="/dyn/obs")
    response = app.test_client().get("/dyn/obs/watchtower/cron/development/baselines")

    assert response.status_code == 200
    assert response.get_json()[0]["compatibility"]["status"] == "unknown"


def test_baseline_creation_rejects_historical_as_of():
    import app.watchtower as watchtower

    with pytest.raises(ValueError, match="as_of_date is no longer supported"):
        watchtower._start_profile_baseline_job("development", {
            "label": "old", "window_start": "2020-01-01", "window_end": "2021-01-01",
            "as_of_date": "2020-01-01",
        })


def test_overview_labels_but_keeps_drift_from_non_current_baseline(monkeypatch):
    import app.watchtower as watchtower

    class Repo:
        def available(self):
            return True

        def profile_cockpit(self, _profile):
            return {"latest_profile_baseline_drift_check": {"id": 10, "baseline_id": 1, "status": "ok"}}

        def list_profile_baselines(self, _profile):
            return [{"id": 2}]

    monkeypatch.setattr(watchtower, "repo", Repo())
    monkeypatch.setattr(watchtower._pbl, "current_baseline_identity", lambda *_args: {})
    monkeypatch.setattr(
        watchtower._pbl, "annotate_baselines_with_compatibility",
        lambda rows, _current: [{**rows[0], "compatibility": {"status": "compatible", "is_default": True}}],
    )
    app = Flask(__name__)
    app.register_blueprint(watchtower.obs_bp, url_prefix="/dyn/obs")
    payload = app.test_client().get("/dyn/obs/watchtower/cron/development/overview").get_json()
    assert payload["latest_profile_baseline_drift_check"]["baseline_id"] == 1
    assert payload["current_profile_baseline_drift_check"] is None
    assert payload["last_profile_baseline_drift_check"]["baseline_id"] == 1
    assert payload["profile_baseline_drift_state"] == {
        "status": "not_checked_current_baseline", "baseline_id": 2, "last_check_baseline_id": 1,
    }


def test_overview_keeps_drift_for_current_baseline(monkeypatch):
    import app.watchtower as watchtower

    check = {"id": 10, "baseline_id": 2, "status": "ok"}

    class Repo:
        def available(self):
            return True

        def profile_cockpit(self, _profile):
            return {"latest_profile_baseline_drift_check": check}

        def list_profile_baselines(self, _profile):
            return [{"id": 2}]

    monkeypatch.setattr(watchtower, "repo", Repo())
    monkeypatch.setattr(watchtower._pbl, "current_baseline_identity", lambda *_args: {})
    monkeypatch.setattr(
        watchtower._pbl, "annotate_baselines_with_compatibility",
        lambda rows, _current: [{**rows[0], "compatibility": {"status": "compatible", "is_default": True}}],
    )
    app = Flask(__name__)
    app.register_blueprint(watchtower.obs_bp, url_prefix="/dyn/obs")
    payload = app.test_client().get("/dyn/obs/watchtower/cron/development/overview").get_json()
    assert payload["latest_profile_baseline_drift_check"] == check
    assert payload["current_profile_baseline_drift_check"] == check
    assert payload["profile_baseline_drift_state"]["status"] == "current"
