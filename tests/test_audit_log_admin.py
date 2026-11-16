"""Audit log: user administration, system settings, the retention run and
groups. Field names, ids and codes only: no e-mail address, password, group
name or free setting value reaches a row.
"""

import json
import uuid

import pytest

from tests.audit_log_support import (  # noqa: E402
    PASSWORD, app, audit, client_for, db, make_recording, make_user, max_id, names, remove_users,
    reset_audit_settings, rows_after, set_setting,
)
from src.models import Group, GroupMembership, LoginFailure, SystemSetting, User  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


@pytest.fixture(autouse=True)
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        set_setting('enable_internal_sharing', 'true')
        yield users
        audit.flush()
        db.session.rollback()
        SystemSetting.set_setting('enable_internal_sharing', None, None, None)
        reset_audit_settings()
        for group in Group.query.filter(Group.name.like('audgrp%')).all():
            GroupMembership.query.filter_by(group_id=group.id).delete()
            db.session.delete(group)
        db.session.commit()
        remove_users(users)


def _dump(rows):
    return json.dumps([r.to_dict() for r in rows])


def test_user_lifecycle(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    name = 'new' + uuid.uuid4().hex[:8]
    start = max_id()
    resp = client.post('/admin/users', json={'username': name, 'email': f'{name}@example.com',
                                             'password': 'SENTINELPASS-1a!'})
    assert resp.status_code in (200, 201), resp.data[:300]
    user_id = resp.get_json()['id']
    created.append(user_id)

    resp = client.put(f'/admin/users/{user_id}', json={
        'email': f'{name}-2@example.com', 'storage_quota_mb': 500, 'password': 'SENTINELPASS-2b!',
        'unlock_login': True, 'is_admin': True})
    assert resp.status_code == 200, resp.data[:300]
    # The same values again: nothing changed, but the password was set.
    assert client.put(f'/admin/users/{user_id}', json={'storage_quota_mb': 500}).status_code == 200
    assert client.post(f'/admin/users/{user_id}/toggle-admin').status_code == 200
    assert client.delete(f'/admin/users/{user_id}').status_code == 200
    created.remove(user_id)

    rows = rows_after(start)
    assert names(rows) == ['admin.user.created', 'admin.user.changed', 'admin.user.password_set',
                           'admin.user.unlocked', 'admin.user.role_changed', 'admin.user.role_changed',
                           'admin.user.deleted']
    assert rows[0].details == {'username': name, 'is_admin': False}
    assert rows[1].details == {'fields': ['email', 'storage_quota_mb']}
    assert rows[2].details == {'self': False}
    assert rows[3].details is None
    assert rows[4].details == {'old': False, 'new': True}
    assert rows[5].details == {'old': True, 'new': False}
    assert rows[6].details == {'username': name, 'recordings_deleted': 0}
    for row in rows:
        assert row.target_user_id == user_id and row.actor_user_id == admin.id
        assert row.event_group == 'admin' and row.actor_kind == 'session'
    dump = _dump(rows)
    assert 'SENTINEL' not in dump and 'example.com' not in dump
    # The username is on the created and deleted rows only.
    assert [name in json.dumps(r.to_dict()) for r in rows] == [True, False, False, False, False, False, True]


def test_an_admin_who_sets_the_own_password_is_marked(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    start = max_id()
    assert client.put(f'/admin/users/{admin.id}', json={'password': 'An0ther-Passw0rd!'}).status_code == 200
    rows = rows_after(start, 'admin.user.password_set')
    assert [(r.target_user_id, r.details) for r in rows] == [(admin.id, {'self': True})]


def test_a_non_admin_changes_nothing_and_records_nothing(created):
    user = make_user(created)
    other = make_user(created, prefix='oth')
    start = max_id()
    assert client_for(user).put(f'/admin/users/{other.id}', json={'is_admin': True}).status_code == 403
    assert rows_after(start) == []


def test_setting_changes_record_values_for_listed_keys_only(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    saved = {key: (row.value if row else None) for key, row in (
        (k, SystemSetting.query.filter_by(key=k).first())
        for k in ('users_can_delete', 'help_center_url', 'global_retention_days', 'deletion_mode'))}
    start = max_id()
    try:
        post = lambda body: client.post('/admin/settings', json=body)  # noqa: E731
        assert post({'key': 'users_can_delete', 'value': 'false', 'setting_type': 'boolean'}).status_code == 200
        assert post({'key': 'global_retention_days', 'value': '30', 'setting_type': 'integer'}).status_code == 200
        assert post({'key': 'deletion_mode', 'value': 'audio_only'}).status_code == 200
        assert post({'key': 'help_center_url', 'value': 'https://SENTINELVALUE.example'}).status_code == 200
        assert post({'key': 'users_can_delete', 'value': ''}).status_code == 200
        assert post({'key': 'SENTINELKEY', 'value': 'SENTINELVALUE'}).status_code == 200
        rows = rows_after(start)
        assert names(rows) == ['admin.setting.changed'] * 6
        assert rows[0].details == {'key': 'users_can_delete', 'new': 'false'}
        assert rows[1].details['key'] == 'global_retention_days' and rows[1].details['new'] == 30
        assert rows[2].details['key'] == 'deletion_mode' and rows[2].details['new'] == 'audio_only'
        assert rows[3].details == {'key': 'help_center_url'}
        assert rows[4].details == {'key': 'users_can_delete', 'old': 'false'}
        assert rows[5].details == {'key': 'other'}
        assert 'SENTINEL' not in _dump(rows)
    finally:
        SystemSetting.query.filter_by(key='SENTINELKEY').delete()
        db.session.commit()
        for key, value in saved.items():
            SystemSetting.set_setting(key, value, None, None)


def test_the_switch_cannot_hide_itself(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    start = max_id()
    resp = client.post('/admin/settings', json={'key': 'audit_log_enabled', 'value': 'false', 'setting_type': 'boolean'})
    assert resp.status_code == 200
    # Off: an ordinary change is not recorded.
    resp = client.post('/admin/settings', json={'key': 'users_can_delete', 'value': 'true', 'setting_type': 'boolean'})
    assert resp.status_code == 200
    SystemSetting.set_setting('users_can_delete', None, None, None)
    # A change of an audit setting is.
    resp = client.post('/admin/settings', json={'key': 'audit_log_store_ip', 'value': 'off'})
    assert resp.status_code == 200
    resp = client.post('/admin/settings', json={'key': 'audit_log_enabled', 'value': 'true', 'setting_type': 'boolean'})
    assert resp.status_code == 200
    rows = rows_after(start)
    assert [r.details for r in rows] == [
        {'key': 'audit_log_enabled', 'new': 'false'},
        {'key': 'audit_log_store_ip', 'new': 'off'},
        {'key': 'audit_log_enabled', 'old': 'false', 'new': 'true'}]
    assert all(r.event == 'admin.setting.changed' and r.actor_user_id == admin.id for r in rows)


def test_a_key_is_known_only_from_a_list_of_the_code(created):
    import re
    from pathlib import Path
    from src.utils.system_settings import ENV_BACKED_SETTINGS
    source = (Path(__file__).resolve().parent.parent / 'src' / 'init_db.py').read_text(encoding='utf-8')
    for key in set(re.findall(r"key='([a-z_0-9]+)'", source)) | set(ENV_BACKED_SETTINGS):
        assert audit.known_setting_key(key), key
    assert audit.known_setting_key('permission_create_groups')
    assert audit.known_setting_key('llm_temperature_summary')
    assert not audit.known_setting_key('llm_temperature_SENTINEL')
    # A key that was saved once is still typed text.
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    start = max_id()
    try:
        for _ in range(2):
            assert client.post('/admin/settings', json={'key': 'sentinel_key', 'value': 'x'}).status_code == 200
        assert [r.details for r in rows_after(start)] == [{'key': 'other'}, {'key': 'other'}]
    finally:
        SystemSetting.query.filter_by(key='sentinel_key').delete()
        db.session.commit()


def test_setting_values(created):
    assert audit.setting_value('users_can_delete', True) == 'true'
    assert audit.setting_value('users_can_delete', 'Yes') == 'true'
    assert audit.setting_value('global_retention_days', ' 90 ') == 90
    assert audit.setting_value('global_retention_days', 'ninety SENTINEL') == 'other'
    assert audit.setting_value('deletion_mode', 'SENTINEL') == 'other'
    assert audit.setting_value('audit_log_store_ip', 'Truncated') == 'truncated'
    assert audit.setting_value('users_can_delete', '') is None
    assert audit.SETTING_VALUE_KEYS >= {'audit_log_enabled', 'audit_log_retention_days',
                                        'audit_log_store_ip', 'audit_log_record_views'}


def test_temperatures_and_visible_models_record_the_key_only(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    start = max_id()
    resp = client.post('/admin/llm-temperatures', json={'values': {'summary': 0.3}})
    assert resp.status_code == 200, resp.data[:300]
    resp = client.post('/admin/llm-temperatures', json={'values': {'summary': None}})
    assert resp.status_code == 200
    rows = rows_after(start, 'admin.setting.changed')
    assert [r.details for r in rows] == [{'key': 'llm_temperature_summary'}] * 2


def test_the_manual_retention_run_records_counts(created, monkeypatch):
    from src.api import admin as admin_module
    admin = make_user(created, prefix='adm', is_admin=True)
    monkeypatch.setattr(admin_module, 'process_auto_deletion', lambda: {
        'checked': 7, 'deleted_audio_only': 0, 'deleted_full': 2, 'exempted': 1,
        'skipped_no_retention': 4, 'errors': 0, 'speaker_cleanup_error': 'SENTINEL text'})
    start = max_id()
    assert client_for(admin).post('/admin/auto-deletion/run').status_code == 200
    rows = rows_after(start)
    assert names(rows) == ['admin.retention.run']
    assert rows[0].details == {'checked': 7, 'deleted_audio_only': 0, 'deleted_full': 2, 'errors': 0}
    assert 'SENTINEL' not in _dump(rows)


def test_inquire_processing_answers_ids_without_titles_and_is_recorded(created, monkeypatch):
    from src.api import admin as admin_module
    admin = make_user(created, prefix='adm', is_admin=True)
    owner = make_user(created, prefix='own')
    good = make_recording(owner, title='SENTINEL good title')
    bad = make_recording(owner, title='SENTINEL bad title')
    calls = []

    def chunks(recording_id):
        calls.append(recording_id)
        if recording_id == bad.id:
            raise RuntimeError('SENTINEL transcript passage')
        return True

    monkeypatch.setattr(admin_module, 'process_recording_chunks', chunks)
    start = max_id()
    resp = client_for(admin).post('/admin/inquire/process-recordings', json={'retry_passes': 0})
    assert resp.status_code == 200, resp.data[:300]
    assert 'SENTINEL' not in resp.get_data(as_text=True)
    body = resp.get_json()
    assert good.id in calls
    # The id, the kind of the error and the number of attempts: no title, no error text.
    assert [f for f in body['failed'] if f['id'] == bad.id] == [
        {'id': bad.id, 'reason': 'RuntimeError length=27', 'attempts': 1}]
    assert all(set(f) == {'id', 'reason', 'attempts'} for f in body['failed'])
    rows = rows_after(start)
    assert names(rows) == ['admin.inquire.processed']
    assert rows[0].actor_user_id == admin.id and rows[0].recording_id is None
    assert rows[0].details == {'processed': body['processed'], 'failed': len(body['failed']),
                               'total': body['total'], 'force': False}
    assert rows[0].details['failed'] >= 1 and rows[0].details['processed'] >= 1
    assert 'SENTINEL' not in _dump(rows)


def test_the_older_inquire_route_is_recorded_with_counts(created, monkeypatch):
    from src.api import admin as admin_module
    admin = make_user(created, prefix='adm', is_admin=True)
    owner = make_user(created, prefix='own')
    make_recording(owner, title='SENTINEL title')
    monkeypatch.setattr(admin_module, 'process_recording_chunks', lambda recording_id: True)
    start = max_id()
    resp = client_for(admin).post('/api/admin/migrate_recordings')
    assert resp.status_code == 200 and 'SENTINEL' not in resp.get_data(as_text=True)
    body = resp.get_json()
    rows = rows_after(start)
    assert names(rows) == ['admin.inquire.processed']
    assert rows[0].details == {'processed': body['processed'], 'failed': 0, 'total': body['total'], 'force': False}


def test_the_auto_deletion_preview_route_is_removed(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    assert client_for(admin).get('/admin/auto-deletion/preview').status_code == 404


def test_group_lifecycle_never_records_the_group_name(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    member = make_user(created, prefix='mem')
    client = client_for(admin)
    name = 'audgrp' + uuid.uuid4().hex[:8]
    start = max_id()
    resp = client.post('/api/admin/groups', json={'name': name, 'description': 'SENTINELDESC'})
    assert resp.status_code == 201, resp.data[:300]
    group_id = resp.get_json()['id']
    assert client.put(f'/api/admin/groups/{group_id}', json={'name': name + 'x', 'description': 'SENTINELDESC2'}).status_code == 200
    resp = client.post(f'/api/admin/groups/{group_id}/members', json={'user_id': member.id, 'role': 'member'})
    assert resp.status_code in (200, 201), resp.data[:300]
    assert client.put(f'/api/admin/groups/{group_id}/members/{member.id}', json={'role': 'admin'}).status_code == 200
    assert client.put(f'/api/admin/groups/{group_id}/members/{member.id}', json={'role': 'admin'}).status_code == 200
    assert client.delete(f'/api/admin/groups/{group_id}/members/{member.id}').status_code == 200
    assert client.delete(f'/api/admin/groups/{group_id}').status_code == 200
    rows = rows_after(start)
    assert names(rows) == ['group.created', 'group.changed', 'group.member.added',
                           'group.member.role_changed', 'group.member.removed', 'group.deleted']
    assert all(r.group_id == group_id and r.event_group == 'admin' and r.actor_user_id == admin.id for r in rows)
    assert rows[1].details == {'fields': ['name', 'description']}
    assert (rows[2].target_user_id, rows[2].details) == (member.id, {'role': 'member'})
    assert (rows[3].target_user_id, rows[3].details) == (member.id, {'role': 'admin', 'old_role': 'member'})
    assert rows[4].target_user_id == member.id and rows[5].details is None
    dump = _dump(rows)
    assert name not in dump and 'SENTINEL' not in dump
