"""Email normalization and case-insensitive matching.

Fixes the mobile-autofill "Invalid email address" failure (trailing space) and
makes account matching case-insensitive, without locking out any user whose
email was stored mixed-case. Motivated by PR #334 (Wladefant); reimplemented as
a case-insensitive lookup plus normalize-on-write so pre-existing mixed-case
accounts keep working.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt
from src.models import User

app.config['WTF_CSRF_ENABLED'] = False


def _mk_user(stored_email, password='Passw0rd!'):
    u = User(username=f'u_{uuid.uuid4().hex[:8]}', email=stored_email,
             password=bcrypt.generate_password_hash(password).decode(),
             email_verified=True)
    db.session.add(u)
    db.session.commit()
    return u


# --- helper unit behavior ---

def test_normalize_email_trims_and_lowercases():
    assert User.normalize_email('  Foo@Bar.COM ') == 'foo@bar.com'
    assert User.normalize_email(None) is None
    assert User.normalize_email('') == ''


def test_find_by_email_is_case_insensitive():
    with app.app_context():
        u = _mk_user('mixed_case_stored@example.com')
        try:
            assert User.find_by_email('MIXED_CASE_STORED@EXAMPLE.COM').id == u.id
            assert User.find_by_email('  mixed_case_stored@example.com  ').id == u.id
            assert User.find_by_email('nope@example.com') is None
        finally:
            db.session.delete(u); db.session.commit()


# --- login route ---

def _post_login(client, email, password='Passw0rd!'):
    return client.post('/login', data={'email': email, 'password': password},
                        follow_redirects=False)


def test_login_succeeds_with_trailing_space_from_autofill():
    with app.app_context():
        u = _mk_user('autofill@example.com')
        client = app.test_client()
        try:
            r = _post_login(client, 'autofill@example.com ')  # trailing space
            assert r.status_code == 302  # redirected into the app, not re-rendered
            with client.session_transaction() as s:
                assert s.get('_user_id') == str(u.id)
        finally:
            db.session.delete(u); db.session.commit()


def test_login_succeeds_with_mixed_case_input():
    with app.app_context():
        u = _mk_user('lower.stored@example.com')
        client = app.test_client()
        try:
            r = _post_login(client, 'Lower.Stored@Example.COM')
            assert r.status_code == 302
            with client.session_transaction() as s:
                assert s.get('_user_id') == str(u.id)
        finally:
            db.session.delete(u); db.session.commit()


def test_login_succeeds_for_preexisting_mixed_case_stored_user():
    """The regression guard: a user whose email row is stored mixed-case (from
    before normalization existed, or via SSO) must still be able to log in.
    Lowercasing the input alone would have locked this user out."""
    with app.app_context():
        u = _mk_user('Legacy.MixedCase@Example.com')  # stored with capitals
        client = app.test_client()
        try:
            r = _post_login(client, 'legacy.mixedcase@example.com')  # typed lowercase
            assert r.status_code == 302
            with client.session_transaction() as s:
                assert s.get('_user_id') == str(u.id)
        finally:
            db.session.delete(u); db.session.commit()


def test_login_still_rejects_syntactically_invalid_email():
    with app.app_context():
        client = app.test_client()
        r = _post_login(client, 'not-an-email')
        assert r.status_code == 200  # form re-rendered, no session
        with client.session_transaction() as s:
            assert '_user_id' not in s


# --- registration form: normalize-on-write + case-insensitive uniqueness ---

def test_registration_form_normalizes_email_before_storage():
    from src.api.auth import RegistrationForm
    with app.test_request_context('/register', method='POST', data={
            'username': 'newbie', 'email': '  New.User@Example.COM ',
            'password': 'Passw0rd!', 'confirm_password': 'Passw0rd!'}):
        form = RegistrationForm()
        # The field filter runs on construction, so what gets stored is normalized.
        assert form.email.data == 'new.user@example.com'


def test_registration_rejects_case_variant_of_existing_email():
    from src.api.auth import RegistrationForm
    with app.app_context():
        u = _mk_user('taken@example.com')
        try:
            with app.test_request_context('/register', method='POST', data={
                    'username': 'someone', 'email': 'TAKEN@Example.com',
                    'password': 'Passw0rd!', 'confirm_password': 'Passw0rd!'}):
                form = RegistrationForm()
                assert not form.validate()
                assert any('already registered' in e for e in form.email.errors)
        finally:
            db.session.delete(u); db.session.commit()
