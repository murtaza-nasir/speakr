"""Password-reset tokens must be single-use and invalidated on supersede /
password change. Previously the itsdangerous token was only signature+expiry
checked, so a leaked link kept working for the full 1h TTL even after it had
been used or the password had already been changed.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt
from src.models import User
from src.services.email import generate_password_reset_token

app.config['WTF_CSRF_ENABLED'] = False


def _make_user():
    suffix = uuid.uuid4().hex[:8]
    u = User(username=f'reset_{suffix}', email=f'reset_{suffix}@local.test',
             password=bcrypt.generate_password_hash('OldPassw0rd!').decode())
    db.session.add(u)
    db.session.commit()
    return u


def _issue_token(user):
    token = generate_password_reset_token(user.id)
    user.password_reset_token = token
    db.session.commit()
    return token


def test_valid_token_resets_password_once():
    with app.app_context():
        user = _make_user()
        token = _issue_token(user)
        client = app.test_client()

        r = client.post(f'/reset-password/{token}',
                        data={'password': 'NewPassw0rd!', 'confirm_password': 'NewPassw0rd!'},
                        follow_redirects=False)
        assert r.status_code in (200, 302)
        db.session.refresh(user)
        assert bcrypt.check_password_hash(user.password, 'NewPassw0rd!')
        assert user.password_reset_token is None  # cleared on use

        # Reuse of the same token must now fail (password unchanged).
        r2 = client.post(f'/reset-password/{token}',
                         data={'password': 'Attacker1!', 'confirm_password': 'Attacker1!'},
                         follow_redirects=False)
        db.session.refresh(user)
        assert not bcrypt.check_password_hash(user.password, 'Attacker1!')
        assert bcrypt.check_password_hash(user.password, 'NewPassw0rd!')

        db.session.delete(user)
        db.session.commit()


def test_superseded_token_is_rejected():
    # A validly-signed older token that no longer matches the token currently
    # stored on the user (a newer reset request replaced it) must be rejected.
    # itsdangerous emits identical tokens within the same second, so we model
    # the supersede by storing a distinct current token directly rather than
    # racing the clock.
    with app.app_context():
        user = _make_user()
        old_token = generate_password_reset_token(user.id)  # validly signed
        user.password_reset_token = old_token + 'SUPERSEDED'  # newer stored token differs
        db.session.commit()
        client = app.test_client()

        r = client.post(f'/reset-password/{old_token}',
                        data={'password': 'ViaOld1!', 'confirm_password': 'ViaOld1!'},
                        follow_redirects=False)
        db.session.refresh(user)
        assert not bcrypt.check_password_hash(user.password, 'ViaOld1!')

        db.session.delete(user)
        db.session.commit()


def test_token_with_no_stored_counterpart_is_rejected():
    with app.app_context():
        user = _make_user()
        # A validly-signed token, but nothing stored on the user (never
        # requested, or already cleared).
        token = generate_password_reset_token(user.id)
        user.password_reset_token = None
        db.session.commit()
        client = app.test_client()

        r = client.post(f'/reset-password/{token}',
                        data={'password': 'NoStore1!', 'confirm_password': 'NoStore1!'},
                        follow_redirects=False)
        db.session.refresh(user)
        assert not bcrypt.check_password_hash(user.password, 'NoStore1!')

        db.session.delete(user)
        db.session.commit()
