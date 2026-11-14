"""Presence in the admin user list: last activity and the online flag.

Every authenticated request records the account's last activity, at most once
per minute. ``GET /admin/users`` reports the stored timestamp and whether it
falls inside the online window. An account that never made an authenticated
request keeps a null timestamp and is reported offline.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt  # noqa: E402
from src.models import APIToken, User  # noqa: E402
from src.services import presence  # noqa: E402
from src.utils.token_auth import hash_token  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False

# The window the spec defines for "online", independent of the backend constant.
ONLINE_WINDOW = timedelta(minutes=5)


@pytest.fixture
def accounts():
    """Accounts created by the test, removed afterwards."""
    with app.app_context():
        created = []
        yield created
        for user_id in created:
            row = db.session.get(User, user_id)
            if row is not None:
                APIToken.query.filter_by(user_id=row.id).delete()
                db.session.delete(row)
        db.session.commit()


def _user(accounts, prefix, **extra):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f'{prefix[:9]}_{suffix}', email=f'{prefix}_{suffix}@local.test',
                password=bcrypt.generate_password_hash('Passw0rd!').decode(), **extra)
    db.session.add(user)
    db.session.commit()
    accounts.append(user.id)
    return user


def _login(client, user):
    from flask import g
    with client.session_transaction() as sess:
        sess.clear()
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    try:
        g.pop('_login_user', None)
    except RuntimeError:
        pass


def _listed_users(client):
    resp = client.get('/admin/users?per_page=100')
    assert resp.status_code == 200, resp.data
    return {row['id']: row for row in resp.get_json()['users']}


def _stored_last_seen(user):
    """The persisted value, read fresh from the database."""
    db.session.expire_all()
    return db.session.get(User, user.id).last_seen_at


def _api_token(user, scopes=None):
    plain = uuid.uuid4().hex + uuid.uuid4().hex
    db.session.add(APIToken(user_id=user.id, name='presence test', token_hash=hash_token(plain),
                            scopes=scopes))
    db.session.commit()
    return plain


def test_listing_reports_no_activity_for_an_account_that_never_signed_in(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        fresh = _user(accounts, 'fresh')

        client = app.test_client()
        _login(client, admin)
        rows = _listed_users(client)

        assert rows[fresh.id]['last_seen_at'] is None
        assert rows[fresh.id]['online'] is False


def test_an_authenticated_request_records_last_activity(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')

        before = datetime.utcnow()
        member_client = app.test_client()
        _login(member_client, member)
        assert member_client.get('/api/v1/capabilities').status_code == 200

        admin_client = app.test_client()
        _login(admin_client, admin)
        row = _listed_users(admin_client)[member.id]

        assert row['online'] is True
        assert row['last_seen_at'].endswith('Z')
        recorded = datetime.fromisoformat(row['last_seen_at'].removesuffix('Z'))
        assert before - timedelta(seconds=1) <= recorded <= datetime.utcnow() + timedelta(seconds=1)


def test_api_token_usage_counts_as_activity(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')
        token = _api_token(member)

        member_client = app.test_client()
        resp = member_client.get('/api/v1/capabilities', headers={'Authorization': f'Bearer {token}'})
        assert resp.status_code == 200

        admin_client = app.test_client()
        _login(admin_client, admin)
        row = _listed_users(admin_client)[member.id]

        assert row['online'] is True
        assert row['last_seen_at'].endswith('Z')


def test_activity_older_than_the_online_window_is_offline(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')
        stale = datetime.utcnow() - ONLINE_WINDOW - timedelta(minutes=5)
        member.last_seen_at = stale
        db.session.commit()

        client = app.test_client()
        _login(client, admin)
        row = _listed_users(client)[member.id]

        assert row['online'] is False
        assert row['last_seen_at'].endswith('Z')
        assert datetime.fromisoformat(row['last_seen_at'].removesuffix('Z')) == stale


def test_static_assets_are_not_activity(accounts):
    with app.app_context():
        user = _user(accounts, 'reader')

        client = app.test_client()
        _login(client, user)
        assert client.get('/static/css/styles.css').status_code == 200

        assert _stored_last_seen(user) is None


def test_a_request_refused_for_its_scope_still_counts_as_activity(accounts):
    with app.app_context():
        user = _user(accounts, 'scoped')
        token = _api_token(user, scopes='["read"]')

        # A write route with a read-only token: the request is authenticated,
        # then refused before the view runs.
        resp = app.test_client().post(
            '/api/v1/recordings/999999/external-refs',
            headers={'Authorization': f'Bearer {token}'}, json={})
        assert resp.status_code == 403

        assert _stored_last_seen(user) is not None


def test_online_flag_uses_the_five_minute_window(accounts):
    with app.app_context():
        now = datetime.utcnow()
        assert presence.is_online(None) is False
        assert presence.is_online(now - ONLINE_WINDOW + timedelta(seconds=1)) is True
        assert presence.is_online(now - ONLINE_WINDOW - timedelta(seconds=1)) is False


def test_record_activity_writes_once_and_then_skips(accounts):
    with app.app_context():
        user = _user(accounts, 'member')

        assert presence.record_activity(user) is True
        first = _stored_last_seen(user)
        assert first is not None

        assert presence.record_activity(user) is False
        assert _stored_last_seen(user) == first


def test_a_second_request_within_the_throttle_keeps_the_timestamp(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')

        member_client = app.test_client()
        _login(member_client, member)
        assert member_client.get('/api/v1/capabilities').status_code == 200
        first = _stored_last_seen(member)
        assert first is not None

        assert member_client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(member) == first


def test_an_anonymous_request_records_no_activity(accounts):
    with app.app_context():
        user = _user(accounts, 'member')

        resp = app.test_client().get('/admin/users')

        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']
        assert _stored_last_seen(user) is None


def test_a_request_with_an_invalid_token_records_no_activity(accounts):
    with app.app_context():
        user = _user(accounts, 'member')

        resp = app.test_client().get(
            '/admin/users',
            headers={'Authorization': 'Bearer not-a-real-token'})

        assert resp.status_code == 401
        assert _stored_last_seen(user) is None
