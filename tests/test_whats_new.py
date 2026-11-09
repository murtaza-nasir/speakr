"""What's new after an update: shown once per account (show_whats_new).

The notes are whatsNew.releases.<release> in the locale files; the server
reports the release, what the user has seen, and whether the admin has turned
the dialog off. Accounts that existed before the column have no value and see
the notes once; new accounts start as having seen the current release.

SHARED-DB: the test users are removed afterwards.
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.config.version import release_key
from src.models import SystemSetting, User

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def user():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"wn_{s}", email=f"wn_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


@pytest.fixture
def setting_restored():
    with app.app_context():
        old = SystemSetting.get_setting("show_whats_new", True)
    yield
    with app.app_context():
        SystemSetting.set_setting("show_whats_new", "true" if old else "false", setting_type="boolean")


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return client


@pytest.mark.parametrize("version, key", [
    ("v0.10.20-alpha", "0_10_20"), ("0.10.20", "0_10_20"), ("v1.2.3", "1_2_3"),
    ("v0.11.0-beta.2", "0_11_0"), ("unknown", None), ("", None),
])
def test_release_key(version, key):
    assert release_key(version) == key


def test_without_an_argument_the_key_is_the_running_version():
    from src.config.version import get_version
    assert release_key() == release_key(get_version())


def test_a_new_account_starts_as_having_seen_the_current_release(user):
    with app.app_context():
        assert db.session.get(User, user).whats_new_seen == release_key()


def test_the_config_reports_the_release_and_what_was_seen(user):
    with app.app_context():
        u = db.session.get(User, user)
        u.whats_new_seen = None  # an account from before this release
        db.session.commit()
    data = _client(user).get("/api/config").get_json()["whats_new"]
    assert data == {"enabled": True, "release": release_key(), "seen": None,
                    "version": data["version"]}
    assert data["version"]


def test_marking_it_seen_stores_the_current_release(user):
    with app.app_context():
        u = db.session.get(User, user)
        u.whats_new_seen = None
        db.session.commit()
    client = _client(user)
    resp = client.post("/api/user/whats-new/seen")
    assert resp.status_code == 200 and resp.get_json()["seen"] == release_key()
    assert client.get("/api/config").get_json()["whats_new"]["seen"] == release_key()


def test_the_admin_can_turn_it_off(user, setting_restored):
    with app.app_context():
        SystemSetting.set_setting("show_whats_new", "false", setting_type="boolean")
    assert _client(user).get("/api/config").get_json()["whats_new"]["enabled"] is False


def test_signed_out_config_has_no_whats_new():
    assert "whats_new" not in app.test_client().get("/api/config").get_json()


def test_the_setting_is_on_by_default():
    with app.app_context():
        assert SystemSetting.get_setting("show_whats_new", None) is True
