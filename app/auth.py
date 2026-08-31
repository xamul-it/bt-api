import os
from datetime import timedelta

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

auth_bp = Blueprint("auth", __name__)

# Exact paths that bypass the auth guard.
_PUBLIC_PATHS = {"/auth/login", "/auth/status", "/github-webhook"}


@auth_bp.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    pw_hash = os.environ["BT_DASH_PW_HASH"]
    if not password or not check_password_hash(pw_hash, password):
        return jsonify(ok=False), 401
    session.permanent = True
    session["authed"] = True
    return jsonify(ok=True)


@auth_bp.post("/auth/logout")
def logout():
    session.clear()
    return "", 204


@auth_bp.get("/auth/status")
def status():
    return jsonify(authed=bool(session.get("authed")))


def init_auth(app):
    """Install session config, register the auth blueprint, add the guard."""
    _ = os.environ["BT_DASH_PW_HASH"]  # required at boot: fail fast, not per-request 500
    app.config.update(
        SECRET_KEY=os.environ["BT_DASH_SECRET_KEY"],  # required: KeyError if unset
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),
        SESSION_REFRESH_EACH_REQUEST=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/",
    )
    app.register_blueprint(auth_bp)

    @app.before_request
    def _require_auth():
        if request.method == "OPTIONS":
            return None
        if request.path in _PUBLIC_PATHS:
            return None
        if not session.get("authed"):
            return jsonify(error="auth required"), 401
        return None


def _main():
    import getpass
    import sys

    if len(sys.argv) != 2 or sys.argv[1] != "hash":
        print("usage: python -m app.auth hash", file=sys.stderr)
        sys.exit(2)
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm : ")
    if pw1 != pw2:
        print("passwords do not match", file=sys.stderr)
        sys.exit(1)
    print(generate_password_hash(pw1, method="pbkdf2:sha256"))


if __name__ == "__main__":
    _main()
