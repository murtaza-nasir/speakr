#!/usr/bin/env python3
"""Coverage tests for src/api/auth.py — the auth/login/account security boundary.

These exercise the HTTP-facing auth blueprint: login (success/failure/logout),
the /account handler's two distinct persistence branches (personal-info vs
preferences — the branch where a past bug routed ``ui_language`` to the wrong
column), password change (success + wrong-current-password rejection),
registration gating (ALLOW_REGISTRATION + REGISTRATION_ALLOWED_DOMAINS), the
SSO callback paths (OIDC client mocked), and auth-required routes redirecting
when unauthenticated.

Hermetic & offline: conftest.py points the app at a throwaway SQLite DB and
sets safe env defaults; SSO/OIDC and SMTP effects are patched. CSRF is disabled
per test_client.

Run (pytest, isolated temp DB from conftest.py):
    python -m pytest tests/test_cov_auth.py -q
"""

import os
import sys
import uuid
import contextlib
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import User  # noqa: E402
from src.api import auth as auth_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_csrf():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = prev


@pytest.fixture
def app_ctx():
    with app.app_context():
        yield


@pytest.fixture
def cleanup_users(app_ctx):
    created = {"usernames": set(), "emails": set(), "subjects": set()}
    yield created
    q = User.query.filter(
        db.or_(
            User.username.in_(created["usernames"] or [""]),
            User.email.in_(created["emails"] or [""]),
            User.sso_subject.in_(created["subjects"] or [""]),
        )
    )
    for u in q.all():
        db.session.delete(u)
    db.session.commit()


# Plaintext password used for login tests; hashed via the same bcrypt the app
# verifies with so check_password_hash succeeds.
GOOD_PW = "Sup3rSecret!"


def _track(cleanup, user):
    cleanup["usernames"].add(user.username)
    cleanup["emails"].add(user.email)
    if user.sso_subject:
        cleanup["subjects"].add(user.sso_subject)


def _mk_user(cleanup, password=GOOD_PW, sso_subject=None, is_admin=False,
             email_verified=True, **extra):
    username = "u_" + uuid.uuid4().hex[:14]
    email = uuid.uuid4().hex[:10] + "@example.com"
    hashed = auth_mod.bcrypt.generate_password_hash(password).decode('utf-8') if password else None
    u = User(username=username, email=email, password=hashed, sso_subject=sso_subject,
             is_admin=is_admin, email_verified=email_verified, **extra)
    db.session.add(u)
    db.session.commit()
    _track(cleanup, u)
    return u


@contextlib.contextmanager
def _client_logged_in_as(user):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True
    yield client


@contextlib.contextmanager
def _envset(**overrides):
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

def test_login_success_sets_session_and_redirects(cleanup_users):
    user = _mk_user(cleanup_users)
    client = app.test_client()
    resp = client.post('/login', data={'email': user.email, 'password': GOOD_PW})
    assert resp.status_code == 302
    assert '/login' not in resp.headers['Location']
    with client.session_transaction() as s:
        assert s.get('_user_id') == str(user.id)


def test_login_wrong_password_no_session(cleanup_users):
    user = _mk_user(cleanup_users)
    client = app.test_client()
    resp = client.post('/login', data={'email': user.email, 'password': 'wrong-password'})
    assert resp.status_code == 200  # re-renders login form
    with client.session_transaction() as s:
        assert '_user_id' not in s


def test_login_unknown_user_no_session(cleanup_users):
    client = app.test_client()
    resp = client.post('/login', data={
        'email': uuid.uuid4().hex[:8] + '@nobody.com', 'password': GOOD_PW})
    assert resp.status_code == 200
    with client.session_transaction() as s:
        assert '_user_id' not in s


def test_login_respects_safe_next(cleanup_users):
    user = _mk_user(cleanup_users)
    client = app.test_client()
    resp = client.post('/login?next=/recordings', data={'email': user.email, 'password': GOOD_PW})
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/recordings')


def test_login_rejects_unsafe_next(cleanup_users):
    user = _mk_user(cleanup_users)
    client = app.test_client()
    resp = client.post('/login?next=//evil.com', data={'email': user.email, 'password': GOOD_PW})
    assert resp.status_code == 302
    assert 'evil.com' not in resp.headers['Location']


