from flask import Flask


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
