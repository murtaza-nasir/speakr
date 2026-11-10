"""Addresses on special-use domains such as .local (#551).

email_validator refuses special-use domains whatever its options. Registration
accepts such an address when its domain is listed in REGISTRATION_ALLOWED_DOMAINS,
and sign-in accepts any well-formed address, so an account an administrator
created on an internal domain can sign in.
"""
import contextlib
import os
import uuid
from unittest import mock

import pytest

from src.app import app, db
from src.api import auth as auth_mod
from src.models import User, SystemSetting

PW = "Reg1ster-Pw!"


@pytest.fixture(autouse=True)
def _no_csrf():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = prev


@contextlib.contextmanager
def _env(**overrides):
    saved = {k: os.environ.get(k) for k in overrides}
    _ENV_TO_SETTING = {
        'ALLOW_REGISTRATION': 'allow_registration',
        'REGISTRATION_ALLOWED_DOMAINS': 'registration_allowed_domains',
    }
    setting_saved = {}
    ctx = app.app_context()
    ctx.push()
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in overrides.items():
            if k in _ENV_TO_SETTING:
                key = _ENV_TO_SETTING[k]
                row = SystemSetting.query.filter_by(key=key).first()
                setting_saved[k] = row.value if row else None
                if v is None:
                    SystemSetting.query.filter_by(key=key).delete()
                else:
                    SystemSetting.set_setting(
                        key, v,
                        setting_type='string' if key == 'registration_allowed_domains' else 'boolean')
        db.session.commit()
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, old in setting_saved.items():
            key = _ENV_TO_SETTING[k]
            if old is None:
                SystemSetting.query.filter_by(key=key).delete()
            else:
                SystemSetting.set_setting(
                    key, old,
                    setting_type='string' if key == 'registration_allowed_domains' else 'boolean')
        db.session.commit()
        ctx.pop()


@pytest.fixture
def emails():
    created = []
    with app.app_context():
        yield created
        for u in User.query.filter(User.email.in_(created or [''])).all():
            db.session.delete(u)
        db.session.commit()


def _register(email, allowed):
    with _env(ALLOW_REGISTRATION='true', REGISTRATION_ALLOWED_DOMAINS=allowed), \
            mock.patch.object(auth_mod, 'is_email_verification_enabled', return_value=False):
        return app.test_client().post('/register', data={
            'username': "loc_" + uuid.uuid4().hex[:10], 'email': email,
            'password': PW, 'confirm_password': PW,
        })


def test_a_listed_local_domain_can_register(emails):
    email = uuid.uuid4().hex[:8] + "@organization.local"
    emails.append(email)
    resp = _register(email, 'organization.local')
    assert resp.status_code == 302
    assert User.query.filter_by(email=email).first() is not None


def test_a_listed_local_domain_matches_in_any_letter_case(emails):
    email = uuid.uuid4().hex[:8] + "@Organization.Local"
    emails.append(email.lower())
    resp = _register(email, 'ORGANIZATION.local')
    assert resp.status_code == 302
    assert User.query.filter_by(email=email.lower()).first() is not None


@pytest.mark.parametrize("allowed", [None, 'other.local', 'example.com'])
def test_a_local_domain_that_is_not_listed_is_refused(emails, allowed):
    email = uuid.uuid4().hex[:8] + "@organization.local"
    emails.append(email)
    resp = _register(email, allowed)
    assert resp.status_code == 200
    assert User.query.filter_by(email=email).first() is None


@pytest.mark.parametrize("email", ["two words@organization.local", "@organization.local",
                                   "a..b@organization.local", "user@organization..local",
                                   "user@-bad.local"])
def test_a_malformed_address_on_a_listed_domain_is_refused(emails, email):
    emails.append(email)
    resp = _register(email, 'organization.local,bad.local,-bad.local')
    assert resp.status_code == 200
    assert User.query.filter_by(email=email).first() is None


def test_an_ordinary_domain_still_registers(emails):
    email = uuid.uuid4().hex[:8] + "@example.com"
    emails.append(email)
    assert _register(email, None).status_code == 302
    assert User.query.filter_by(email=email).first() is not None


def test_an_account_on_a_local_domain_can_sign_in(emails):
    email = uuid.uuid4().hex[:8] + "@organization.local"
    emails.append(email)
    hashed = auth_mod.bcrypt.generate_password_hash(PW).decode('utf-8')
    user = User(username="loc_" + uuid.uuid4().hex[:10], email=email, password=hashed, email_verified=True)
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    with _env(REGISTRATION_ALLOWED_DOMAINS=None):
        resp = client.post('/login', data={'email': email, 'password': PW})
    assert resp.status_code == 302
    with client.session_transaction() as s:
        assert s.get('_user_id') == str(user.id)


def test_sign_in_still_refuses_a_malformed_address():
    with app.app_context():
        resp = app.test_client().post('/login', data={'email': 'not an address', 'password': PW})
    assert resp.status_code == 200
