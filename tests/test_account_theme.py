"""Light/dark mode and color scheme saved with the account (D#419).

The theme was kept only in the browser's localStorage. It is now also saved
with the account when the user changes it, and a saved value replaces the
browser's copy on every page the user opens. Accounts that have not changed
it since the upgrade have no saved value, and each browser keeps its own.

SHARED-DB: the test users are removed afterwards.
"""

import json
import os
import re
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def user():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"theme_{s}", email=f"theme_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return client


def _saved_theme(html):
    m = re.search(r"var saved = (\{.*?\});", html)
    return json.loads(m.group(1)) if m else None


def test_nothing_is_saved_until_the_user_changes_the_theme(user):
    page = _client(user).get("/").get_data(as_text=True)
    assert _saved_theme(page) == {"dark": None, "scheme": None}
    assert "window.speakrSaveTheme" in page


def test_a_saved_theme_is_written_to_every_page(user):
    client = _client(user)
    resp = client.post("/api/user/preferences", json={"dark_mode": True, "color_scheme": "teal"})
    assert resp.status_code == 200
    for path in ("/", "/account"):
        page = client.get(path).get_data(as_text=True)
        assert _saved_theme(page) == {"dark": True, "scheme": "teal"}, path
    # The account value is applied before the page reads localStorage.
    page = client.get("/").get_data(as_text=True)
    assert page.index("var saved =") < page.index("localStorage.getItem('darkMode')")


def test_one_setting_can_be_saved_alone(user):
    client = _client(user)
    assert client.post("/api/user/preferences", json={"color_scheme": "rose"}).status_code == 200
    with app.app_context():
        u = db.session.get(User, user)
        assert (u.theme_dark_mode, u.theme_color_scheme) == (None, "rose")


@pytest.mark.parametrize("body", [{"dark_mode": "yes"}, {"color_scheme": "red"}, {"color_scheme": "<x>"}])
def test_invalid_values_are_refused(user, body):
    client = _client(user)
    assert client.post("/api/user/preferences", json=body).status_code == 400
    with app.app_context():
        u = db.session.get(User, user)
        assert (u.theme_dark_mode, u.theme_color_scheme) == (None, None)


def test_signed_out_pages_have_no_account_theme():
    page = app.test_client().get("/login").get_data(as_text=True)
    assert "speakrSaveTheme" not in page
