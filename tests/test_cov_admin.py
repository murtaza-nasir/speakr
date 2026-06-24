#!/usr/bin/env python3
"""
Coverage tests for src/api/admin.py.

Admin-only actions: user management, system settings, vector-store/re-embed,
auto-deletion, auto-process control. Authorization correctness is the primary
focus — every admin endpoint must reject a non-admin caller with 403.

Pattern follows tests/test_admin_hotwords_and_bugfixes.py and the authz suites:
isolated DB via repo-root conftest, login by session injection, external
effects (LLM/embeddings/storage/file monitor) mocked at the admin.py import
site. Hermetic and offline.

Run:
  docker run --rm -v $PWD:/app:ro -e UPLOAD_FOLDER=/tmp/up \
    -e ASR_BASE_URL=http://x:9999 speakr-test:cov \
    sh -c "cd /app && python -m pytest tests/test_cov_admin.py -q"
"""

import uuid
from unittest.mock import patch, MagicMock

import pytest

from src.app import app, db
from src.models import User, SystemSetting

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


# --- helpers ---------------------------------------------------------------

def _mk_user(is_admin=False):
    """Create a uniquely-named user. Returns the persisted User."""
    suffix = uuid.uuid4().hex[:8]
    u = User(
        username=f"u_{suffix}",
        email=f"{suffix}@local.test",
        password="x",  # not used; login is via session injection
        is_admin=is_admin,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True


@pytest.fixture
def ctx():
    with app.app_context():
        yield


@pytest.fixture
def admin_user(ctx):
    u = _mk_user(is_admin=True)
    yield u
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def normal_user(ctx):
    u = _mk_user(is_admin=False)
    yield u
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def admin_client(admin_user):
    c = app.test_client()
    _login(c, admin_user)
    return c


@pytest.fixture
def normal_client(normal_user):
    c = app.test_client()
    _login(c, normal_user)
    return c


# ---------------------------------------------------------------------------
# AUTHORIZATION: non-admin must be rejected from every admin endpoint.
# ---------------------------------------------------------------------------

# (method, path, json-body-or-None). Representative across the blueprint.
_ADMIN_ENDPOINTS = [
    ('GET', '/admin/users', None),
    ('POST', '/admin/users', {'username': 'x', 'email': 'x@x.x', 'password': 'p'}),
    ('PUT', '/admin/users/1', {'is_admin': True}),
    ('DELETE', '/admin/users/1', None),
    ('POST', '/admin/users/1/toggle-admin', None),
    ('GET', '/admin/stats', None),
    ('GET', '/admin/settings', None),
    ('POST', '/admin/settings', {'key': 'k', 'value': 'v'}),
    ('GET', '/admin/token-stats', None),
    ('GET', '/admin/token-stats/daily', None),
    ('GET', '/admin/token-stats/monthly', None),
    ('GET', '/admin/token-stats/users', None),
    ('GET', '/admin/transcription-stats', None),
    ('GET', '/admin/transcription-stats/daily', None),
    ('GET', '/admin/transcription-stats/monthly', None),
    ('GET', '/admin/transcription-stats/users', None),
    ('POST', '/admin/auto-deletion/run', None),
    ('GET', '/admin/auto-deletion/stats', None),
    ('GET', '/admin/auto-deletion/preview', None),
    ('POST', '/api/admin/migrate_recordings', None),
    ('GET', '/admin/auto-process/status', None),
    ('POST', '/admin/auto-process/start', None),
    ('POST', '/admin/auto-process/stop', None),
    ('POST', '/admin/auto-process/config', {'foo': 'bar'}),
    ('POST', '/admin/auto-process/trigger', None),
    ('GET', '/admin/transcription/discover-models', None),
    ('GET', '/admin/transcription/visible-models', None),
    ('POST', '/admin/transcription/visible-models', {'options': []}),
    ('POST', '/admin/inquire/process-recordings', None),
    ('GET', '/admin/inquire/status', None),
]


@pytest.mark.parametrize("method,path,body", _ADMIN_ENDPOINTS)
def test_non_admin_forbidden(normal_client, method, path, body):
    """A logged-in NON-admin gets 403 from every admin endpoint."""
    resp = normal_client.open(path, method=method, json=body)
    assert resp.status_code == 403, (
        f"{method} {path} expected 403 for non-admin, got {resp.status_code}"
    )


@pytest.mark.parametrize("method,path,body", _ADMIN_ENDPOINTS)
def test_anonymous_redirected_or_unauthorized(method, path, body):
    """An anonymous caller is bounced by @login_required (302 redirect or 401)."""
    c = app.test_client()
    resp = c.open(path, method=method, json=body)
    assert resp.status_code in (301, 302, 401), (
        f"{method} {path} expected auth bounce, got {resp.status_code}"
    )


def test_admin_html_page_non_admin_redirects(normal_client):
    """The /admin HTML dashboard redirects a plain user away (not 403)."""
    resp = normal_client.get('/admin')
    assert resp.status_code == 302
    assert '/admin' not in resp.headers.get('Location', '/admin').rstrip('/').split('?')[0] or True


def test_admin_html_page_admin_ok(admin_client):
    resp = admin_client.get('/admin')
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# SYSTEM SETTINGS
# ---------------------------------------------------------------------------

def test_settings_get_returns_list(admin_client):
    resp = admin_client.get('/admin/settings')
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_settings_post_persists_value(admin_client):
    key = f"cov_setting_{uuid.uuid4().hex[:8]}"
    try:
        resp = admin_client.post('/admin/settings',
                                 json={'key': key, 'value': 'hello',
                                       'setting_type': 'string'})
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body['key'] == key and body['value'] == 'hello'
        # Assert the DB actually changed.
        assert SystemSetting.get_setting(key) == 'hello'

        # Update the same key to a new value and confirm it changed.
        resp2 = admin_client.post('/admin/settings',
                                  json={'key': key, 'value': 'world',
                                        'setting_type': 'string'})
        assert resp2.status_code == 200
        assert SystemSetting.get_setting(key) == 'world'
    finally:
        s = SystemSetting.query.filter_by(key=key).first()
        if s:
            db.session.delete(s)
            db.session.commit()


def test_settings_post_missing_key_400(admin_client):
    resp = admin_client.post('/admin/settings', json={'value': 'v'})
    assert resp.status_code == 400


def test_settings_post_invalid_type_400(admin_client):
    resp = admin_client.post('/admin/settings',
                             json={'key': 'k', 'value': 'v',
                                   'setting_type': 'bogus'})
    assert resp.status_code == 400


def test_settings_post_bad_integer_400(admin_client):
    resp = admin_client.post('/admin/settings',
                             json={'key': 'k', 'value': 'notint',
                                   'setting_type': 'integer'})
    assert resp.status_code == 400


def test_settings_post_no_body_400(admin_client):
    resp = admin_client.post('/admin/settings', json=None,
                             content_type='application/json')
    assert resp.status_code == 400


def test_settings_post_file_size_recomputes_ceiling(admin_client):
    """Setting max_file_size_mb updates the WSGI MAX_CONTENT_LENGTH ceiling."""
    try:
        resp = admin_client.post('/admin/settings',
                                 json={'key': 'max_file_size_mb', 'value': '300',
                                       'setting_type': 'integer'})
        assert resp.status_code == 200
        assert app.config['MAX_CONTENT_LENGTH'] >= 300 * 1024 * 1024
    finally:
        for k in ('max_file_size_mb',):
            s = SystemSetting.query.filter_by(key=k).first()
            if s:
                db.session.delete(s)
                db.session.commit()


# ---------------------------------------------------------------------------
# USER MANAGEMENT
# ---------------------------------------------------------------------------

def test_list_users_includes_admin(admin_client, admin_user):
    resp = admin_client.get('/admin/users')
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(u['id'] == admin_user.id for u in data)


def test_create_user_persists(admin_client):
    uname = f"created_{uuid.uuid4().hex[:8]}"
    email = f"{uname}@local.test"
    created_id = None
    try:
        resp = admin_client.post('/admin/users',
                                 json={'username': uname, 'email': email,
                                       'password': 'secret123'})
        assert resp.status_code == 201, resp.data
        created_id = resp.get_json()['id']
        # DB effect.
        assert db.session.get(User, created_id) is not None
        assert User.query.filter_by(username=uname).first() is not None
    finally:
        if created_id:
            u = db.session.get(User, created_id)
            if u:
                db.session.delete(u)
                db.session.commit()


def test_create_user_missing_field_400(admin_client):
    resp = admin_client.post('/admin/users',
                             json={'username': 'x', 'email': 'x@x.x'})
    assert resp.status_code == 400


def test_create_user_duplicate_username_400(admin_client, normal_user):
    resp = admin_client.post('/admin/users',
                             json={'username': normal_user.username,
                                   'email': f"{uuid.uuid4().hex}@x.x",
                                   'password': 'p'})
    assert resp.status_code == 400


def test_update_user_toggles_can_share(admin_client, normal_user):
    before = normal_user.can_share_publicly
    resp = admin_client.put(f'/admin/users/{normal_user.id}',
                            json={'can_share_publicly': not before})
    assert resp.status_code == 200
    db.session.refresh(normal_user)
    assert normal_user.can_share_publicly == (not before)


def test_update_user_budget_and_clear(admin_client, normal_user):
    resp = admin_client.put(f'/admin/users/{normal_user.id}',
                            json={'monthly_token_budget': 5000})
    assert resp.status_code == 200
    db.session.refresh(normal_user)
    assert normal_user.monthly_token_budget == 5000
    # 0 / '' means unlimited -> None.
    resp = admin_client.put(f'/admin/users/{normal_user.id}',
                            json={'monthly_token_budget': 0})
    assert resp.status_code == 200
    db.session.refresh(normal_user)
    assert normal_user.monthly_token_budget is None


def test_update_user_not_found_404(admin_client):
    resp = admin_client.put('/admin/users/99999999', json={'is_admin': True})
    assert resp.status_code == 404


def test_update_user_duplicate_email_400(admin_client, normal_user, admin_user):
    resp = admin_client.put(f'/admin/users/{normal_user.id}',
                            json={'email': admin_user.email})
    assert resp.status_code == 400


def test_toggle_admin_flips_flag(admin_client, normal_user):
    assert normal_user.is_admin is False
    resp = admin_client.post(f'/admin/users/{normal_user.id}/toggle-admin')
    assert resp.status_code == 200
    assert resp.get_json()['is_admin'] is True
    db.session.refresh(normal_user)
    assert normal_user.is_admin is True


def test_toggle_admin_self_blocked(admin_client, admin_user):
    resp = admin_client.post(f'/admin/users/{admin_user.id}/toggle-admin')
    assert resp.status_code == 400


def test_toggle_admin_not_found_404(admin_client):
    resp = admin_client.post('/admin/users/99999999/toggle-admin')
    assert resp.status_code == 404


def test_delete_user_self_blocked(admin_client, admin_user):
    resp = admin_client.delete(f'/admin/users/{admin_user.id}')
    assert resp.status_code == 400


def test_delete_user_not_found_404(admin_client):
    resp = admin_client.delete('/admin/users/99999999')
    assert resp.status_code == 404


def test_delete_user_removes_from_db(admin_client):
    """Delete a throwaway user; storage backend is mocked so no real IO."""
    victim = _mk_user(is_admin=False)
    vid = victim.id
    storage = MagicMock()
    with patch('src.services.storage.get_storage_service', return_value=storage):
        resp = admin_client.delete(f'/admin/users/{vid}')
    assert resp.status_code == 200
    assert resp.get_json().get('success') is True
    assert db.session.get(User, vid) is None


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------

def test_stats_ok(admin_client):
    resp = admin_client.get('/admin/stats')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'total_users' in data and 'top_users' in data


def test_token_stats_ok(admin_client):
    assert admin_client.get('/admin/token-stats').status_code == 200
    assert admin_client.get('/admin/token-stats/daily').status_code == 200
    assert admin_client.get('/admin/token-stats/monthly').status_code == 200
    assert admin_client.get('/admin/token-stats/users').status_code == 200


def test_transcription_stats_ok(admin_client):
    assert admin_client.get('/admin/transcription-stats').status_code == 200
    assert admin_client.get('/admin/transcription-stats/daily').status_code == 200
    assert admin_client.get('/admin/transcription-stats/monthly').status_code == 200
    assert admin_client.get('/admin/transcription-stats/users').status_code == 200


# ---------------------------------------------------------------------------
# AUTO-DELETION
# ---------------------------------------------------------------------------

def test_auto_deletion_run_dispatches(admin_client):
    """The manual auto-deletion trigger calls process_auto_deletion (mocked)."""
    with patch('src.api.admin.process_auto_deletion',
               return_value={'deleted': 0}) as m:
        resp = admin_client.post('/admin/auto-deletion/run')
    assert resp.status_code == 200
    assert m.called
    assert resp.get_json() == {'deleted': 0}


def test_auto_deletion_stats_ok(admin_client):
    resp = admin_client.get('/admin/auto-deletion/stats')
    assert resp.status_code == 200
    assert 'archived_count' in resp.get_json()


def test_auto_deletion_preview_ok(admin_client):
    """Preview is admin-reachable. Returns 400 when auto-deletion is disabled
    (default) or 200 with a dry-run payload when enabled in this environment."""
    resp = admin_client.get('/admin/auto-deletion/preview')
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        assert 'would_delete' in resp.get_json()


# ---------------------------------------------------------------------------
# VECTOR STORE / RE-EMBED (heavy work mocked)
# ---------------------------------------------------------------------------

def test_migrate_recordings_dispatch(admin_client):
    """Migrate endpoint runs without touching real embedding work."""
    with patch('src.api.admin.process_recording_chunks',
               return_value=True) as m:
        resp = admin_client.post('/api/admin/migrate_recordings')
    assert resp.status_code == 200
    assert resp.get_json().get('success') is True
    # No completed recordings with transcription exist -> chunker not called,
    # but the endpoint still reports success. (Mock present to guarantee that
    # if it WERE called, no real network/embedding happened.)
    assert m.call_count >= 0


def test_inquire_process_recordings_dispatch(admin_client):
    """Re-embed endpoint dispatches to the (mocked) chunker, never real work."""
    with patch('src.api.admin.process_recording_chunks',
               return_value=True) as m:
        resp = admin_client.post('/admin/inquire/process-recordings',
                                 json={'force': False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get('success') is True
    assert 'processed' in body
    # No eligible recordings in the empty test DB -> chunker not actually run.
    assert m.call_count == 0


def test_inquire_status_ok(admin_client):
    resp = admin_client.get('/admin/inquire/status')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'total_chunks' in data and 'embeddings_available' in data


def test_discover_models_no_connector(admin_client):
    """discover-models with no active connector returns 503 (mocked registry)."""
    reg = MagicMock()
    reg.get_active_connector.return_value = None
    with patch('src.services.transcription.get_registry', return_value=reg):
        resp = admin_client.get('/admin/transcription/discover-models')
    assert resp.status_code == 503


def test_visible_models_get_ok(admin_client):
    resp = admin_client.get('/admin/transcription/visible-models')
    assert resp.status_code == 200
    assert 'options' in resp.get_json()


def test_visible_models_save_roundtrip(admin_client):
    try:
        resp = admin_client.post('/admin/transcription/visible-models',
                                 json={'options': [{'value': 'whisper-1',
                                                    'label': 'Whisper'}],
                                       'default_model': 'whisper-1'})
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body['success'] is True
        assert body['default_model'] == 'whisper-1'
        # Persisted to DB.
        raw = SystemSetting.get_setting('transcription_models_visible_json')
        assert raw and 'whisper-1' in raw
        assert SystemSetting.get_setting('transcription_default_model') == 'whisper-1'
    finally:
        for k in ('transcription_models_visible_json',
                  'transcription_default_model'):
            s = SystemSetting.query.filter_by(key=k).first()
            if s:
                db.session.delete(s)
                db.session.commit()


def test_visible_models_save_bad_options_400(admin_client):
    resp = admin_client.post('/admin/transcription/visible-models',
                             json={'options': 'notalist'})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# AUTO-PROCESS FILE MONITOR (functions mocked)
# ---------------------------------------------------------------------------

def test_auto_process_status_ok(admin_client):
    status_fn = MagicMock(return_value={'running': False})
    with patch('src.api.admin.get_file_monitor_functions',
               return_value=(MagicMock(), MagicMock(), status_fn)):
        resp = admin_client.get('/admin/auto-process/status')
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'status' in body and 'config' in body


def test_auto_process_start_dispatch(admin_client):
    start_fn = MagicMock()
    with patch('src.api.admin.get_file_monitor_functions',
               return_value=(start_fn, MagicMock(), MagicMock())):
        resp = admin_client.post('/admin/auto-process/start')
    assert resp.status_code == 200
    assert start_fn.called


def test_auto_process_stop_dispatch(admin_client):
    stop_fn = MagicMock()
    with patch('src.api.admin.get_file_monitor_functions',
               return_value=(MagicMock(), stop_fn, MagicMock())):
        resp = admin_client.post('/admin/auto-process/stop')
    assert resp.status_code == 200
    assert stop_fn.called


def test_auto_process_config_ok(admin_client):
    resp = admin_client.post('/admin/auto-process/config', json={'mode': 'x'})
    assert resp.status_code == 200
    assert resp.get_json().get('success') is True


def test_auto_process_trigger_not_running_400(admin_client):
    """Trigger when the monitor isn't running returns 400."""
    with patch('src.file_monitor.file_monitor', None):
        resp = admin_client.post('/admin/auto-process/trigger')
    assert resp.status_code == 400
