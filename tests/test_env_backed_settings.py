"""Settings that can be changed in System Settings keep using .env until an
admin saves a value (#567 follow-up).

Before this, the first start copied each .env value into the database, and
later .env changes had no effect after a restart.
"""

import uuid

import pytest

import src.app as app_module
from src.app import app, db
from src.models import SystemSetting, User
from src.utils.system_settings import ENV_BACKED_SETTINGS

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

KEY = 'enable_public_sharing'
ENV = 'ENABLE_PUBLIC_SHARING'


@pytest.fixture
def unsaved_setting():
    """The row as a fresh start creates it: no value."""
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        saved = (row.value, row.setting_type) if row else None
        SystemSetting.set_setting(KEY, None, ENV_BACKED_SETTINGS[KEY][3], 'boolean')
    yield
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        if saved is None:
            db.session.delete(row)
            db.session.commit()
        else:
            SystemSetting.set_setting(KEY, saved[0], None, saved[1])


def _read():
    # A new app context per read, as each request has its own.
    with app.app_context():
        return app_module.ENABLE_PUBLIC_SHARING


def test_env_change_applies_while_no_value_is_saved(unsaved_setting, monkeypatch):
    monkeypatch.setenv(ENV, 'false')
    assert _read() is False
    monkeypatch.setenv(ENV, 'true')
    assert _read() is True


def test_saved_value_overrides_env(unsaved_setting, monkeypatch):
    monkeypatch.setenv(ENV, 'true')
    with app.app_context():
        SystemSetting.set_setting(KEY, 'false', None, 'boolean')
    assert _read() is False


def test_admin_settings_show_env_value_until_saved(unsaved_setting, monkeypatch):
    monkeypatch.setenv(ENV, 'false')
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        admin = User(username=f'a_{suffix}', email=f'{suffix}@local.test', password='x', is_admin=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    try:
        client = app.test_client()
        with client.session_transaction() as s:
            s['_user_id'] = str(admin_id)
            s['_fresh'] = True

        row = next(r for r in client.get('/admin/settings').get_json() if r['key'] == KEY)
        assert row['value'] == 'false'
        assert row['source'] == 'env'

        resp = client.post('/admin/settings', json={'key': KEY, 'value': 'true', 'setting_type': 'boolean'})
        assert resp.status_code == 200
        row = next(r for r in client.get('/admin/settings').get_json() if r['key'] == KEY)
        assert row['value'] == 'true'
        assert 'source' not in row
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, admin_id))
            db.session.commit()


# --- .env parsing, scheduler, returning to .env ----------------------------

@pytest.fixture
def admin_client():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        admin = User(username=f'a_{suffix}', email=f'{suffix}@local.test', password='x', is_admin=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(admin_id)
        s['_fresh'] = True
    yield client
    with app.app_context():
        db.session.delete(db.session.get(User, admin_id))
        db.session.commit()


@pytest.fixture
def restore_settings():
    """Put back every env-backed row exactly as it was."""
    with app.app_context():
        saved = {r.key: (r.value, r.setting_type) for r in
                 SystemSetting.query.filter(SystemSetting.key.in_(list(ENV_BACKED_SETTINGS))).all()}
    yield
    with app.app_context():
        for key in ENV_BACKED_SETTINGS:
            row = SystemSetting.query.filter_by(key=key).first()
            if key in saved:
                SystemSetting.set_setting(key, saved[key][0], None, saved[key][1])
            elif row is not None:
                db.session.delete(row)
                db.session.commit()


@pytest.mark.parametrize('raw, expected', [
    ('true', True), ('TRUE', True), (' True ', True),
    ('1', False), ('yes', False), ('on', False), ('false', False),
])
def test_env_booleans_are_true_only(unsaved_setting, monkeypatch, raw, expected):
    # Same rule as before #567: ENABLE_PUBLIC_SHARING=1 meant off.
    monkeypatch.setenv(ENV, raw)
    assert _read() is expected


@pytest.mark.parametrize('enabled, days, due', [
    ('true', '30', True), ('false', '30', False), ('true', '0', False),
])
def test_scheduled_deletion_reads_settings_at_run_time(restore_settings, monkeypatch, enabled, days, due):
    from src.config.startup import scheduled_deletion_due
    monkeypatch.setenv('ENABLE_AUTO_DELETION', 'false')
    monkeypatch.setenv('GLOBAL_RETENTION_DAYS', '0')
    with app.app_context():
        SystemSetting.set_setting('enable_auto_deletion', enabled, None, 'boolean')
        SystemSetting.set_setting('global_retention_days', days, None, 'integer')
    with app.app_context():
        assert scheduled_deletion_due() is due


@pytest.mark.parametrize('key, env, env_value, saved, expected', [
    ('enable_public_sharing', 'ENABLE_PUBLIC_SHARING', 'false', 'true', 'false'),
    ('help_center_url', 'HELP_CENTER_URL', 'https://help.example.test', 'https://other.example.test', 'https://help.example.test'),
    ('max_concurrent_uploads', 'MAX_CONCURRENT_UPLOADS', '7', '2', '7'),
])
@pytest.mark.parametrize('cleared', [None, ''])
def test_clearing_returns_to_env(restore_settings, admin_client, monkeypatch, key, env, env_value, saved, expected, cleared):
    monkeypatch.setenv(env, env_value)
    setting_type = ENV_BACKED_SETTINGS[key][2]
    assert admin_client.post('/admin/settings', json={'key': key, 'value': saved, 'setting_type': setting_type}).status_code == 200
    row = next(r for r in admin_client.get('/admin/settings').get_json() if r['key'] == key)
    assert row['value'] == saved and row['env_backed'] is True and 'source' not in row

    assert admin_client.post('/admin/settings', json={'key': key, 'value': cleared, 'setting_type': setting_type}).status_code == 200
    row = next(r for r in admin_client.get('/admin/settings').get_json() if r['key'] == key)
    assert row['value'] == expected and row['source'] == 'env'
    with app.app_context():
        assert SystemSetting.query.filter_by(key=key).first().value is None


def test_clearing_other_settings_is_unchanged(admin_client):
    # Settings without an .env fallback still reject an empty boolean.
    resp = admin_client.post('/admin/settings', json={'key': 'enable_folders', 'value': '', 'setting_type': 'boolean'})
    assert resp.status_code == 400
