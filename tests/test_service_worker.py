"""The service worker's URL and scope, and the mark the server leaves at sign-out.

static/sw.js is served at /sw.js, so that its scope is the whole app; from
/static/sw.js it controlled no page. What the worker does with a request is
tested in static/js/sw.test.js, the page's side in
static/js/service-worker-client.test.js, and both in a browser in
tests/e2e/test_service_worker_ui.py.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _worker_source():
    with open(os.path.join(ROOT, "static", "sw.js"), "rb") as f:
        return f.read()


@pytest.fixture
def signed_in():
    previous = app.config.get("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_ENABLED"] = True
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"sw_{s}", email=f"sw_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield client
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()
    app.config["WTF_CSRF_ENABLED"] = previous


@pytest.mark.parametrize("path", ["/sw.js", "/sw.js?v=1.2.3"])
def test_the_worker_is_served_from_the_root_without_a_session(path):
    response = app.test_client().get(path)
    assert response.status_code == 200
    assert response.data == _worker_source()
    assert response.mimetype == "text/javascript"
    # The browser's update check must see a new release at once.
    assert response.headers["Cache-Control"] == "no-cache"
    assert "ETag" in response.headers
    # A file at the root needs no wider scope than its own directory.
    assert "Service-Worker-Allowed" not in response.headers


def test_the_worker_url_sets_no_session_cookie():
    response = app.test_client().get("/sw.js")
    assert "Set-Cookie" not in response.headers


def test_the_worker_is_revalidated_with_its_etag():
    client = app.test_client()
    etag = client.get("/sw.js").headers["ETag"]
    assert client.get("/sw.js", headers={"If-None-Match": etag}).status_code == 304


def _signed_out_cookie(response):
    cookies = [c for c in response.headers.getlist("Set-Cookie") if c.startswith("speakr_signed_out=")]
    assert len(cookies) == 1, response.headers.getlist("Set-Cookie")
    return cookies[0]


def test_sign_out_marks_the_browser_for_the_page_that_follows(signed_in):
    token = signed_in.get("/api/csrf-token").get_json()["csrf_token"]
    response = signed_in.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    cookie = _signed_out_cookie(response)
    parts = [p.strip() for p in cookie.split(";")]
    assert parts[0] == "speakr_signed_out=1"
    assert "Path=/" in parts and "SameSite=Lax" in parts
    # Read by the page's script, so not HttpOnly; no lifetime beyond the browser session.
    assert "HttpOnly" not in parts
    assert not any(p.lower().startswith(("max-age", "expires")) for p in parts)
    assert "Secure" not in parts
    # The session has ended.
    assert signed_in.get("/account").status_code in (302, 401)


def test_the_mark_is_secure_over_https(signed_in):
    token = signed_in.get("/api/csrf-token", base_url="https://localhost").get_json()["csrf_token"]
    response = signed_in.post("/logout", data={"csrf_token": token}, base_url="https://localhost",
                              headers={"Referer": "https://localhost/"})
    assert response.status_code == 302
    assert "Secure" in [p.strip() for p in _signed_out_cookie(response).split(";")]


def test_sign_out_with_an_expired_token_from_the_apps_own_page_leaves_the_mark_too(signed_in):
    response = signed_in.post("/logout", data={}, headers={"Origin": "http://localhost", "Accept": "text/html"},
                              content_type="application/x-www-form-urlencoded")
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    _signed_out_cookie(response)


def test_a_refused_sign_out_leaves_no_mark(signed_in):
    """A cross-site form signs nobody out and must not make the browser clear its data."""
    response = signed_in.post("/logout", data={}, headers={"Origin": "https://evil.example", "Accept": "text/html"},
                              content_type="application/x-www-form-urlencoded")
    assert response.status_code == 400
    assert not [c for c in response.headers.getlist("Set-Cookie") if c.startswith("speakr_signed_out=")]
    assert signed_in.get("/account").status_code == 200


def test_signing_in_or_opening_a_page_leaves_no_mark(signed_in):
    for path in ("/", "/account", "/login"):
        response = signed_in.get(path)
        assert not [c for c in response.headers.getlist("Set-Cookie") if c.startswith("speakr_signed_out=")], path


def test_static_files_are_not_marked_private_or_no_store(signed_in):
    """The worker stores /static/ responses only; they must stay storable and
    carry nothing of the account."""
    response = signed_in.get("/static/js/i18n.js?v=1")
    assert response.status_code == 200
    control = response.headers.get("Cache-Control", "")
    assert "no-store" not in control and "private" not in control