def test_login_when_already_authenticated_redirects(cleanup_users):
    user = _mk_user(cleanup_users)
    with _client_logged_in_as(user) as client:
        resp = client.get('/login')
        assert resp.status_code == 302


def test_logout_clears_session(cleanup_users):
    user = _mk_user(cleanup_users)
    with _client_logged_in_as(user) as client:
        resp = client.get('/logout')
        assert resp.status_code == 302
        with client.session_transaction() as s:
            assert '_user_id' not in s


def test_login_blocked_when_email_unverified(cleanup_users):
    # auth.py:205 — verification required AND user not verified ⇒ login
    # is gated: the check-email page renders and no session is established.
    user = _mk_user(cleanup_users, email_verified=False)
    with mock.patch.object(auth_mod, 'is_email_verification_required', return_value=True):
        client = app.test_client()
        resp = client.post('/login', data={'email': user.email, 'password': GOOD_PW})
        assert resp.status_code == 200  # check_email page, not a login redirect
        with client.session_transaction() as s:
            assert '_user_id' not in s


def test_login_succeeds_when_email_verified(cleanup_users):
    # The companion: even with verification required, a VERIFIED user logs
    # in. This kills the and->or mutation on auth.py:205 (under `or`, a
    # verified user would be wrongly blocked).
    user = _mk_user(cleanup_users, email_verified=True)
    with mock.patch.object(auth_mod, 'is_email_verification_required', return_value=True):
        client = app.test_client()
        resp = client.post('/login', data={'email': user.email, 'password': GOOD_PW})
        assert resp.status_code == 302
        with client.session_transaction() as s:
            assert s.get('_user_id') == str(user.id)


def test_login_sso_only_user_prompted_to_use_sso(cleanup_users):
    # User with no local password → account uses SSO, password login refused.
    user = _mk_user(cleanup_users, password=None, sso_subject="sub-" + uuid.uuid4().hex)
    client = app.test_client()
    resp = client.post('/login', data={'email': user.email, 'password': GOOD_PW})
    assert resp.status_code == 200
    with client.session_transaction() as s:
        assert '_user_id' not in s


# ---------------------------------------------------------------------------
# /account — personal-info branch vs preferences branch
# ---------------------------------------------------------------------------

def test_account_requires_login():
    client = app.test_client()
    resp = client.get('/account')
    assert resp.status_code in (302, 401)
    if resp.status_code == 302:
        assert '/login' in resp.headers['Location']


def test_account_get_renders_for_logged_in_user(cleanup_users):
    user = _mk_user(cleanup_users)
    with _client_logged_in_as(user) as client:
        resp = client.get('/account')
        assert resp.status_code == 200


def test_account_personal_info_branch_persists_name_fields(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/account', data={
            'user_name': 'Jane Doe',
            'user_job_title': 'Engineer',
            'user_company': 'Acme',
        })
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    assert refreshed.name == 'Jane Doe'
    assert refreshed.job_title == 'Engineer'
    assert refreshed.company == 'Acme'


def test_account_personal_info_branch_does_not_touch_language(cleanup_users):
    # The personal-info branch must NOT write ui_language (past-bug guard).
    user = _mk_user(cleanup_users, ui_language='fr')
    uid = user.id
    with _client_logged_in_as(user) as client:
        client.post('/account', data={'user_name': 'Bob'})
    refreshed = db.session.get(User, uid)
    assert refreshed.ui_language == 'fr'  # unchanged
    assert refreshed.name == 'Bob'


def test_account_preferences_branch_persists_languages(cleanup_users):
    user = _mk_user(cleanup_users, ui_language='en')
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/account', data={
            'preferences_form': '1',
            'ui_language': 'es',
            'transcription_language': 'fr',
            'output_language': 'Spanish',
            'show_timestamps_simple_view': 'on',
            'editor_autosave': 'on',
            'audio_player_position': 'top',
        })
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    # ui_language goes to ui_language (NOT transcription_language) — the bug.
    assert refreshed.ui_language == 'es'
    assert refreshed.transcription_language == 'fr'
    assert refreshed.output_language == 'Spanish'
    assert refreshed.show_timestamps_simple_view is True
    assert refreshed.editor_autosave is True
    assert refreshed.audio_player_position == 'top'


