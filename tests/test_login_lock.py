"""Password sign-in lock per account and client address (discussion #529).

Wrong passwords are counted per account and client address. At the maximum,
password sign-ins for that account from that address are refused for the
lockout period, a correct password included. Other addresses still sign in.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt  # noqa: E402
from src.models import User, LoginFailure, SystemSetting  # noqa: E402
from src.services import login_lock  # noqa: E402
from src.services.email import generate_password_reset_token  # noqa: E402

PW = "Sup3rSecret!"
ADDR = "203.0.113.7"
OTHER = "198.51.100.9"


@pytest.fixture(autouse=True)
def _ctx():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        saved = {k: SystemSetting.get_setting(k) for k in ('login_max_failed_attempts', 'login_lockout_minutes')}
        _setting('login_max_failed_attempts', 5)
        _setting('login_lockout_minutes', 15)
        made = []
        yield made
        db.session.rollback()
        for u in made:
            u = db.session.get(User, u.id)
            if u:
                db.session.delete(u)
        for key, value in saved.items():
            row = SystemSetting.query.filter_by(key=key).first()
            if value is None and row:
                db.session.delete(row)
            elif value is not None:
                _setting(key, value)
        db.session.commit()
    app.config['WTF_CSRF_ENABLED'] = prev


def _setting(key, value):
    SystemSetting.set_setting(key=key, value=str(value), setting_type='integer')


def _user(made, password=PW, **extra):
    suffix = uuid.uuid4().hex[:10]
    u = User(username=f"ll_{suffix}", email=f"ll_{suffix}@example.com",
             password=bcrypt.generate_password_hash(password).decode() if password else None,
             email_verified=True, **extra)
    db.session.add(u)
    db.session.commit()
    made.append(u)
    return u


def _forget_current_user():
    # The test holds an app context open, so Flask-Login's cached user on g
    # would carry over from one request to the next.
    g.pop('_login_user', None)


def _login(user_or_email, password, addr=ADDR):
    _forget_current_user()
    email = user_or_email if isinstance(user_or_email, str) else user_or_email.email
    client = app.test_client()
    resp = client.post('/login', data={'email': email, 'password': password},
                       environ_base={'REMOTE_ADDR': addr})
    with client.session_transaction() as s:
        signed_in = s.get('_user_id') is not None
    return resp, signed_in


def _rows(user):
    return LoginFailure.query.filter_by(user_id=user.id).all()


def _as(user):
    _forget_current_user()
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True
    return client


def test_fifth_failure_locks_and_correct_password_is_refused(_ctx):
    user = _user(_ctx)
    for _ in range(5):
        resp, signed_in = _login(user, 'wrong')
        assert resp.status_code == 200 and not signed_in
    (row,) = _rows(user)
    assert row.locked_until and row.locked_until > datetime.utcnow()
    assert row.failed_count == 0
    resp, signed_in = _login(user, PW)
    assert resp.status_code == 200 and not signed_in
    # Further attempts during the lock do not move it or count
    until = row.locked_until
    _login(user, 'wrong')
    db.session.expire_all()
    (row,) = _rows(user)
    assert row.locked_until == until and row.failed_count == 0


def test_locked_page_matches_wrong_password_page(_ctx):
    user = _user(_ctx)
    wrong, _ = _login(user, 'wrong')
    for _ in range(4):
        _login(user, 'wrong')
    locked, _ = _login(user, PW)
    assert locked.status_code == wrong.status_code == 200
    assert locked.get_data(as_text=True).count('alert') == wrong.get_data(as_text=True).count('alert')


def test_other_address_still_signs_in(_ctx):
    user = _user(_ctx)
    for _ in range(5):
        _login(user, 'wrong', addr=ADDR)
    _, signed_in = _login(user, PW, addr=OTHER)
    assert signed_in
    _, signed_in = _login(user, PW, addr=ADDR)
    assert not signed_in


def test_success_before_maximum_clears_count(_ctx):
    user = _user(_ctx)
    for _ in range(4):
        _login(user, 'wrong')
    assert _rows(user)[0].failed_count == 4
    _, signed_in = _login(user, PW)
    assert signed_in
    db.session.expire_all()
    assert _rows(user) == []
    # The count starts again, so four more failures do not lock
    for _ in range(4):
        _login(user, 'wrong')
    _, signed_in = _login(user, PW)
    assert signed_in


def test_expired_lock_allows_sign_in(_ctx):
    user = _user(_ctx)
    db.session.add(LoginFailure(user_id=user.id, address=ADDR, failed_count=0,
                                locked_until=datetime.utcnow() - timedelta(minutes=1),
                                updated_at=datetime.utcnow()))
    db.session.commit()
    _, signed_in = _login(user, PW)
    assert signed_in
    db.session.expire_all()
    assert _rows(user) == []


def test_failure_after_expired_lock_starts_from_one(_ctx):
    user = _user(_ctx)
    db.session.add(LoginFailure(user_id=user.id, address=ADDR, failed_count=0,
                                locked_until=datetime.utcnow() - timedelta(minutes=1),
                                updated_at=datetime.utcnow()))
    db.session.commit()
    _login(user, 'wrong')
    db.session.expire_all()
    (row,) = _rows(user)
    assert row.failed_count == 1
    assert not login_lock.is_locked(user.id, ADDR)


def test_stale_failures_are_forgotten(_ctx):
    user = _user(_ctx)
    db.session.add(LoginFailure(user_id=user.id, address=ADDR, failed_count=4,
                                updated_at=datetime.utcnow() - timedelta(days=2)))
    db.session.commit()
    _login(user, 'wrong')
    db.session.expire_all()
    (row,) = _rows(user)
    assert row.failed_count == 1 and row.locked_until is None


def test_zero_attempts_disables_lock(_ctx):
    _setting('login_max_failed_attempts', 0)
    db.session.commit()
    user = _user(_ctx)
    for _ in range(7):
        _login(user, 'wrong')
    assert _rows(user) == []
    _, signed_in = _login(user, PW)
    assert signed_in


def test_lockout_minutes_below_one_uses_default(_ctx):
    _setting('login_lockout_minutes', 0)
    db.session.commit()
    assert login_lock.lock_settings() == (5, login_lock.DEFAULT_LOCKOUT_MINUTES)


def test_custom_maximum(_ctx):
    _setting('login_max_failed_attempts', 2)
    db.session.commit()
    user = _user(_ctx)
    _login(user, 'wrong')
    _login(user, 'wrong')
    _, signed_in = _login(user, PW)
    assert not signed_in


def test_unknown_email_and_sso_account_write_nothing(_ctx):
    before = LoginFailure.query.count()
    for _ in range(6):
        _login(f"nobody_{uuid.uuid4().hex[:8]}@example.com", 'wrong')
    sso = _user(_ctx, password=None, sso_subject=f"sub-{uuid.uuid4().hex}")
    for _ in range(6):
        _login(sso, 'wrong')
    assert LoginFailure.query.count() == before


def test_get_login_page_counts_nothing(_ctx):
    user = _user(_ctx)
    app.test_client().get('/login', environ_base={'REMOTE_ADDR': ADDR})
    assert _rows(user) == []


def test_unverified_account_correct_password_clears_count(_ctx, monkeypatch):
    from src.api import auth as auth_mod
    monkeypatch.setattr(auth_mod, 'is_email_verification_required', lambda: True)
    user = _user(_ctx)
    user.email_verified = False
    db.session.commit()
    for _ in range(3):
        _login(user, 'wrong')
    resp, signed_in = _login(user, PW)
    assert resp.status_code == 200 and not signed_in
    db.session.expire_all()
    assert _rows(user) == []


def test_reset_password_clears_lock(_ctx):
    user = _user(_ctx)
    for _ in range(5):
        _login(user, 'wrong')
    token = generate_password_reset_token(user.id)
    user.password_reset_token = token
    db.session.commit()
    resp = app.test_client().post(f'/reset-password/{token}',
                                  data={'password': 'NewPassw0rd!', 'confirm_password': 'NewPassw0rd!'})
    assert resp.status_code == 302
    db.session.expire_all()
    assert _rows(user) == []
    _, signed_in = _login(user, 'NewPassw0rd!')
    assert signed_in


def test_admin_unlock_and_list_flag(_ctx):
    admin = _user(_ctx, is_admin=True)
    user = _user(_ctx)
    for _ in range(5):
        _login(user, 'wrong')
    client = _as(admin)
    rows = {r['id']: r for r in client.get('/admin/users').get_json()}
    assert rows[user.id]['login_locked'] is True
    assert rows[admin.id]['login_locked'] is False
    assert 'failed_count' not in rows[user.id] and 'address' not in rows[user.id]

    resp = client.put(f'/admin/users/{user.id}', json={'unlock_login': True})
    assert resp.status_code == 200
    assert resp.get_json()['login_locked'] is False
    db.session.expire_all()
    assert _rows(user) == []
    rows = {r['id']: r for r in client.get('/admin/users').get_json()}
    assert rows[user.id]['login_locked'] is False
    _, signed_in = _login(user, PW)
    assert signed_in


def test_unlock_requires_site_admin(_ctx):
    other = _user(_ctx)
    user = _user(_ctx)
    for _ in range(5):
        _login(user, 'wrong')
    resp = _as(other).put(f'/admin/users/{user.id}', json={'unlock_login': True})
    assert resp.status_code == 403
    db.session.expire_all()
    assert login_lock.is_locked(user.id, ADDR)


def test_admin_password_change_clears_lock(_ctx):
    admin = _user(_ctx, is_admin=True)
    user = _user(_ctx)
    for _ in range(5):
        _login(user, 'wrong')
    resp = _as(admin).put(f'/admin/users/{user.id}', json={'password': 'An0therPass!'})
    assert resp.status_code == 200
    db.session.expire_all()
    assert _rows(user) == []


def test_unlock_flag_must_be_true(_ctx):
    admin = _user(_ctx, is_admin=True)
    user = _user(_ctx)
    for _ in range(5):
        _login(user, 'wrong')
    resp = _as(admin).put(f'/admin/users/{user.id}', json={'unlock_login': 'yes'})
    assert resp.status_code == 200
    assert login_lock.is_locked(user.id, ADDR)


def test_deleting_user_removes_rows(_ctx):
    admin = _user(_ctx, is_admin=True)
    user = _user(_ctx)
    _login(user, 'wrong')
    uid = user.id
    resp = _as(admin).delete(f'/admin/users/{uid}')
    assert resp.status_code == 200
    db.session.expire_all()
    assert LoginFailure.query.filter_by(user_id=uid).count() == 0


@pytest.mark.parametrize('raw, key', [
    ('203.0.113.7', '203.0.113.7'),
    ('2001:db8:1:2:aaaa::1', '2001:db8:1:2::/64'),
    ('2001:db8:1:2:ffff:ffff:ffff:ffff', '2001:db8:1:2::/64'),
    ('fe80::1%eth0', 'fe80::/64'),
    ('::ffff:203.0.113.7', '203.0.113.7'),
    ('', 'unknown'),
    (None, 'unknown'),
    ('not-an-ip', 'not-an-ip'),
])
def test_client_key(raw, key):
    assert login_lock.client_key(raw) == key


def test_ipv6_addresses_in_one_prefix_share_a_lock(_ctx):
    user = _user(_ctx)
    for i in range(5):
        _login(user, 'wrong', addr=f'2001:db8:1:2::{i + 1:x}')
    _, signed_in = _login(user, PW, addr='2001:db8:1:2::ff')
    assert not signed_in
    _, signed_in = _login(user, PW, addr='2001:db8:1:3::1')
    assert signed_in
