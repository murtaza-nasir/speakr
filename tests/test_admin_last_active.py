"""The "Last active" time in the admin user list.

A request of a signed-in account stores the time of its last use, at most
once per five minutes, unless the request is one that an open tab sends on a
timer. ``GET /admin/users`` reports the stored time to site admins and can
sort by it. The write is made outside the session of the request, waits a
fraction of a second for a lock at most, and its errors are swallowed.
"""

import contextlib
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt  # noqa: E402
from src.models import APIToken, Group, GroupMembership, User  # noqa: E402
from src.services import last_active  # noqa: E402
from src.utils.token_auth import hash_token  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A request that is slower than this was held up by the lock. The write waits
# LOCK_WAIT_SECONDS (0.25); the application's own SQLite timeout is 30 seconds.
SLOW_SECONDS = 3.0


@pytest.fixture(autouse=True)
def _forget_failures():
    """The retry interval after a failed write must not carry over between tests."""
    last_active._retry_after.clear()
    yield
    last_active._retry_after.clear()


@pytest.fixture
def accounts():
    """Accounts created by the test, removed afterwards."""
    with app.app_context():
        created = []
        yield created
        db.session.rollback()
        for user_id in created:
            row = db.session.get(User, user_id)
            if row is not None:
                APIToken.query.filter_by(user_id=row.id).delete()
                GroupMembership.query.filter_by(user_id=row.id).delete()
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


def _client_for(user):
    client = app.test_client()
    _login(client, user)
    return client


def _listed_users(client, query='per_page=1000'):
    resp = client.get(f'/admin/users?{query}')
    assert resp.status_code == 200, resp.data
    return resp.get_json()['users']


def _listed_by_id(client):
    return {row['id']: row for row in _listed_users(client)}


def _stored_last_seen(user):
    """The persisted value, read fresh from the database."""
    db.session.rollback()
    db.session.expire_all()
    return db.session.get(User, user.id).last_seen_at


def _set_last_seen(user, value):
    user.last_seen_at = value
    db.session.commit()


def _api_token(user, scopes=None):
    plain = uuid.uuid4().hex + uuid.uuid4().hex
    db.session.add(APIToken(user_id=user.id, name='last active test', token_hash=hash_token(plain),
                            scopes=scopes))
    db.session.commit()
    return plain


@contextlib.contextmanager
def _database_locked_by_another_writer(user):
    """Hold the lock that the last-active write needs, on another connection.

    SQLite has one writer: BEGIN IMMEDIATE takes its place. On PostgreSQL the
    row of the account is locked.
    """
    db.session.rollback()
    if db.engine.dialect.name == 'sqlite':
        conn = sqlite3.connect(db.engine.url.database, timeout=1, isolation_level=None)
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield
        finally:
            conn.execute('ROLLBACK')
            conn.close()
    else:
        conn = db.engine.connect()
        trans = conn.begin()
        conn.execute(text('SELECT id FROM "user" WHERE id = :id FOR UPDATE'), {'id': user.id})
        try:
            yield
        finally:
            trans.rollback()
            conn.close()


# --- What the listing reports ------------------------------------------------

