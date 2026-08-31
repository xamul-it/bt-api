import pytest


def test_status_unauthed(client):
    r = client.get("/auth/status")
    assert r.status_code == 200
    assert r.get_json() == {"authed": False}


def test_protected_requires_auth(client):
    r = client.get("/dyn/protected")
    assert r.status_code == 401
    assert r.get_json() == {"error": "auth required"}


def test_login_wrong_password(client):
    r = client.post("/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert r.get_json() == {"ok": False}


def test_login_missing_body(client):
    r = client.post("/auth/login")
    assert r.status_code == 401


def test_login_then_access_protected(client):
    r = client.post("/auth/login", json={"password": "testpw"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}

    r = client.get("/dyn/protected")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}

    r = client.get("/auth/status")
    assert r.get_json() == {"authed": True}


def test_logout_clears_session(client):
    client.post("/auth/login", json={"password": "testpw"})
    r = client.post("/auth/logout")
    assert r.status_code == 204

    r = client.get("/dyn/protected")
    assert r.status_code == 401


def test_options_not_blocked(client):
    r = client.open("/dyn/protected", method="OPTIONS")
    assert r.status_code != 401


def test_allowlisted_path_not_forced_to_401(client):
    # /github-webhook is not registered on this bare test app, so it 404s;
    # the guard must NOT turn it into a 401.
    r = client.post("/github-webhook")
    assert r.status_code != 401


def test_init_auth_requires_pw_hash(monkeypatch):
    """Verify init_auth fails fast if BT_DASH_PW_HASH is unset."""
    import os
    from flask import Flask
    from app.auth import init_auth

    # Save the current hash
    pw_hash = os.environ.pop("BT_DASH_PW_HASH")
    try:
        app = Flask(__name__)
        with pytest.raises(KeyError):
            init_auth(app)
    finally:
        # Restore the hash so other tests are unaffected
        os.environ["BT_DASH_PW_HASH"] = pw_hash
