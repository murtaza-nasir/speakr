"""Audit log: sign-in, session, password and API token events.

Each action writes its event exactly once, and nothing typed by the user (the
address, a password, a token name, a token value) reaches a row.
"""

import json
import uuid
from unittest import mock

import pytest

from tests.audit_log_support import (  # noqa: E402
    PASSWORD, anonymous_client, api_token, app, audit, bearer, client_for, db, make_user, max_id,
    names, remove_users, reset_audit_settings, rows_after,
)
from src.models import APIToken, LoginFailure, SystemSetting, User  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True
ADDRESS = '198.51.100.20'


@pytest.fixture(autouse=True)
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        # Ids of deleted accounts are given out again on SQLite, so a new
        # address per test keeps the de-duplication keys of two tests apart.
        global ADDRESS
        h = uuid.uuid4().hex
        ADDRESS = f'2001:db8:{h[:4]}:{h[4:8]}::1'
        yield users
        audit.flush()
        db.session.rollback()
        for user_id in users:
            LoginFailure.query.filter_by(user_id=user_id).delete()
        db.session.commit()
        remove_users(users)


def _post_login(client, email, password, remember=False):
    data = {'email': email, 'password': password}
    if remember:
        data['remember'] = 'y'
    return client.post('/login', data=data, environ_base={'REMOTE_ADDR': ADDRESS},
                       headers={'User-Agent': 'audit-test-agent'})


def test_password_sign_in_and_sign_out(created):
    user = make_user(created)
    start = max_id()
    client = anonymous_client()
    assert _post_login(client, user.email, PASSWORD, remember=True).status_code == 302
    assert client.post('/logout').status_code == 302
    # A second sign-out without a session records nothing.
    assert client.post('/logout').status_code == 302
    rows = rows_after(start)
    assert names(rows) == ['auth.login.succeeded', 'auth.logout']
    signed_in, signed_out = rows
    assert signed_in.actor_kind == 'session' and signed_in.actor_user_id == user.id
    assert signed_in.details == {'method': 'password', 'remember': True}
    assert signed_in.ip == ADDRESS and signed_in.user_agent == 'audit-test-agent'
    assert signed_in.event_group == 'auth' and signed_in.outcome == 'success'
    assert signed_out.actor_user_id == user.id and signed_out.details is None


def test_failed_sign_ins_carry_a_reason_code_and_never_the_typed_address(created):
    user = make_user(created)
    no_password = make_user(created, prefix='sso', password=None)
    start = max_id()
    client = anonymous_client()
    _post_login(client, user.email, 'wrong-SENTINELPASS')
    _post_login(client, 'SENTINELADDR@nowhere.test', 'wrong-SENTINELPASS')
    _post_login(client, no_password.email, 'wrong-SENTINELPASS')
    rows = rows_after(start, 'auth.login.failed')
    by_target = {r.target_user_id: r for r in rows}
    assert by_target[user.id].details == {'reason': 'bad_password', 'method': 'password'}
    assert by_target[None].details == {'reason': 'unknown_account', 'method': 'password'}
    assert by_target[no_password.id].details == {'reason': 'no_local_password', 'method': 'password'}
    assert all(r.outcome == 'failed' and r.actor_kind == 'anonymous' and r.actor_user_id is None for r in rows)
    dump = json.dumps([r.to_dict() for r in rows_after(start)])
    assert 'SENTINEL' not in dump and user.email not in dump


def test_repeated_failures_give_one_row_per_minute_address_and_account(created):
    user = make_user(created)
    SystemSetting.set_setting('login_max_failed_attempts', '0', None, 'integer')
    try:
        start = max_id()
        client = anonymous_client()
        for _ in range(4):
            _post_login(client, user.email, 'wrong')
        assert len(rows_after(start, 'auth.login.failed')) == 1
    finally:
        SystemSetting.set_setting('login_max_failed_attempts', '5', None, 'integer')


def test_the_lock_is_recorded_once_and_later_attempts_are_refused_as_locked(created):
    user = make_user(created)
    SystemSetting.set_setting('login_max_failed_attempts', '2', None, 'integer')
    SystemSetting.set_setting('login_lockout_minutes', '7', None, 'integer')
    try:
        start = max_id()
        client = anonymous_client()
        _post_login(client, user.email, 'wrong')
        _post_login(client, user.email, 'wrong')
        locked = rows_after(start, 'auth.login.locked')
        assert len(locked) == 1
        assert locked[0].target_user_id == user.id and locked[0].details == {'minutes': 7}
        audit.reset_memory()
        with mock.patch.object(audit, '_epoch', return_value=audit.time.time() + 120):
            _post_login(client, user.email, PASSWORD)
        reasons = [r.details['reason'] for r in rows_after(start, 'auth.login.failed')]
        assert reasons == ['bad_password', 'locked']
        assert rows_after(start, 'auth.login.succeeded') == []
    finally:
        SystemSetting.set_setting('login_max_failed_attempts', '5', None, 'integer')
        SystemSetting.set_setting('login_lockout_minutes', '15', None, 'integer')


