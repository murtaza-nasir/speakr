"""Unauthenticated API requests must get a JSON 401, not a 302 to /login.

Regression tests for issue #333: Flask-Login's default unauthorized
behavior redirects everything to the login page. HTTP clients follow the
redirect, receive the login page as 200 text/html, and conclude the call
succeeded — connection checks pass with garbage tokens, and a POST upload
can silently turn into a GET /login with the body dropped.

The handler in src/app.py returns a JSON 401 for /api/ paths and for any
request that presented an API token (whatever the path), while keeping the
login redirect for plain browser page loads.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app


def _client():
    return app.test_client()


def test_api_v1_with_invalid_bearer_token_gets_json_401():
    r = _client().get(
        "/api/v1/tags",
        headers={"Authorization": "Bearer definitely-not-a-real-token"},
    )
    assert r.status_code == 401
    assert r.is_json
    assert "error" in r.get_json()
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_api_v1_with_no_credentials_gets_json_401():
    r = _client().get("/api/v1/tags")
    assert r.status_code == 401
    assert r.is_json
    assert "error" in r.get_json()


def test_spa_api_path_unauthenticated_gets_json_401():
    """Session-backed /api/ endpoints also 401 as JSON: the SPA's fetch
    calls previously followed the redirect and choked on login-page HTML."""
    r = _client().get("/api/tags")
    assert r.status_code == 401
    assert r.is_json


def test_non_api_path_with_token_gets_json_401():
    """A token client hitting a non-/api endpoint (e.g. /speakers) is still
    an API caller — redirecting it to the login page would produce the same
    false-success behavior from the issue. (Unsafe methods with an invalid
    token are rejected even earlier, by the CSRF gate, with a 400 — also a
    real error status, so no silent-success path exists there either.)"""
    r = _client().get(
        "/speakers",
        headers={"X-API-Token": "not-a-real-token"},
    )
    assert r.status_code == 401
    assert r.is_json


def test_browser_page_load_still_redirects_to_login():
    r = _client().get("/")
    assert r.status_code == 302
    assert "/login" in r.headers.get("Location", "")