def test_listing_reports_no_time_for_an_account_that_was_never_used(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        fresh = _user(accounts, 'fresh')

        row = _listed_by_id(_client_for(admin))[fresh.id]

        assert row['last_seen_at'] is None
        # No online state is derived from the time.
        assert 'online' not in row


def test_an_authenticated_request_records_the_time(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')

        before = datetime.utcnow()
        assert _client_for(member).get('/api/v1/capabilities').status_code == 200

        row = _listed_by_id(_client_for(admin))[member.id]

        assert row['last_seen_at'].endswith('Z')
        recorded = datetime.fromisoformat(row['last_seen_at'].removesuffix('Z'))
        assert before - timedelta(seconds=1) <= recorded <= datetime.utcnow() + timedelta(seconds=1)


def test_the_admin_sees_the_time_written_by_the_listing_request_itself(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        admin_id = admin.id
        before = datetime.utcnow()
        client = _client_for(admin)
        # As in a served request: the row is loaded anew, not taken from here.
        db.session.remove()

        row = {r['id']: r for r in _listed_users(client)}[admin_id]

        assert row['last_seen_at'] is not None
        recorded = datetime.fromisoformat(row['last_seen_at'].removesuffix('Z'))
        assert recorded >= before - timedelta(seconds=1)
        # The session of the request was not made dirty by it.
        assert not db.session.dirty


def test_api_token_usage_counts_for_the_owner_of_the_token(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')
        token = _api_token(member)

        resp = app.test_client().get('/api/v1/capabilities', headers={'Authorization': f'Bearer {token}'})
        assert resp.status_code == 200

        row = _listed_by_id(_client_for(admin))[member.id]
        assert row['last_seen_at'] and row['last_seen_at'].endswith('Z')


def test_a_stored_time_is_reported_as_an_explicit_utc_instant(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        member = _user(accounts, 'member')
        months_ago = (datetime.utcnow() - timedelta(days=120)).replace(microsecond=0)
        _set_last_seen(member, months_ago)

        row = _listed_by_id(_client_for(admin))[member.id]

        assert row['last_seen_at'].endswith('Z')
        assert datetime.fromisoformat(row['last_seen_at'].removesuffix('Z')) == months_ago


def test_a_group_admin_is_not_given_the_time(accounts):
    with app.app_context():
        lead = _user(accounts, 'lead')
        member = _user(accounts, 'member')
        _set_last_seen(lead, datetime.utcnow())
        _set_last_seen(member, datetime.utcnow() - timedelta(days=3))
        group = Group(name=f'la_{uuid.uuid4().hex[:8]}')
        db.session.add(group)
        db.session.commit()
        try:
            db.session.add_all([
                GroupMembership(group_id=group.id, user_id=lead.id, role='admin'),
                GroupMembership(group_id=group.id, user_id=member.id, role='member'),
            ])
            db.session.commit()

            rows = _listed_users(_client_for(lead), 'per_page=1000&sort=last_active&order=desc')

            assert {row['id'] for row in rows} == {lead.id, member.id}
            assert all('last_seen_at' not in row for row in rows)
            # The sort is not honoured either, since it would reveal the order
            # (by last active the lead would be first): the order is by id.
            assert [row['id'] for row in rows] == [member.id, lead.id]
        finally:
            db.session.rollback()
            GroupMembership.query.filter_by(group_id=group.id).delete()
            db.session.delete(db.session.get(Group, group.id))
            db.session.commit()


# --- What counts as use ------------------------------------------------------

def test_every_background_endpoint_is_a_registered_route():
    """A renamed view must not drop out of the list unnoticed."""
    missing = sorted(last_active.BACKGROUND_ENDPOINTS - set(app.view_functions))
    assert missing == []


@pytest.mark.parametrize('source, literal, path, method', [
    ('static/js/modules/composables/notifications.js', "'/api/notifications/count'",
     '/api/notifications/count', 'GET'),
    ('static/js/app.modular.js', "'/api/recordings/job-queue-status'",
     '/api/recordings/job-queue-status', 'GET'),
    ('static/js/modules/composables/reprocess.js', '`/recording/${recordingId}/status`',
     '/recording/1/status', 'GET'),
    ('static/js/modules/utils/summary-follow-up.js', '`/recording/${recordingId}/status`',
     '/recording/1/status', 'GET'),
    ('static/js/csrf-refresh.js', "'/api/csrf-token'", '/api/csrf-token', 'GET'),
])
def test_addresses_polled_by_the_front_end_are_background_endpoints(source, literal, path, method):
    with open(os.path.join(ROOT, source), encoding='utf-8') as handle:
        assert literal in handle.read(), f'{source} no longer requests {literal}'
    endpoint, _ = app.url_map.bind('localhost').match(path, method=method)
    assert endpoint in last_active.BACKGROUND_ENDPOINTS
    assert last_active.counts_as_activity(endpoint) is False


@pytest.mark.parametrize('method, path', [
    ('GET', '/api/notifications/count'),
    ('GET', '/api/recordings/job-queue-status'),
    ('GET', '/recording/999999/status'),
    ('GET', '/status/999999'),
    ('POST', '/api/recordings/batch-status'),
    ('GET', '/api/csrf-token'),
    ('GET', '/static/css/styles.css'),
    ('GET', '/no/such/route'),
])
def test_requests_sent_on_a_timer_are_not_use(accounts, method, path):
    with app.app_context():
        user = _user(accounts, 'reader')
        client = _client_for(user)

        resp = client.open(path, method=method, json={} if method == 'POST' else None)
        assert resp.status_code < 500

        assert _stored_last_seen(user) is None

        # The same session then does something, and that is recorded.
        assert client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(user) is not None


def test_a_request_refused_for_its_scope_still_counts(accounts):
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


def test_an_incognito_request_counts_and_stores_only_the_time(accounts):
    with app.app_context():
        user = _user(accounts, 'private')
        before = db.session.get(User, user.id)
        columns = [c.name for c in User.__table__.columns if c.name != 'last_seen_at']
        snapshot = {name: getattr(before, name) for name in columns}

        # Refused (no file, or the mode is off), which does not matter here:
        # the request was made by the account on an incognito route.
        resp = _client_for(user).post('/api/recordings/incognito', data={})
        assert resp.status_code in (400, 403)

        assert _stored_last_seen(user) is not None
        after = db.session.get(User, user.id)
        assert {name: getattr(after, name) for name in columns} == snapshot


def test_an_anonymous_request_records_nothing(accounts):
    with app.app_context():
        user = _user(accounts, 'member')

        resp = app.test_client().get('/admin/users')

        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']
        assert _stored_last_seen(user) is None


def test_a_request_with_an_invalid_token_records_nothing(accounts):
    with app.app_context():
        user = _user(accounts, 'member')

        resp = app.test_client().get(
            '/admin/users',
            headers={'Authorization': 'Bearer not-a-real-token'})

        assert resp.status_code == 401
        assert _stored_last_seen(user) is None


# --- How often it is written -------------------------------------------------

def test_record_activity_writes_once_and_then_skips(accounts):
    with app.app_context():
        user = _user(accounts, 'member')

        assert last_active.record_activity(user) is True
        first = _stored_last_seen(user)
        assert first is not None

        assert last_active.record_activity(db.session.get(User, user.id)) is False
        assert _stored_last_seen(user) == first


def test_a_second_request_within_the_interval_keeps_the_time(accounts):
    with app.app_context():
        member = _user(accounts, 'member')
        client = _client_for(member)

        assert client.get('/api/v1/capabilities').status_code == 200
        first = _stored_last_seen(member)
        assert first is not None

        assert client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(member) == first


def test_the_time_is_written_once_per_five_minutes(accounts):
    assert last_active.WRITE_INTERVAL == timedelta(minutes=5)
    with app.app_context():
        member = _user(accounts, 'member')
        client = _client_for(member)

        recent = (datetime.utcnow() - timedelta(minutes=4)).replace(microsecond=0)
        _set_last_seen(member, recent)
        assert client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(member) == recent

        due = (datetime.utcnow() - timedelta(minutes=6)).replace(microsecond=0)
        _set_last_seen(member, due)
        assert client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(member) > due


def test_two_writers_cannot_move_the_time_backwards(accounts):
    """The UPDATE is conditional: a worker that still holds an old row does
    not overwrite a time that another worker stored a moment ago."""
    with app.app_context():
        member = _user(accounts, 'member')
        newer = (datetime.utcnow() + timedelta(minutes=1)).replace(microsecond=0)
        now = datetime.utcnow()

        _set_last_seen(member, newer)
        written = last_active._write(member.id, now, now - last_active.WRITE_INTERVAL)

        assert written is False
        assert _stored_last_seen(member) == newer


# --- The write cannot delay or fail the request ------------------------------

def test_the_write_does_not_commit_the_session_of_the_request(accounts):
    with app.app_context():
        member = _user(accounts, 'member')
        loaded = db.session.get(User, member.id)
        loaded.name = 'not saved'  # pending in the session, not flushed

        assert last_active.record_activity(loaded) is True
        db.session.rollback()

        db.session.expire_all()
        stored = db.session.get(User, member.id)
        assert stored.name != 'not saved'
        assert stored.last_seen_at is not None


def test_a_locked_database_does_not_hold_up_the_request(accounts, caplog):
    with app.app_context():
        member = _user(accounts, 'member')
        client = _client_for(member)

        with caplog.at_level(logging.WARNING, logger='src.services.last_active'):
            with _database_locked_by_another_writer(member):
                started = time.monotonic()
                resp = client.get('/api/v1/capabilities')
                elapsed = time.monotonic() - started

        assert resp.status_code == 200
        assert elapsed < SLOW_SECONDS, f'the request took {elapsed:.2f}s behind the lock'
        assert _stored_last_seen(member) is None
        assert any('was not stored' in record.getMessage() for record in caplog.records)

        # Within the retry interval nothing is tried, even without the lock.
        assert client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(member) is None

        # Once the retry interval has passed, it is stored.
        last_active._retry_after[member.id] = time.monotonic() - 1
        assert client.get('/api/v1/capabilities').status_code == 200
        assert _stored_last_seen(member) is not None


def test_a_locked_database_is_tried_once_per_retry_interval(accounts, monkeypatch):
    with app.app_context():
        member = _user(accounts, 'member')
        client = _client_for(member)
        attempts = []
        real_write = last_active._write

        def counting_write(*args):
            attempts.append(args)
            return real_write(*args)

        monkeypatch.setattr(last_active, '_write', counting_write)
        with _database_locked_by_another_writer(member):
            started = time.monotonic()
            for _ in range(5):
                assert client.get('/api/v1/capabilities').status_code == 200
            elapsed = time.monotonic() - started

        assert len(attempts) == 1
        assert elapsed < SLOW_SECONDS


def test_the_wait_for_a_lock_is_a_fraction_of_a_second(accounts):
    """Measured on the write itself, without the rest of a request."""
    assert last_active.LOCK_WAIT_SECONDS <= 0.5
    with app.app_context():
        member = _user(accounts, 'member')

        with _database_locked_by_another_writer(member):
            started = time.monotonic()
            written = last_active.record_activity(db.session.get(User, member.id))
            elapsed = time.monotonic() - started

        assert written is False
        assert elapsed < 1.5, f'the write waited {elapsed:.2f}s for the lock'


@pytest.mark.parametrize('failure', ['write raises', 'database unreachable'])
def test_a_failed_write_is_swallowed(accounts, monkeypatch, caplog, failure):
    with app.app_context():
        member = _user(accounts, 'member')
        client = _client_for(member)

        if failure == 'write raises':
            def broken(*args):
                raise RuntimeError('disk is gone')
            monkeypatch.setattr(last_active, '_write', broken)
        else:
            from sqlalchemy import create_engine
            from sqlalchemy.pool import NullPool
            unreachable = create_engine(
                'sqlite:////nonexistent-directory/last-active.db', poolclass=NullPool)
            monkeypatch.setattr(last_active, '_write_engine', lambda: unreachable)

        with caplog.at_level(logging.WARNING, logger='src.services.last_active'):
            resp = client.get('/api/v1/capabilities')

        assert resp.status_code == 200
        assert resp.get_json()
        assert _stored_last_seen(member) is None
        messages = [record.getMessage() for record in caplog.records
                    if record.name == 'src.services.last_active']
        assert len(messages) == 1 and 'was not stored' in messages[0]
        # The text of the error is not logged, only its kind.
        assert 'disk is gone' not in messages[0]

        # The session of the request is usable afterwards.
        assert db.session.get(User, member.id) is not None


# --- Sorting -----------------------------------------------------------------

def _order_of(rows, users):
    wanted = {user.id for user in users}
    return [row['id'] for row in rows if row['id'] in wanted]


def test_the_list_sorts_by_last_active_with_never_used_accounts_first(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        recent = _user(accounts, 'recent')
        never = _user(accounts, 'never')
        months = _user(accounts, 'months')
        _set_last_seen(recent, datetime.utcnow() - timedelta(hours=3))
        _set_last_seen(months, datetime.utcnow() - timedelta(days=200))
        client = _client_for(admin)
        trio = [recent, never, months]

        ascending = _listed_users(client, 'per_page=1000&sort=last_active&order=asc')
        assert _order_of(ascending, trio) == [never.id, months.id, recent.id]
        # Ascending is the default direction.
        assert _order_of(_listed_users(client, 'per_page=1000&sort=last_active'), trio) == [
            never.id, months.id, recent.id]

        descending = _listed_users(client, 'per_page=1000&sort=last_active&order=desc')
        assert _order_of(descending, trio) == [recent.id, months.id, never.id]


def test_the_sorted_list_is_paged_in_that_order(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        _set_last_seen(admin, datetime.utcnow())
        client = _client_for(admin)

        everyone = [row['id'] for row in _listed_users(
            client, 'per_page=1000&sort=last_active&order=desc')]
        paged = []
        page = 1
        while True:
            resp = client.get(f'/admin/users?per_page=2&page={page}&sort=last_active&order=desc')
            body = resp.get_json()
            paged.extend(row['id'] for row in body['users'])
            if not body['pagination']['has_next']:
                break
            page += 1

        assert paged == everyone
        assert len(paged) == len(set(paged))


def test_the_list_sorts_by_username_without_regard_to_case(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        client = _client_for(admin)
        tag = uuid.uuid4().hex[:6]
        users = []
        for name in (f'zeta{tag}', f'Alpha{tag}', f'mid{tag}'):
            user = User(username=name, email=f'{name}@local.test',
                        password=bcrypt.generate_password_hash('Passw0rd!').decode())
            db.session.add(user)
            db.session.commit()
            accounts.append(user.id)
            users.append(user)
        zeta, alpha, mid = users

        ascending = _listed_users(client, 'per_page=1000&sort=username&order=asc')
        assert _order_of(ascending, users) == [alpha.id, mid.id, zeta.id]
        descending = _listed_users(client, 'per_page=1000&sort=username&order=desc')
        assert _order_of(descending, users) == [zeta.id, mid.id, alpha.id]


def test_the_default_order_and_an_unknown_sort_are_by_id(accounts):
    with app.app_context():
        admin = _user(accounts, 'admin', is_admin=True)
        _user(accounts, 'second')
        client = _client_for(admin)

        for query in ('per_page=1000', 'per_page=1000&sort=email', 'per_page=1000&sort=&order='):
            ids = [row['id'] for row in _listed_users(client, query)]
            assert ids == sorted(ids), query