def test_registration(created):
    from src.api import auth as auth_mod
    start = max_id()
    client = anonymous_client()
    name = 'reg' + uuid.uuid4().hex[:8]
    with mock.patch.object(auth_mod, 'registration_enabled', return_value=True), \
            mock.patch.object(auth_mod, 'is_registration_domain_allowed', return_value=True), \
            mock.patch.object(auth_mod, 'is_email_verification_enabled', return_value=False):
        resp = client.post('/register', data={
            'username': name, 'email': f'{name}@example.com',
            'password': PASSWORD, 'confirm_password': PASSWORD})
    assert resp.status_code == 302, resp.data[:300]
    user = User.query.filter_by(username=name).first()
    created.append(user.id)
    rows = rows_after(start)
    assert names(rows) == ['auth.registered']
    assert rows[0].target_user_id == user.id and rows[0].actor_kind == 'anonymous' and rows[0].details is None
    assert name not in json.dumps(rows[0].to_dict())


def test_password_change_is_recorded_with_the_change_only(created):
    user = make_user(created)
    client = client_for(user)
    start = max_id()
    client.post('/change_password', data={
        'current_password': 'not-the-password', 'new_password': 'N3w-Passw0rd!', 'confirm_password': 'N3w-Passw0rd!'})
    assert rows_after(start) == []
    client.post('/change_password', data={
        'current_password': PASSWORD, 'new_password': 'N3w-Passw0rd!', 'confirm_password': 'N3w-Passw0rd!'})
    rows = rows_after(start)
    assert names(rows) == ['auth.password.changed']
    assert rows[0].actor_user_id == user.id and rows[0].details is None


def test_password_reset_request_and_reset(created):
    from src.api import auth as auth_mod
    user = make_user(created)
    start = max_id()
    client = anonymous_client()
    # SQLite can give this user the id of a user an earlier test deleted, and
    # a reset request of that user may still sit in the same five-minute
    # de-duplication bucket. A bucket of this test's own avoids that.
    own_bucket = 2_000_000_000 + (uuid.uuid4().int % 100_000) * audit.DEDUP_RESET_REQUESTED
    with mock.patch.object(auth_mod, 'is_smtp_configured', return_value=True), \
            mock.patch.object(auth_mod, 'can_resend_password_reset', return_value=(True, 0)), \
            mock.patch.object(auth_mod, 'send_password_reset_email', return_value=True), \
            mock.patch.object(audit, '_epoch', return_value=own_bucket):
        client.post('/forgot-password', data={'email': user.email})
        client.post('/forgot-password', data={'email': user.email})
        client.post('/forgot-password', data={'email': 'SENTINELADDR@nowhere.test'})
    requested = rows_after(start, 'auth.password.reset_requested')
    assert sorted((r.target_user_id or 0) for r in requested) == [0, user.id]

    user = db.session.get(User, user.id)
    from src.utils.token_auth import hash_token
    link_token = 'tok-' + 'a' * 20
    user.password_reset_token = hash_token(link_token)
    db.session.commit()
    with mock.patch.object(auth_mod, 'verify_reset_token', return_value=user.id):
        resp = client.post(f'/reset-password/{link_token}', data={
            'password': 'An0ther-Passw0rd!', 'confirm_password': 'An0ther-Passw0rd!'})
    assert resp.status_code == 302
    reset = rows_after(start, 'auth.password.reset')
    assert len(reset) == 1 and reset[0].target_user_id == user.id and reset[0].actor_kind == 'anonymous'
    assert 'SENTINEL' not in json.dumps([r.to_dict() for r in rows_after(start)])


def test_sso_unlink(created):
    user = make_user(created, sso_subject='sub-' + str(max_id()), sso_provider='Test')
    client = client_for(user)
    start = max_id()
    client.post('/auth/sso/unlink')
    rows = rows_after(start)
    assert names(rows) == ['auth.sso.unlinked'] and rows[0].actor_user_id == user.id


