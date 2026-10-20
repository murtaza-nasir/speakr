"""Usernames set by an administrator are 2 to 20 characters.

The username column holds 20 characters. Sign-up and SSO already keep to
that; the admin endpoints did not, and on PostgreSQL a longer name gave a
500 error.
"""

import uuid

from src.app import app, db
from src.models import User

app.config["WTF_CSRF_ENABLED"] = False


def _admin_client():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        admin = User(username=f"ulen_{s}", email=f"ulen_{s}@local.test", password="x", is_admin=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)
        sess["_fresh"] = True
    return client, admin_id


def _cleanup(*ids):
    with app.app_context():
        for uid in ids:
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
        db.session.commit()


def test_create_refuses_a_long_username():
    client, admin_id = _admin_client()
    resp = client.post("/admin/users", json={"username": "u" * 21, "email": f"{uuid.uuid4().hex[:8]}@local.test",
                                             "password": "Passw0rd!long"})
    assert resp.status_code == 400
    assert "20 characters" in resp.get_json()["error"]
    _cleanup(admin_id)


def test_update_refuses_a_long_username():
    client, admin_id = _admin_client()
    resp = client.put(f"/admin/users/{admin_id}", json={"username": "u" * 21})
    assert resp.status_code == 400
    _cleanup(admin_id)


def test_twenty_characters_are_accepted():
    client, admin_id = _admin_client()
    name = uuid.uuid4().hex[:20]
    resp = client.post("/admin/users", json={"username": name, "email": f"{name[:8]}@local.test",
                                             "password": "Passw0rd!long"})
    assert resp.status_code in (200, 201), resp.data
    with app.app_context():
        created = User.query.filter_by(username=name).first().id
    _cleanup(created, admin_id)
