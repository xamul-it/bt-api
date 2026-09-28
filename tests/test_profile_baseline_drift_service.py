from app.service import profile_baseline_drift_service as service


def test_run_checks_continues_after_independent_profile_error(monkeypatch):
    class Repo:
        def list_known_profiles(self):
            return ["broken", "working"]

    def check(_repo, profile, _days):
        if profile == "broken":
            raise RuntimeError("boom")
        return {"profile": profile, "status": "ok"}

    monkeypatch.setattr(service, "_check_profile", check)
    assert service._run_checks(Repo(), 10) == [
        {"profile": "broken", "status": "error", "error": "boom"},
        {"profile": "working", "status": "ok"},
    ]


def test_check_profile_persists_compatible_default(monkeypatch):
    stored = []

    class Repo:
        def list_profile_baselines(self, _profile):
            return [{"id": 7}]

        def record_profile_baseline_drift_check(self, *args):
            stored.append(args)
            return {"id": 11}

    monkeypatch.setattr(service.pbl, "current_baseline_identity", lambda *_args: {})
    monkeypatch.setattr(
        service.pbl, "annotate_baselines_with_compatibility",
        lambda rows, _current: [
            {**rows[0], "compatibility": {"status": "compatible", "is_default": True}},
        ],
    )
    monkeypatch.setattr(
        service.pbl, "compute_baseline_drift",
        lambda *_args, **_kwargs: {"status": "ok", "score": 0.2},
    )
    result = service._check_profile(Repo(), "development", 10)
    assert result == {"profile": "development", "baseline_id": 7, "status": "ok", "check_id": 11}
    assert stored[0][0:4] == ("development", 7, "ok", 0.2)