def test_sso_sign_in_link_and_failure(created):
    from src.api import auth as auth_mod
    user = make_user(created)
    other = make_user(created, prefix='lnk')
    start = max_id()
    patches = [
        mock.patch.object(auth_mod, 'is_sso_enabled', return_value=True),
        mock.patch.object(auth_mod, 'get_sso_client', return_value=mock.MagicMock()),
        mock.patch.object(auth_mod, 'get_sso_config', return_value={'provider_name': 'Test'}),
        mock.patch.object(auth_mod, 'update_user_profile_from_claims', return_value=None),
    ]
    for p in patches:
        p.start()
    try:
        with mock.patch.object(auth_mod, 'resolve_userinfo', return_value={'sub': 'subject-1'}), \
                mock.patch.object(auth_mod, 'create_or_update_sso_user', return_value=user):
            assert anonymous_client().get('/auth/sso/callback').status_code == 302
        with mock.patch.object(auth_mod, 'resolve_userinfo', side_effect=RuntimeError('SENTINEL provider text')):
            assert anonymous_client().get('/auth/sso/callback').status_code == 302
        client = client_for(other)
        with client.session_transaction() as sess:
            sess['sso_link_user_id'] = other.id
        with mock.patch.object(auth_mod, 'resolve_userinfo', return_value={'sub': 'subject-' + str(start)}):
            assert client.get('/auth/sso/callback').status_code == 302
    finally:
        for p in patches:
            p.stop()
    rows = sorted(rows_after(start), key=lambda r: ['auth.login.succeeded', 'auth.login.failed', 'auth.sso.linked'].index(r.event))
    assert names(rows) == ['auth.login.succeeded', 'auth.login.failed', 'auth.sso.linked']
    assert rows[0].details == {'method': 'sso', 'remember': False} and rows[0].actor_user_id == user.id
    assert rows[1].details == {'reason': 'sso_error', 'method': 'sso'}
    assert rows[2].target_user_id == other.id
    assert 'SENTINEL' not in json.dumps([r.to_dict() for r in rows])


def test_token_lifecycle_records_the_id_and_scopes_and_never_the_name(created):
    user = make_user(created)
    client = client_for(user)
    start = max_id()
    resp = client.post('/api/tokens', json={'name': 'SENTINELNAME', 'scopes': ['read', 'write'], 'expires_in_days': 30})
    assert resp.status_code == 201, resp.data
    body = resp.get_json()
    token_id, plain = body['id'], body['token']
    assert client.patch(f'/api/tokens/{token_id}', json={'scopes': ['read']}).status_code == 200
    assert client.patch(f'/api/tokens/{token_id}', json={'name': 'SENTINELNAME2'}).status_code == 200
    # First use, then a second use.
    fresh = anonymous_client()
    assert fresh.get('/api/v1/users/me', headers=bearer(plain)).status_code == 200
    assert anonymous_client().get('/api/v1/users/me', headers=bearer(plain)).status_code == 200
    client = client_for(user)
    assert client.delete(f'/api/tokens/{token_id}').status_code == 200
    rows = rows_after(start)
    # The first use goes through the queue, so its id is not in sequence.
    assert sorted(names(rows)) == ['token.changed', 'token.changed', 'token.created', 'token.first_used', 'token.revoked']
    rows = [r for r in rows if r.event != 'token.first_used'] + [r for r in rows if r.event == 'token.first_used']
    rows = [rows[0], rows[1], rows[2], rows[4], rows[3]]
    assert rows[0].details == {'token_id': token_id, 'scopes': ['read', 'write'], 'expires': True}
    assert rows[0].target_user_id == user.id and rows[0].actor_kind == 'session'
    assert rows[1].details == {'token_id': token_id, 'scopes': ['read']}
    assert rows[2].details == {'token_id': token_id}
    assert rows[3].actor_kind == 'api_token' and rows[3].actor_token_id == token_id
    assert rows[3].actor_user_id == user.id
    assert rows[4].details == {'token_id': token_id}
    dump = json.dumps([r.to_dict() for r in rows])
    assert 'SENTINEL' not in dump and plain not in dump


def test_a_token_revokes_itself(created):
    user = make_user(created)
    plain, token_id = api_token(user, scopes='["read"]')
    start = max_id()
    resp = anonymous_client().delete('/api/v1/tokens/current', headers=bearer(plain))
    assert resp.status_code == 200, resp.data
    rows = sorted(rows_after(start), key=lambda r: r.event)
    assert names(rows) == ['token.first_used', 'token.revoked']
    assert rows[1].actor_kind == 'api_token' and rows[1].actor_token_id == token_id
    assert db.session.get(APIToken, token_id) is None


def test_nothing_is_recorded_while_the_log_is_off(created):
    from tests.audit_log_support import set_setting
    user = make_user(created)
    set_setting('audit_log_enabled', 'false')
    start = max_id()
    client = anonymous_client()
    _post_login(client, user.email, 'wrong')
    _post_login(client, user.email, PASSWORD)
    client.post('/api/tokens', json={'name': 'x'})
    client.post('/logout')
    assert rows_after(start) == []