def test_account_preferences_branch_normalizes_legacy_language_name(cleanup_users):
    # Legacy display-name transcription language must be normalized to a code.
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        client.post('/account', data={
            'preferences_form': '1',
            'ui_language': 'en',
            'transcription_language': 'Français',
        })
    refreshed = db.session.get(User, uid)
    assert refreshed.transcription_language == 'fr'


def test_account_preferences_invalid_audio_position_falls_back(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        client.post('/account', data={
            'preferences_form': '1',
            'ui_language': 'en',
            'audio_player_position': 'sideways',
        })
    refreshed = db.session.get(User, uid)
    assert refreshed.audio_player_position == 'bottom'


def test_account_prompts_branch_persists_summary_prompt(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/account', data={
            'summary_prompt': 'Summarize tightly.',
            'extract_events': 'on',
            'transcription_hotwords': 'Speakr',
        })
        assert resp.status_code == 302
        assert '#prompts' in resp.headers['Location']
    refreshed = db.session.get(User, uid)
    assert refreshed.summary_prompt == 'Summarize tightly.'
    assert refreshed.extract_events is True
    assert refreshed.transcription_hotwords == 'Speakr'


def test_account_ajax_returns_json(cleanup_users):
    user = _mk_user(cleanup_users)
    with _client_logged_in_as(user) as client:
        resp = client.post('/account', data={'user_name': 'Ajax'},
                           headers={'X-Requested-With': 'XMLHttpRequest'})
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------

def test_change_password_success(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    new_pw = "Brand-New-Pw9!"
    with _client_logged_in_as(user) as client:
        resp = client.post('/change_password', data={
            'current_password': GOOD_PW,
            'new_password': new_pw,
            'confirm_password': new_pw,
        })
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    assert auth_mod.bcrypt.check_password_hash(refreshed.password, new_pw)
    assert not auth_mod.bcrypt.check_password_hash(refreshed.password, GOOD_PW)


def test_change_password_wrong_current_rejected(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/change_password', data={
            'current_password': 'not-the-real-one',
            'new_password': "Another-Pw9!",
            'confirm_password': "Another-Pw9!",
        })
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    # Password unchanged.
    assert auth_mod.bcrypt.check_password_hash(refreshed.password, GOOD_PW)


def test_change_password_mismatch_confirmation_rejected(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        client.post('/change_password', data={
            'current_password': GOOD_PW,
            'new_password': "Another-Pw9!",
            'confirm_password': "Different-Pw9!",
        })
    refreshed = db.session.get(User, uid)
    assert auth_mod.bcrypt.check_password_hash(refreshed.password, GOOD_PW)


def test_change_password_weak_new_rejected(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        client.post('/change_password', data={
            'current_password': GOOD_PW,
            'new_password': "weak",
            'confirm_password': "weak",
        })
    refreshed = db.session.get(User, uid)
    assert auth_mod.bcrypt.check_password_hash(refreshed.password, GOOD_PW)


def test_change_password_sso_only_user_redirected_to_reset(cleanup_users):
    # GHSA-x4q4-3ww4-h329: SSO-only (no local password) cannot set password here.
    user = _mk_user(cleanup_users, password=None, sso_subject="sub-" + uuid.uuid4().hex)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/change_password', data={
            'new_password': "Brand-New-Pw9!",
            'confirm_password': "Brand-New-Pw9!",
        })
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    assert refreshed.password is None  # no password was set


def test_change_password_requires_login():
    client = app.test_client()
    resp = client.post('/change_password', data={})
    assert resp.status_code in (302, 401)


# ---------------------------------------------------------------------------
# Registration gating
# ---------------------------------------------------------------------------

def test_register_blocked_when_disabled(cleanup_users):
    with _envset(ALLOW_REGISTRATION='false'):
        client = app.test_client()
        resp = client.get('/register')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_register_allowed_creates_user(cleanup_users):
    username = "reg_" + uuid.uuid4().hex[:10]
    email = uuid.uuid4().hex[:10] + "@example.com"
    cleanup_users["usernames"].add(username)
    cleanup_users["emails"].add(email)
    pw = "Reg1ster-Pw!"
    # Force verification off so the success branch redirects to login.
    with _envset(ALLOW_REGISTRATION='true'), \
            mock.patch.object(auth_mod, 'is_email_verification_enabled', return_value=False):
        client = app.test_client()
        resp = client.post('/register', data={
            'username': username,
            'email': email,
            'password': pw,
            'confirm_password': pw,
        })
        assert resp.status_code == 302
    created = User.query.filter_by(email=email).first()
    assert created is not None
    assert created.email_verified is True  # verification disabled → verified


