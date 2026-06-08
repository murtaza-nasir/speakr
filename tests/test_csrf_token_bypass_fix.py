"""Regression tests for GHSA-x4q4-3ww4-h329 (Irina Iarlykanova).

The advisory documented two coupled bugs:

A. ``csrf_exempt_for_api_tokens`` called ``csrf.exempt(view_func)`` from
   a before_request hook, mutating Flask-WTF's process-global
   ``_exempt_views`` set on every request that looked like it carried a
   token. That mutation persisted for the lifetime of the worker.

B. ``is_token_authenticated()`` returned True for the mere presence of a
   token value (header or ``?token=``), never validating the value
   against the DB. So any cross-origin GET with ``?token=anything``
   would silently disable CSRF protection on the targeted endpoint for
   the rest of the worker's lifetime.

The fix introduced:

1. ``load_user_from_token_headers_only()`` — DB-validated, header-only.
2. A per-request ``csrf_token_aware_check`` before_request hook that
   calls ``csrf.protect()`` unless a valid header token is present.
3. ``WTF_CSRF_CHECK_DEFAULT = False`` so Flask-WTF's own auto-check
   does not double-run.

These tests assert that:

- ``?token=anything`` no longer bypasses CSRF.
- ``X-API-Token: anything`` no longer bypasses CSRF.
- A valid header token DOES bypass CSRF (preserving the legitimate
  automation use case).
- Crucially, an unsuccessful bypass attempt does NOT add the target
  view to ``csrf._exempt_views``. The exempt set is registration-time
  immutable from a request handler's perspective.
- ``/account/change_password`` refuses to act on SSO-only accounts.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, csrf
from src.models import User, APIToken
from src.utils.token_auth import hash_token


def _make_user(prefix, **overrides):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password=overrides.pop('password', 'placeholder-bcrypt-hash'),
        **overrides,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_token(user, value=None):
    plaintext = value or uuid.uuid4().hex
    api_token = APIToken(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        name='test',
    )
    db.session.add(api_token)
    db.session.commit()
    return plaintext, api_token


def _login_session(client, user):
    """Plant a session cookie matching `user` and clear Flask-Login's
    per-app-context user cache."""
    from flask import g
    with client.session_transaction() as sess:
        sess.clear()
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    try:
        g.pop('_login_user', None)
    except RuntimeError:
        pass


def _exempt_views_snapshot():
    return set(csrf._exempt_views)


def test_query_string_fake_token_does_not_bypass_csrf():
    """Attack 2 from the advisory: an attacker's cross-origin GET sends
    ?token=any-value to silently disable CSRF for the endpoint. Must
    fail under the fix."""
    # Don't disable CSRF; we want to exercise the production path
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('csrf_qstr')
            client = app.test_client()
            _login_session(client, user)

            # 1. The "disable CSRF" GET request the attacker would send.
            #    This used to mutate csrf._exempt_views permanently.
            before = _exempt_views_snapshot()
            client.get('/account?token=any-bogus-value')
            after = _exempt_views_snapshot()

            # The exempt set must be unchanged.
            assert before == after, (
                'csrf._exempt_views was mutated by a request with a bogus '
                'query-string token; the fix did not stick.'
            )

            # 2. A follow-up POST without CSRF token must STILL be rejected.
            resp = client.post(
                '/account',
                data={'summary_prompt': 'EXPLOIT_PROOF'},
                follow_redirects=False,
            )
            assert resp.status_code == 400, (
                f'Expected 400 (CSRF rejected), got {resp.status_code}'
            )

            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_header_with_fake_token_does_not_bypass_csrf():
    """Attack 1 variant: a bogus value in X-API-Token must not exempt."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('csrf_hdr')
            client = app.test_client()
            _login_session(client, user)

            before = _exempt_views_snapshot()
            resp = client.post(
                '/account',
                data={'summary_prompt': 'EXPLOIT_PROOF'},
                headers={'X-API-Token': 'totally-bogus'},
                follow_redirects=False,
            )
            after = _exempt_views_snapshot()
            assert before == after, 'exempt set mutated by bogus header token'
            assert resp.status_code == 400

            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_valid_header_token_bypasses_csrf_for_this_request_only():
    """The legitimate use case: a real API token in a header lets
    automation tools POST without a CSRF token. The bypass is per-request
    and does not mutate csrf._exempt_views."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('csrf_valid')
            plaintext, _ = _make_token(user)
            client = app.test_client()
            _login_session(client, user)

            before = _exempt_views_snapshot()
            resp = client.post(
                '/account',
                data={'summary_prompt': 'API-DRIVEN-CHANGE'},
                headers={'X-API-Token': plaintext},
                follow_redirects=False,
            )
            after = _exempt_views_snapshot()

            # The set must still be unchanged: no mutation from a
            # request handler is the whole point of the fix.
            assert before == after
            # 302 redirect on success (account POST redirects).
            assert resp.status_code in (200, 302), resp.data

            # And the very next request without a token must again be
            # subject to CSRF.
            _login_session(client, user)
            resp2 = client.post(
                '/account',
                data={'summary_prompt': 'NO_TOKEN_NO_CSRF'},
                follow_redirects=False,
            )
            assert resp2.status_code == 400, (
                'Endpoint stayed exempt after a token-authenticated '
                'request — the bypass was not properly per-request.'
            )

            # Clean up
            for t in APIToken.query.filter_by(user_id=user.id).all():
                db.session.delete(t)
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_valid_query_string_token_does_not_bypass_csrf_post():
    """The query-string token is fine for read-only data access (the
    historical use case) but must never be honoured as a CSRF-bypass
    signal because Simple Cross-Origin Requests can carry one without
    preflight. Even when the token is genuinely valid."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('csrf_qvalid')
            plaintext, _ = _make_token(user)
            client = app.test_client()
            _login_session(client, user)

            resp = client.post(
                f'/account?token={plaintext}',
                data={'summary_prompt': 'EXPLOIT_PROOF'},
                follow_redirects=False,
            )
            assert resp.status_code == 400, (
                'A valid query-string token must NOT be honoured for '
                'CSRF bypass; that was the exact attack vector in '
                'GHSA-x4q4-3ww4-h329.'
            )

            for t in APIToken.query.filter_by(user_id=user.id).all():
                db.session.delete(t)
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_change_password_refuses_sso_only_account():
    """The chained takeover finding: SSO-only users had no local
    password, so the change_password route used to skip the
    current-password check and silently set a new one. After the fix
    the endpoint must refuse outright and point users to the safe
    email-gated path instead."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = False  # focus on logic, not CSRF
    try:
        with app.app_context():
            user = _make_user('sso_only', password=None, sso_provider='Drift', sso_subject=f'subj-{uuid.uuid4().hex[:6]}')
            assert user.password is None
            client = app.test_client()
            _login_session(client, user)

            resp = client.post(
                '/change_password',
                data={
                    'new_password': 'A_Strong_New_Password!1',
                    'confirm_password': 'A_Strong_New_Password!1',
                },
                follow_redirects=False,
            )
            # The route redirects on the refusal path (with a flash).
            assert resp.status_code == 302

            # The user must still have no password.
            db.session.refresh(user)
            assert user.password is None, (
                'change_password set a password on an SSO-only account; '
                'the chained-takeover finding from GHSA-x4q4-3ww4-h329 '
                'is not fixed.'
            )

            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_sso_only_user_can_add_password_via_password_reset_flow():
    """Regression test for the legitimate SSO-to-password workflow that
    would have caught it if the GHSA-x4q4-3ww4-h329 fix were over-broad
    (i.e. blocking change_password without offering a replacement).

    The shape of the legitimate flow:

      1. SSO-only user (no local password) wants to unlink SSO later.
      2. ``sso_unlink`` requires a password, so they need to add one.
      3. They go to ``forgot_password`` with their email.
      4. The system emails a reset link (must work for SSO-only users).
      5. They open the link and ``reset_password`` sets their first
         password.
      6. The account now has both an SSO link and a password, so
         ``sso_unlink`` will work next time.

    Steps 3-5 are covered here. Step 6 is exercised separately because
    sso_unlink hits CSRF; the unit-level coverage is in the route's own
    happy-path tests.
    """
    from unittest.mock import patch

    saved_csrf = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        with app.app_context():
            user = _make_user(
                'sso_reset',
                password=None,
                sso_provider='Drift',
                sso_subject=f'subj-{uuid.uuid4().hex[:6]}',
                email_verified=True,
            )
            assert user.password is None

            client = app.test_client()

            # Step 3-4: forgot_password must send a reset email even
            # though the user has no existing password.
            with patch('src.api.auth.send_password_reset_email', return_value=True) as send_mail, \
                 patch('src.api.auth.can_resend_password_reset', return_value=(True, 0)), \
                 patch('src.api.auth.is_smtp_configured', return_value=True):
                resp = client.post('/forgot-password', data={'email': user.email}, follow_redirects=False)
                # forgot_password renders a "check your email" template on success (200)
                assert resp.status_code == 200, resp.data
                send_mail.assert_called_once()
                # Confirm the call targeted the SSO-only user we just created
                sent_to = send_mail.call_args.args[0]
                assert sent_to.id == user.id, (
                    'forgot_password did not call send_password_reset_email '
                    'for an SSO-only user; this is the regression that '
                    'would have surfaced any over-broad fix to '
                    'change_password.'
                )

            # Step 5: reset_password sets the first password.
            # Patch the token verifier so we do not need the real
            # serializer round-trip for the test.
            new_password = 'A_Strong_New_Password!1'
            with patch('src.api.auth.verify_reset_token', return_value=user.id):
                resp = client.post(
                    '/reset-password/fake-token-value',
                    data={'password': new_password, 'confirm_password': new_password},
                    follow_redirects=False,
                )
                # Successful reset redirects to login
                assert resp.status_code == 302, resp.data

            db.session.refresh(user)
            assert user.password is not None, (
                'reset_password did not set an initial password on an '
                'SSO-only user; the safe add-password flow is broken.'
            )
            # And the SSO link is intact, so sso_unlink can run later.
            assert user.sso_subject is not None
            assert user.sso_provider is not None

            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved_csrf


def test_api_v1_blueprint_remains_csrf_exempt_for_all_token_methods():
    """The documented programmatic surface (every example in
    docs/user-guide/api-tokens.md points at /api/v1/*) must continue
    to work for all four documented token methods even after the
    csrf_exempt_for_api_tokens hook was removed. The v1 blueprint is
    CSRF-exempt at registration time, so neither the hook nor its
    replacement is in the picture; this test pins that.

    We exercise the /api/v1/stats endpoint because it is a GET that
    Flask-WTF would normally not check anyway, plus it does not need
    any setup. The real assertion is that authentication completes
    via the request_loader and the route returns the user's stats."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('api_v1_methods')
            plaintext, _ = _make_token(user)
            client = app.test_client()
            # No session login -- token must do all the work
            for method_name, kwargs in [
                ('Authorization Bearer', {'headers': {'Authorization': f'Bearer {plaintext}'}}),
                ('X-API-Token', {'headers': {'X-API-Token': plaintext}}),
                ('API-Token', {'headers': {'API-Token': plaintext}}),
                ('query string', {'query_string': {'token': plaintext}}),
            ]:
                resp = client.get('/api/v1/stats', **kwargs)
                assert resp.status_code == 200, (
                    f'/api/v1/stats with {method_name} returned '
                    f'{resp.status_code}; documented token method broke'
                )
            for t in APIToken.query.filter_by(user_id=user.id).all():
                db.session.delete(t)
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_api_v1_post_with_query_string_token_still_works():
    """Critical for backwards compatibility: a POST to /api/v1/* with
    ``?token=...`` still succeeds, because the v1 blueprint is
    blueprint-level CSRF-exempt. The fix only changed how non-v1
    endpoints react to a query-string token, not the v1 surface."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('api_v1_query_post')
            plaintext, _ = _make_token(user)
            client = app.test_client()
            # Need a recording owned by the user to call regenerate_title
            from src.models import Recording
            rec = Recording(
                user_id=user.id,
                title='for-test',
                audio_path='/tmp/nonexistent.mp3',
                status='COMPLETED',
                transcription='hello world',
            )
            db.session.add(rec)
            db.session.commit()

            # Pure-query-string authentication on a v1 POST endpoint.
            # The v1 route delegates to src.api.recordings.regenerate_title,
            # which calls _generate_ai_title (a real LLM call). Mock that so
            # the test exercises the auth/CSRF path without needing a live LLM
            # — otherwise it returns 500 in CI and masks what we're asserting.
            from unittest.mock import patch
            with patch('src.tasks.processing._generate_ai_title', return_value='Mock Title'), \
                 patch('src.services.job_queue.job_queue.enqueue', return_value=999):
                resp = client.post(
                    f'/api/v1/recordings/{rec.id}/regenerate_title?token={plaintext}',
                )
            # Acceptable success codes (the route may 202 or 200)
            assert resp.status_code in (200, 202, 404), (
                f'/api/v1/* POST with ?token= regressed; got {resp.status_code}: {resp.data!r}'
            )
            # 404 is acceptable here if the endpoint requires more
            # setup than we have (e.g. completed transcript); what we
            # really care about is that it is NOT a 400 CSRF rejection.
            assert resp.status_code != 400

            db.session.delete(rec)
            for t in APIToken.query.filter_by(user_id=user.id).all():
                db.session.delete(t)
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_browser_session_post_with_valid_csrf_token_succeeds():
    """Backwards-compat positive: the regular web-UI flow (logged-in
    user POSTs with a CSRF token from the form) must still succeed
    after the CSRF refactor. The Attack 2 test already covers the
    NEGATIVE path (no token → 400); this pins the POSITIVE path."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('csrf_browser_ok')
            client = app.test_client()
            _login_session(client, user)

            # Fetch the page to obtain a fresh CSRF token from the form.
            page = client.get('/account')
            import re
            m = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
            assert m, 'Could not find csrf_token in /account page'
            csrf_token = m.group(1).decode()

            resp = client.post(
                '/account',
                data={'user_name': 'Renamed User', 'csrf_token': csrf_token},
                follow_redirects=False,
            )
            # 302 redirect on success (account form pattern)
            assert resp.status_code in (200, 302), (
                f'Browser POST with valid CSRF token regressed: {resp.status_code}'
            )

            db.session.refresh(user)
            assert user.name == 'Renamed User', (
                'Field did not persist; CSRF accepted but view did not run.'
            )

            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_change_password_happy_path_for_user_with_existing_password():
    """Backwards-compat positive: the change_password fix only refuses
    accounts where ``current_user.password`` is None. Regular users
    with an existing password must still be able to change theirs.
    The fix did not touch that flow, but the test pins it so we know
    if anyone breaks it later."""
    from flask_bcrypt import Bcrypt
    bcrypt = Bcrypt(app)
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        with app.app_context():
            current_password = 'Existing_Password!1'
            hashed = bcrypt.generate_password_hash(current_password).decode()
            user = _make_user('csrf_chpw_happy', password=hashed)
            client = app.test_client()
            _login_session(client, user)

            new_password = 'A_New_Password!1'
            resp = client.post(
                '/change_password',
                data={
                    'current_password': current_password,
                    'new_password': new_password,
                    'confirm_password': new_password,
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302

            db.session.refresh(user)
            assert bcrypt.check_password_hash(user.password, new_password), (
                'change_password did not update the password on a happy-path '
                'user; the SSO refusal fix was over-broad.'
            )
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_api_v1_post_with_authorization_header_token_succeeds():
    """Backwards-compat positive for the recommended automation path.
    docs/user-guide/api-tokens.md lists ``Authorization: Bearer ...`` as
    the recommended method; this pins that POSTing to a v1 endpoint
    that way returns a non-CSRF response code (i.e. authentication and
    routing both worked; the actual route may 404 or 202 depending on
    record state, but it must not be CSRF-rejected)."""
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            user = _make_user('api_v1_auth_hdr_post')
            plaintext, _ = _make_token(user)
            client = app.test_client()
            from src.models import Recording
            rec = Recording(
                user_id=user.id,
                title='for-test',
                audio_path='/tmp/nonexistent.mp3',
                status='COMPLETED',
                transcription='hello world',
            )
            db.session.add(rec)
            db.session.commit()

            from unittest.mock import patch
            with patch('src.services.job_queue.job_queue.enqueue', return_value=999):
                resp = client.post(
                    f'/api/v1/recordings/{rec.id}/regenerate_title',
                    headers={'Authorization': f'Bearer {plaintext}'},
                )
            # Anything except 400/401/403 is fine here; the route's own
            # internal semantics may 404 if it cannot find an LLM, etc.
            assert resp.status_code not in (400, 401, 403), (
                f'Bearer token POST on /api/v1/* returned {resp.status_code} '
                f'(expected success or non-auth error); the documented '
                f'automation path is broken.'
            )
            db.session.delete(rec)
            for t in APIToken.query.filter_by(user_id=user.id).all():
                db.session.delete(t)
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def test_forgot_password_does_not_email_when_smtp_not_configured():
    """Safety net: when SMTP is not configured, forgot_password must
    NOT attempt to send (otherwise we leak SMTP misconfiguration errors
    on every reset request). The route should redirect with a flash."""
    from unittest.mock import patch
    saved_csrf = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        with app.app_context():
            user = _make_user('sso_no_smtp', password=None, sso_provider='Drift', sso_subject=f'subj-{uuid.uuid4().hex[:6]}')
            client = app.test_client()
            with patch('src.api.auth.is_smtp_configured', return_value=False), \
                 patch('src.api.auth.send_password_reset_email') as send_mail:
                resp = client.post('/forgot-password', data={'email': user.email}, follow_redirects=False)
                # Should redirect to login with a flash, not crash
                assert resp.status_code == 302
                send_mail.assert_not_called()
            db.session.delete(user)
            db.session.commit()
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved_csrf


def teardown_module(module):
    with app.app_context():
        for u in User.query.filter(User.username.like('csrf_%')).all():
            for t in APIToken.query.filter_by(user_id=u.id).all():
                db.session.delete(t)
            db.session.delete(u)
        for u in User.query.filter(User.username.like('sso_only_%')).all():
            db.session.delete(u)
        db.session.commit()
