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


def _recorder_files():
    """RECORDER_FILES of static/sw.js: the offline recorder and what it loads (#650)."""
    import re
    source = _worker_source().decode("utf-8")
    block = source[source.index("const RECORDER_FILES = ["):]
    block = block[:block.index("];")]
    names = dict(re.findall(r"const (RECORDER_PAGE|RECORDER_SCRIPT) = '([^']+)';", source))
    files = []
    for path, name in re.findall(r"'(/static/[^']+)'|\b(RECORDER_PAGE|RECORDER_SCRIPT)\b", block):
        files.append(path or names[name])
    return files


def test_the_offline_recorder_files_are_static_files_served_without_a_session():
    files = _recorder_files()
    assert "/static/offline-recorder.html" in files and "/static/js/offline-recorder.js" in files
    # The capture of the three sources, shared with the in-app recorder (#697).
    assert "/static/js/modules/utils/audio-capture.js" in files
    assert len(files) == len(set(files)) >= 10
    client = app.test_client()
    for path in files:
        response = client.get(f"{path}?v=1.2.3")
        assert response.status_code == 200, path
        assert "no-store" not in response.headers.get("Cache-Control", ""), path
        assert response.headers.get("Set-Cookie") is None, path
        response.close()


def test_the_offline_recorder_page_has_no_user_data_and_no_template():
    """It is a static file: no Jinja, no session, the same bytes for everyone."""
    client = app.test_client()
    response = client.get("/static/offline-recorder.html")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    response.close()
    for marker in ("{{", "{%", "csrf", "current_user", "/api/"):
        assert marker not in html, marker
    assert "__SPEAKR_VERSION__" in html


def test_the_offline_recorder_page_is_the_same_signed_in(signed_in):
    anonymous = app.test_client().get("/static/offline-recorder.html")
    mine = signed_in.get("/static/offline-recorder.html")
    assert mine.status_code == 200
    assert mine.get_data() == anonymous.get_data()
    anonymous.close()
    mine.close()


def test_the_app_tells_the_offline_recorder_how_long_its_note_lasts(signed_in):
    """REMEMBER_COOKIE_DAYS reaches the page as data-remember-cookie-days, beside
    data-recording-max-hours, for the expiry and the limits of the offline
    recorder's entry (#650)."""
    response = signed_in.get("/")
    html = response.get_data(as_text=True)
    response.close()
    days = app.config["REMEMBER_COOKIE_DURATION"].days
    assert f'data-remember-cookie-days="{days}"' in html
    assert 'data-recording-max-hours="' in html