def test_register_blocked_by_disallowed_domain(cleanup_users):
    username = "reg_" + uuid.uuid4().hex[:10]
    email = uuid.uuid4().hex[:10] + "@evil.com"
    pw = "Reg1ster-Pw!"
    with _envset(ALLOW_REGISTRATION='true', REGISTRATION_ALLOWED_DOMAINS='example.com'), \
            mock.patch.object(auth_mod, 'is_email_verification_enabled', return_value=False):
        client = app.test_client()
        resp = client.post('/register', data={
            'username': username,
            'email': email,
            'password': pw,
            'confirm_password': pw,
        })
        assert resp.status_code == 200  # re-renders register with flash
    assert User.query.filter_by(email=email).first() is None


def test_is_registration_domain_allowed_helper(app_ctx):
    with _envset(REGISTRATION_ALLOWED_DOMAINS='example.com'):
        assert auth_mod.is_registration_domain_allowed('a@example.com') is True
        assert auth_mod.is_registration_domain_allowed('a@evil.com') is False
        assert auth_mod.is_registration_domain_allowed('') is False
        assert auth_mod.is_registration_domain_allowed('no-at-sign') is False
    with _envset(REGISTRATION_ALLOWED_DOMAINS=None):
        assert auth_mod.is_registration_domain_allowed('anyone@anywhere.com') is True


# ---------------------------------------------------------------------------
# SSO callback paths (OIDC client mocked)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _sso_on():
    with mock.patch.object(auth_mod, 'is_sso_enabled', return_value=True), \
            mock.patch.object(auth_mod, 'init_sso_client', return_value=mock.MagicMock()), \
            mock.patch.object(auth_mod, 'get_sso_config',
                              return_value={'provider_name': 'Keycloak', 'redirect_uri': 'https://app/cb'}):
        yield


def test_sso_callback_disabled_redirects_to_login():
    with mock.patch.object(auth_mod, 'is_sso_enabled', return_value=False):
        client = app.test_client()
        resp = client.get('/auth/sso/callback')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_sso_callback_success_logs_in_user(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    user = _mk_user(cleanup_users, password=None, sso_subject=sub)
    oauth = mock.MagicMock()
    oauth.sso.authorize_access_token.return_value = {'userinfo': {'sub': sub}}
    with _sso_on(), \
            mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth), \
            mock.patch.object(auth_mod, 'create_or_update_sso_user', return_value=user):
        client = app.test_client()
        resp = client.get('/auth/sso/callback')
        assert resp.status_code == 302
        assert '/login' not in resp.headers['Location']
        with client.session_transaction() as s:
            assert s.get('_user_id') == str(user.id)


def test_sso_callback_token_error_redirects_to_login():
    oauth = mock.MagicMock()
    oauth.sso.authorize_access_token.side_effect = Exception("boom")
    with _sso_on(), mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth):
        client = app.test_client()
        resp = client.get('/auth/sso/callback')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_sso_callback_missing_subject_redirects_to_login():
    oauth = mock.MagicMock()
    oauth.sso.authorize_access_token.return_value = {'userinfo': {}}  # no 'sub'
    with _sso_on(), mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth):
        client = app.test_client()
        resp = client.get('/auth/sso/callback')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_sso_callback_permission_error_redirects_to_login():
    oauth = mock.MagicMock()
    oauth.sso.authorize_access_token.return_value = {'userinfo': {'sub': 'sub-' + uuid.uuid4().hex}}
    with _sso_on(), \
            mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth), \
            mock.patch.object(auth_mod, 'create_or_update_sso_user',
                              side_effect=PermissionError("domain not allowed")):
        client = app.test_client()
        resp = client.get('/auth/sso/callback')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_sso_callback_links_to_target_user(cleanup_users):
    target = _mk_user(cleanup_users)
    uid = target.id
    sub = "sub-" + uuid.uuid4().hex
    oauth = mock.MagicMock()
    oauth.sso.authorize_access_token.return_value = {'userinfo': {'sub': sub, 'name': 'Linked'}}
    with _sso_on(), \
            mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth), \
            mock.patch.object(auth_mod, 'update_user_profile_from_claims'):
        client = app.test_client()
        with client.session_transaction() as s:
            s['_user_id'] = str(uid)
            s['_fresh'] = True
            s['sso_link_user_id'] = uid
        resp = client.get('/auth/sso/callback')
        assert resp.status_code == 302
        assert '/account' in resp.headers['Location']
    refreshed = db.session.get(User, uid)
    cleanup_users["subjects"].add(sub)
    assert refreshed.sso_subject == sub
    assert refreshed.sso_provider == 'Keycloak'


