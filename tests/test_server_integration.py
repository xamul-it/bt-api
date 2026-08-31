import pytest

# conftest.py has already set BT_DASH_SECRET_KEY / BT_DASH_PW_HASH.
try:
    import server
except Exception as exc:  # pragma: no cover - env/data not present on this box
    pytest.skip(f"cannot import server: {exc}", allow_module_level=True)


@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    server.app.config["SESSION_COOKIE_SECURE"] = False
    return server.app.test_client()


def test_dyn_route_blocked_without_session(client):
    r = client.get("/dyn/sc/index")
    assert r.status_code == 401
    assert r.get_json() == {"error": "auth required"}


def test_dyn_route_reachable_after_login(client):
    r = client.post("/auth/login", json={"password": "testpw"})
    assert r.status_code == 200
    r = client.get("/dyn/sc/index")
    assert r.status_code != 401


def test_github_webhook_not_blocked_by_guard(client):
    # No signature -> handler itself rejects (403) or 503 if unconfigured,
    # but NOT 401 from the guard.
    r = client.post("/github-webhook", data=b"{}")
    assert r.status_code != 401
