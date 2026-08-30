import os

from werkzeug.security import generate_password_hash

# Must be set BEFORE app.auth is imported / init_auth is called.
os.environ.setdefault("BT_DASH_SECRET_KEY", "test-secret-key-not-for-prod")
os.environ.setdefault(
    "BT_DASH_PW_HASH", generate_password_hash("testpw", method="pbkdf2:sha256")
)

import pytest
from flask import Flask, jsonify

from app.auth import init_auth


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_auth(app)
    # test client speaks http:// -> don't let Secure suppress the cookie
    app.config["SESSION_COOKIE_SECURE"] = False

    @app.get("/dyn/protected")
    def _protected():
        return jsonify(ok=True)

    return app


@pytest.fixture
def client(app):
    return app.test_client()