def test_sso_login_disabled_redirects():
    with mock.patch.object(auth_mod, 'is_sso_enabled', return_value=False):
        client = app.test_client()
        resp = client.get('/auth/sso/login')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


def test_sso_login_does_not_store_unsafe_next(cleanup_users):
    # auth.py:249 open-redirect guard — a dangerous ?next must NOT be
    # persisted to session['sso_next'] (it would be honoured after the
    # IdP round-trip). Both an absolute and a scheme-relative target.
    oauth = mock.MagicMock()
    oauth.sso.authorize_redirect.return_value = 'redirecting'  # valid view return
    with _sso_on(), mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth):
        for evil in ('http://evil.com', '//evil.com'):
            client = app.test_client()
            resp = client.get('/auth/sso/login', query_string={'next': evil})
            assert resp.status_code == 200
            with client.session_transaction() as s:
                assert s.get('sso_next') != evil
                assert 'sso_next' not in s


def test_sso_login_stores_safe_next(cleanup_users):
    # The safe-path companion: a local relative target IS stored.
    oauth = mock.MagicMock()
    oauth.sso.authorize_redirect.return_value = 'redirecting'
    with _sso_on(), mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth):
        client = app.test_client()
        resp = client.get('/auth/sso/login', query_string={'next': '/recordings'})
        assert resp.status_code == 200
        with client.session_transaction() as s:
            assert s.get('sso_next') == '/recordings'


def test_sso_unlink_with_password(cleanup_users):
    user = _mk_user(cleanup_users, sso_subject="sub-" + uuid.uuid4().hex, sso_provider='Keycloak')
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/auth/sso/unlink')
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    assert refreshed.sso_subject is None


