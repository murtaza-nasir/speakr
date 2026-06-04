"""Regression tests for /api/v1/users/me (issue #281).

Companion apps need a way to display the current user's identity without
scraping a private endpoint. This endpoint returns a stable subset of the
profile and preferences fields plus group memberships.
"""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User
from src.models.organization import Group, GroupMembership

app.config["WTF_CSRF_ENABLED"] = False


def _setup_user(username_prefix, **overrides):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{username_prefix}_{suffix}",
        email=f"{username_prefix}_{suffix}@local.test",
        password="x",
        **overrides,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_users_me_returns_authenticated_user():
    with app.app_context():
        user = _setup_user(
            "me_basic",
            name="Test User",
            is_admin=True,
            email_verified=True,
            ui_language="fr",
            extract_events=True,
        )
        client = app.test_client()
        _login(client, user)
        resp = client.get("/api/v1/users/me")
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["id"] == user.id
        assert body["username"] == user.username
        assert body["email"] == user.email
        assert body["name"] == "Test User"
        assert body["is_admin"] is True
        assert body["email_verified"] is True
        assert body["preferences"]["ui_language"] == "fr"
        assert body["preferences"]["extract_events"] is True
        assert body["group_memberships"] == []
        db.session.delete(user)
        db.session.commit()


def test_users_me_includes_group_memberships():
    with app.app_context():
        user = _setup_user("me_group")
        group = Group(name=f"group_{uuid.uuid4().hex[:8]}", description="t")
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMembership(group_id=group.id, user_id=user.id, role="admin"))
        db.session.commit()
        client = app.test_client()
        _login(client, user)
        resp = client.get("/api/v1/users/me")
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["group_memberships"]) == 1
        membership = body["group_memberships"][0]
        assert membership["group_id"] == group.id
        assert membership["group_name"] == group.name
        assert membership["role"] == "admin"
        db.session.delete(group)
        db.session.delete(user)
        db.session.commit()


def test_users_me_requires_auth():
    with app.app_context():
        client = app.test_client()
        resp = client.get("/api/v1/users/me", follow_redirects=False)
        # @login_required redirects to login page when unauthenticated
        assert resp.status_code in (302, 401), resp.status_code


def test_openapi_documents_users_me():
    with app.app_context():
        client = app.test_client()
        resp = client.get("/api/v1/openapi.json")
        assert resp.status_code == 200
        schema = resp.get_json()
        assert "/users/me" in schema["paths"]
        path = schema["paths"]["/users/me"]
        assert "get" in path
        assert "Users" in path["get"]["tags"]


def teardown_module(module):
    with app.app_context():
        for u in User.query.filter(User.username.like("me_%")).all():
            db.session.delete(u)
        for g in Group.query.filter(Group.name.like("group_%")).all():
            db.session.delete(g)
        db.session.commit()


if __name__ == "__main__":
    test_users_me_returns_authenticated_user()
    test_users_me_includes_group_memberships()
    test_users_me_requires_auth()
    test_openapi_documents_users_me()
    print("All /api/v1/users/me tests passed.")
