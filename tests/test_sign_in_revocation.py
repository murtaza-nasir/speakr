"""Remember-me cookie and sign-ins after a password change (#437).

The remember-me cookie had Flask-Login's defaults (365 days, no Secure flag,
no SameSite), and neither it nor the session was ended when the password
changed. A password change, a reset by email or a password set by an
administrator now signs the account out of every other browser. Sign-ins made
before the change keep working until the password is next set.
"""

import uuid
from datetime import timedelta

import pytest

from src.app import app, bcrypt, db, load_user
from src.models import User

app.config["WTF_CSRF_ENABLED"] = False

OLD = "Old-passw0rd!x"
NEW = "New-passw0rd!y"


@pytest.fixture
def user_id():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        u = User(username=f"rev_{suffix}", email=f"rev_{suffix}@local.test",
                 password=bcrypt.generate_password_hash(OLD).decode("utf-8"))
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        u = db.session.get(User, uid)
        if u:
            db.session.delete(u)
            db.session.commit()


def _client(login_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = login_id
        sess["_fresh"] = True
    return client


def _signed_in(client):
    return client.get("/account").status_code == 200


def test_cookie_settings():
    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"
    assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    assert app.config["REMEMBER_COOKIE_SECURE"] == app.config["SESSION_COOKIE_SECURE"]
    assert app.config["REMEMBER_COOKIE_DURATION"] == timedelta(days=30)
    assert app.config["REMEMBER_COOKIE_REFRESH_EACH_REQUEST"] is True


def test_existing_plain_id_sign_in_stays_valid(user_id):
    with app.app_context():
        assert db.session.get(User, user_id).get_id() == str(user_id)
        assert load_user(str(user_id)).id == user_id
    assert _signed_in(_client(str(user_id)))


def test_password_change_signs_out_other_browsers(user_id):
    this, other = _client(str(user_id)), _client(str(user_id))
    resp = this.post("/change_password", data={
        "current_password": OLD, "new_password": NEW, "confirm_password": NEW})
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(User, user_id).session_version == 1
    assert _signed_in(this)
    assert not _signed_in(other)


def test_admin_setting_the_password_signs_the_user_out(user_id):
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        admin = User(username=f"reva_{suffix}", email=f"reva_{suffix}@local.test",
                     password="x", is_admin=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    victim = _client(str(user_id))
    resp = _client(str(admin_id)).put(f"/admin/users/{user_id}", json={"password": NEW})
    assert resp.status_code == 200, resp.data
    assert not _signed_in(victim)
    with app.app_context():
        db.session.delete(db.session.get(User, admin_id))
        db.session.commit()


@pytest.mark.parametrize("login_id", ["abc", "1:x", ""])
def test_malformed_login_ids_are_refused(login_id):
    with app.app_context():
        assert load_user(login_id) is None


def test_older_version_is_refused(user_id):
    with app.app_context():
        u = db.session.get(User, user_id)
        u.end_all_sessions()
        u.end_all_sessions()
        db.session.commit()
        assert load_user(f"{user_id}:1") is None
        assert load_user(str(user_id)) is None
        assert load_user(f"{user_id}:2").id == user_id