def test_sso_unlink_without_password_blocked(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    user = _mk_user(cleanup_users, password=None, sso_subject=sub)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/auth/sso/unlink')
        assert resp.status_code == 302
    refreshed = db.session.get(User, uid)
    assert refreshed.sso_subject == sub  # still linked


# ---------------------------------------------------------------------------
# JSON preference endpoints (login-required)
# ---------------------------------------------------------------------------

def test_update_auto_summarization_requires_login():
    client = app.test_client()
    resp = client.post('/api/user/auto-summarization', json={'enabled': False})
    assert resp.status_code in (302, 401)


def test_update_auto_summarization_persists(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/api/user/auto-summarization', json={'enabled': False})
        assert resp.status_code == 200
        assert resp.get_json()['auto_summarization'] is False
    assert db.session.get(User, uid).auto_summarization is False


def test_update_auto_speaker_labelling_invalid_threshold(cleanup_users):
    user = _mk_user(cleanup_users)
    with _client_logged_in_as(user) as client:
        resp = client.post('/api/user/auto-speaker-labelling',
                           json={'enabled': True, 'threshold': 'bogus'})
        assert resp.status_code == 400


def test_update_auto_speaker_labelling_persists(cleanup_users):
    user = _mk_user(cleanup_users)
    uid = user.id
    with _client_logged_in_as(user) as client:
        resp = client.post('/api/user/auto-speaker-labelling',
                           json={'enabled': True, 'threshold': 'high'})
        assert resp.status_code == 200
    refreshed = db.session.get(User, uid)
    assert refreshed.auto_speaker_labelling is True
    assert refreshed.auto_speaker_labelling_threshold == 'high'


# ---------------------------------------------------------------------------
# Registration-domain parsing edge cases (is_registration_domain_allowed)
# ---------------------------------------------------------------------------

def test_registration_domain_empty_env_allows_all(app_ctx):
    # auth.py:112-113 — REGISTRATION_ALLOWED_DOMAINS unset/empty ⇒ no
    # restriction ⇒ every domain is allowed.
    with _envset(REGISTRATION_ALLOWED_DOMAINS=''):
        assert auth_mod.is_registration_domain_allowed('a@anywhere.com') is True
        assert auth_mod.is_registration_domain_allowed('b@evil.example') is True


def test_registration_domain_whitespace_env_allows_all(app_ctx):
    # auth.py:112 — a whitespace-only value strips to nothing ⇒ still no
    # restriction. (The `or not domains_env.strip()` clause covers this.)
    with _envset(REGISTRATION_ALLOWED_DOMAINS='   '):
        assert auth_mod.is_registration_domain_allowed('a@anywhere.com') is True


def test_registration_domain_commas_only_allows_all(app_ctx):
    # auth.py:115-117 — a value with content but no real domains after the
    # comma split (so `allowed` is empty) ⇒ treated as no restriction ⇒ True.
    # This reaches line 117 specifically; `return True`->`return False` here
    # would wrongly reject every registration.
    with _envset(REGISTRATION_ALLOWED_DOMAINS=', ,'):
        assert auth_mod.is_registration_domain_allowed('a@anywhere.com') is True
        assert auth_mod.is_registration_domain_allowed('b@example.com') is True


def test_registration_domain_allowlist_in_and_out(app_ctx):
    # auth.py:119-124 — a real allowlist gates by domain: in-list True,
    # out-of-list False. Case-insensitive on both env and email side.
    with _envset(REGISTRATION_ALLOWED_DOMAINS='Example.com, corp.net'):
        assert auth_mod.is_registration_domain_allowed('user@example.com') is True
        assert auth_mod.is_registration_domain_allowed('user@CORP.NET') is True
        assert auth_mod.is_registration_domain_allowed('user@evil.com') is False


# ---------------------------------------------------------------------------
# SSO client initialization on the login route (auth.py:243-244)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _sso_enabled_with_config():
    """SSO enabled + a usable config, WITHOUT patching the client helpers so
    each test controls get_sso_client / init_sso_client itself."""
    with mock.patch.object(auth_mod, 'is_sso_enabled', return_value=True), \
            mock.patch.object(auth_mod, 'get_sso_config',
                              return_value={'provider_name': 'Keycloak',
                                            'redirect_uri': 'https://app/cb'}):
        yield


def test_sso_login_uses_cached_client_without_reinit():
    # auth.py:243 — when get_sso_client() returns a client, the `or` must
    # short-circuit and init_sso_client must NOT be called. The `or`->`and`
    # mutation would call init_sso_client (and use ITS return value instead).
    oauth = mock.MagicMock()
    oauth.sso.authorize_redirect.return_value = 'redirecting'
    with _sso_enabled_with_config(), \
            mock.patch.object(auth_mod, 'get_sso_client', return_value=oauth), \
            mock.patch.object(auth_mod, 'init_sso_client') as init_mock:
        client = app.test_client()
        resp = client.get('/auth/sso/login')
        assert resp.status_code == 200
        init_mock.assert_not_called()
        oauth.sso.authorize_redirect.assert_called_once()


def test_sso_login_inits_client_when_none_cached():
    # auth.py:243 — get_sso_client() None ⇒ init_sso_client IS invoked and its
    # client is used. Under `or`->`and`, `None and init_sso_client(...)`
    # short-circuits, init is never called, and the route bails to /login.
    oauth = mock.MagicMock()
    oauth.sso.authorize_redirect.return_value = 'redirecting'
    with _sso_enabled_with_config(), \
            mock.patch.object(auth_mod, 'get_sso_client', return_value=None), \
            mock.patch.object(auth_mod, 'init_sso_client', return_value=oauth) as init_mock:
        client = app.test_client()
        resp = client.get('/auth/sso/login')
        assert resp.status_code == 200
        init_mock.assert_called_once()
        oauth.sso.authorize_redirect.assert_called_once()


def test_sso_login_bails_to_login_when_client_unavailable():
    # auth.py:244 — when neither a cached nor a freshly-initialized client is
    # available (both None), `if not oauth` must redirect to login. The
    # `if not oauth`->`if oauth` mutation would instead fall through and
    # dereference None (no 302-to-login).
    with _sso_enabled_with_config(), \
            mock.patch.object(auth_mod, 'get_sso_client', return_value=None), \
            mock.patch.object(auth_mod, 'init_sso_client', return_value=None):
        client = app.test_client()
        resp = client.get('/auth/sso/login')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
