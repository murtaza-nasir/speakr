"""Email verification and password reset tokens are stored as hashes (#693).

The link in the email has the signed itsdangerous token; the user row holds
only its SHA-256 hash, as for API tokens. A copy of the database therefore
holds no link that works. Both links are single use: the link must belong to
the hash of the latest token stored on the user. A token that an earlier
version stored in plaintext is removed once on upgrade.

SHARED-DB: the users are removed afterwards.
"""

import os
import re
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from src.app import app, bcrypt
from src.database import db
from src.models import User
from src.services import email as email_service
from src.utils.token_auth import hash_token

COLUMN = {email_service.send_password_reset_email: 'password_reset_token',
          email_service.send_verification_email: 'email_verification_token'}
LINK = {email_service.send_password_reset_email: '/reset-password/',
        email_service.send_verification_email: '/verify-email/'}
SENDERS = list(COLUMN)


@pytest.fixture
def csrf_off():
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = saved


@pytest.fixture
def user():
    created = []

    def make(**kwargs):
        with app.app_context():
            suffix = uuid.uuid4().hex[:8]
            u = User(username=f'hashtok_{suffix}', email=f'hashtok_{suffix}@example.com',
                     password=bcrypt.generate_password_hash('OldPassw0rd!').decode(), **kwargs)
            db.session.add(u)
            db.session.commit()
            created.append(u.id)
            return u.id
    yield make
    with app.app_context():
        User.query.filter(User.id.in_(created)).delete(synchronize_session=False)
        db.session.commit()


def _issue(sender, uid):
    """Send the email and return the token of its link."""
    sent = {}

    def capture(to, subject, html_body, text_body=None):
        sent['text'] = text_body
        return True

    with patch.object(email_service, 'is_smtp_configured', return_value=True), \
            patch.object(email_service, 'is_email_verification_enabled', return_value=True), \
            patch.object(email_service, '_send_email', side_effect=capture), \
            app.test_request_context('/'):
        assert sender(db.session.get(User, uid))
    m = re.search(re.escape(LINK[sender]) + r'([^\s"<]+)', sent['text'])
    assert m, sent['text']
    return m.group(1)


def _stored(uid, column):
    with app.app_context():
        return getattr(db.session.get(User, uid), column)


@pytest.mark.parametrize('sender', SENDERS)
def test_issued_token_is_stored_as_hash(user, sender):
    uid = user(email_verified=False)
    token = _issue(sender, uid)
    stored = _stored(uid, COLUMN[sender])
    assert stored == hash_token(token) and stored != token
    assert re.fullmatch(r'[0-9a-f]{64}', stored)


def test_original_token_completes_the_reset_once(user, csrf_off):
    uid = user()
    token = _issue(email_service.send_password_reset_email, uid)
    form = {'password': 'NewPassw0rd!', 'confirm_password': 'NewPassw0rd!'}
    client = app.test_client()
    resp = client.post(f'/reset-password/{token}', data=form)
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
    with app.app_context():
        u = db.session.get(User, uid)
        assert bcrypt.check_password_hash(u.password, 'NewPassw0rd!')
        assert u.password_reset_token is None
    again = client.post(f'/reset-password/{token}',
                        data={'password': 'Attacker1!x', 'confirm_password': 'Attacker1!x'})
    assert '/forgot-password' in again.headers['Location']
    with app.app_context():
        assert bcrypt.check_password_hash(db.session.get(User, uid).password, 'NewPassw0rd!')


def test_original_token_completes_the_verification_once(user):
    uid = user(email_verified=False)
    token = _issue(email_service.send_verification_email, uid)
    client = app.test_client()
    resp = client.get(f'/verify-email/{token}')
    assert resp.status_code == 200
    with app.app_context():
        u = db.session.get(User, uid)
        assert u.email_verified is True and u.email_verification_token is None


@pytest.mark.parametrize('sender', SENDERS)
def test_the_stored_hash_is_not_a_working_link(user, csrf_off, sender):
    """What a reader of the database sees cannot be used as the link."""
    uid = user(email_verified=False)
    _issue(sender, uid)
    stored = _stored(uid, COLUMN[sender])
    client = app.test_client()
    if sender is email_service.send_password_reset_email:
        resp = client.post(f'/reset-password/{stored}',
                           data={'password': 'Attacker1!x', 'confirm_password': 'Attacker1!x'})
        assert '/forgot-password' in resp.headers['Location']
        with app.app_context():
            assert bcrypt.check_password_hash(db.session.get(User, uid).password, 'OldPassw0rd!')
    else:
        client.get(f'/verify-email/{stored}')
        with app.app_context():
            assert db.session.get(User, uid).email_verified is False


def test_mismatched_reset_token_is_rejected(user, csrf_off):
    uid = user()
    token = _issue(email_service.send_password_reset_email, uid)
    with app.app_context():
        u = db.session.get(User, uid)
        u.password_reset_token = hash_token(token + 'newer')     # a newer request replaced it
        db.session.commit()
    resp = app.test_client().post(f'/reset-password/{token}',
                                  data={'password': 'NewPassw0rd!', 'confirm_password': 'NewPassw0rd!'})
    assert resp.status_code == 302 and '/forgot-password' in resp.headers['Location']
    with app.app_context():
        assert bcrypt.check_password_hash(db.session.get(User, uid).password, 'OldPassw0rd!')


@pytest.mark.parametrize('stored', [None, 'newer'])
def test_verification_link_is_bound_to_the_stored_hash(user, stored):
    """A verification link that was used, cleared or replaced verifies nothing."""
    uid = user(email_verified=False)
    token = _issue(email_service.send_verification_email, uid)
    with app.app_context():
        u = db.session.get(User, uid)
        u.email_verification_token = None if stored is None else hash_token(token + stored)
        db.session.commit()
    resp = app.test_client().get(f'/verify-email/{token}')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
    with app.app_context():
        assert db.session.get(User, uid).email_verified is False


def test_plaintext_tokens_of_an_earlier_version_are_removed(user):
    from src.init_db import _clear_plaintext_account_tokens
    old, kept = user(email_verified=False), user(email_verified=False)
    with app.app_context():
        signed_reset = email_service.generate_password_reset_token(old)
        signed_verify = email_service.generate_verification_token(old)
        db.session.get(User, old).password_reset_token = signed_reset
        db.session.get(User, old).email_verification_token = signed_verify
        db.session.get(User, kept).password_reset_token = hash_token('a')
        db.session.get(User, kept).email_verification_token = hash_token('b')
        db.session.commit()
        for _ in range(2):              # a second run changes nothing
            _clear_plaintext_account_tokens(db.engine, app)
            db.session.expire_all()
            assert db.session.get(User, old).password_reset_token is None
            assert db.session.get(User, old).email_verification_token is None
            assert db.session.get(User, kept).password_reset_token == hash_token('a')
            assert db.session.get(User, kept).email_verification_token == hash_token('b')


def test_cleanup_is_registered_once_in_the_ledger():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / 'src' / 'init_db.py').read_text(encoding='utf-8')
    assert "run_once(engine, '0011_clear_plaintext_account_tokens'" in source
    assert source.count("'0011_") == 1
    with app.app_context():
        assert db.session.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = "
            "'0011_clear_plaintext_account_tokens'")).scalar() == 1
