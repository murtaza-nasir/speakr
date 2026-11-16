"""Audit log: internal shares, public links, and the copy of the former
share_audit_log table (ledger id 0009).

The public_id of a link is the access secret and reaches no row. Rows of a
share remain after the recording and the accounts are deleted.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.audit_log_support import (  # noqa: E402
    anonymous_client, api_token, app, audit, bearer, client_for, db, make_recording, make_user,
    max_id, names, remove_users, reset_audit_settings, rows_after, set_setting,
)
from src.models import AuditEvent, InternalShare, Recording, Share, ShareAuditLog, SystemSetting  # noqa: E402
from src.services.audit_import import copy_share_audit_log  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        set_setting('enable_internal_sharing', 'true')
        set_setting('enable_public_sharing', 'true')
        set_setting('user_search_scope', 'everyone')
        yield users
        audit.flush()
        db.session.rollback()
        for key in ('enable_internal_sharing', 'enable_public_sharing', 'user_search_scope'):
            SystemSetting.set_setting(key, None, None, None)
        ShareAuditLog.query.delete()
        db.session.commit()
        remove_users(users)


def _dump(rows):
    return json.dumps([r.to_dict() for r in rows])


# --- Internal shares ------------------------------------------------------------

def test_internal_share_created_changed_and_revoked(created):
    owner = make_user(created)
    reader = make_user(created, prefix='rdr')
    recording = make_recording(owner, title='SENTINELTITLE')
    client = client_for(owner)
    start = max_id()
    resp = client.post(f'/api/recordings/{recording.id}/share-internal',
                       json={'user_id': reader.id, 'can_edit': False, 'can_reshare': False})
    assert resp.status_code == 201, resp.data[:300]
    share_id = resp.get_json()['share']['id']
    assert client.patch(f'/api/internal-shares/{share_id}', json={'can_edit': True}).status_code == 200
    # A request that changes nothing writes nothing.
    assert client.patch(f'/api/internal-shares/{share_id}', json={'can_edit': True}).status_code == 200
    assert client.delete(f'/api/internal-shares/{share_id}').status_code == 200
    rows = rows_after(start)
    assert names(rows) == ['share.internal.created', 'share.internal.changed', 'share.internal.revoked']
    assert rows[0].details == {'share_id': share_id, 'can_edit': False, 'can_reshare': False}
    assert rows[1].details == {'share_id': share_id, 'can_edit': True, 'can_reshare': False,
                               'old_can_edit': False, 'old_can_reshare': False}
    assert rows[2].details == {'share_id': share_id}
    for row in rows:
        assert (row.recording_id, row.recording_owner_id, row.actor_user_id, row.target_user_id) == \
            (recording.id, owner.id, owner.id, reader.id)
        assert row.event_group == 'share' and row.actor_kind == 'session'
    assert 'SENTINEL' not in _dump(rows) and reader.username not in _dump(rows)
    # Nothing is written to the former table any more.
    assert ShareAuditLog.query.count() == 0


def test_a_cascade_revocation_is_marked(created):
    owner = make_user(created)
    middle = make_user(created, prefix='mid')
    last = make_user(created, prefix='lst')
    recording = make_recording(owner)
    first = InternalShare(recording_id=recording.id, owner_id=owner.id, shared_with_user_id=middle.id,
                          can_edit=True, can_reshare=True)
    second = InternalShare(recording_id=recording.id, owner_id=middle.id, shared_with_user_id=last.id,
                           can_edit=False, can_reshare=False)
    db.session.add_all([first, second])
    db.session.commit()
    first_id, second_id = first.id, second.id
    start = max_id()
    assert client_for(owner).delete(f'/api/internal-shares/{first_id}').status_code == 200
    rows = rows_after(start, 'share.internal.revoked')
    assert [(r.target_user_id, r.details) for r in rows] == [
        (last.id, {'share_id': second_id, 'cascade': True}), (middle.id, {'share_id': first_id})]


def test_share_rows_survive_the_recording_and_the_accounts(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    owner = make_user(created)
    reader = make_user(created, prefix='rdr')
    recording = make_recording(owner)
    recording_id, owner_id, reader_id = recording.id, owner.id, reader.id
    start = max_id()
    resp = client_for(owner).post(f'/api/recordings/{recording_id}/share-internal', json={'user_id': reader_id})
    assert resp.status_code == 201, resp.data[:300]
    client = client_for(admin)
    assert client.delete(f'/admin/users/{reader_id}').status_code == 200
    created.remove(reader_id)
    assert client.delete(f'/admin/users/{owner_id}').status_code == 200
    created.remove(owner_id)
    assert db.session.get(Recording, recording_id) is None
    rows = rows_after(start, 'share.internal.created')
    assert [(r.recording_id, r.recording_owner_id, r.actor_user_id, r.target_user_id) for r in rows] == [
        (recording_id, owner_id, owner_id, reader_id)]


def test_deleting_the_target_of_a_former_share_audit_row_succeeds(created):
    """A row of the former table that names the account as target must not
    stop the deletion of the account (a foreign key without ON DELETE)."""
    admin = make_user(created, prefix='adm', is_admin=True)
    owner = make_user(created)
    reader = make_user(created, prefix='rdr')
    recording = make_recording(owner)
    db.session.add(ShareAuditLog(action='created', recording_id=recording.id, actor_id=owner.id,
                                 target_user_id=reader.id, permissions_granted={'can_edit': False}))
    db.session.commit()
    reader_id = reader.id
    resp = client_for(admin).delete(f'/admin/users/{reader_id}')
    assert resp.status_code == 200, resp.data[:300]
    created.remove(reader_id)
    assert ShareAuditLog.query.filter_by(target_user_id=reader_id).count() == 0


# --- Public links -----------------------------------------------------------------

def _https():
    from unittest import mock
    from src.api import shares as shares_module
    return mock.patch.object(shares_module, 'link_is_https', return_value=True)


def test_public_link_lifecycle_never_records_the_public_id(created):
    owner = make_user(created, can_share_publicly=True)
    recording = make_recording(owner, title='SENTINELTITLE')
    client = client_for(owner)
    start = max_id()
    with _https():
        resp = client.post(f'/api/recording/{recording.id}/share', json={'share_summary': True, 'share_notes': False})
        assert resp.status_code == 201, resp.data[:300]
        share = resp.get_json()['share']
        share_id, public_id = share['id'], share['public_id']
        # The same request again reuses the link and changes nothing.
        assert client.post(f'/api/recording/{recording.id}/share',
                           json={'share_summary': True, 'share_notes': False}).status_code == 200
        # Other flags on the reused link are a change.
        assert client.post(f'/api/recording/{recording.id}/share',
                           json={'share_summary': False, 'share_notes': False}).status_code == 200
    assert client.put(f'/api/share/{share_id}', json={'share_notes': True}).status_code == 200

    visitor = anonymous_client()
    for _ in range(5):
        assert visitor.get(f'/share/{public_id}', headers={'User-Agent': 'visitor-a'}).status_code == 200
    assert anonymous_client().get(f'/share/{public_id}', headers={'User-Agent': 'visitor-b'}).status_code == 200
    assert anonymous_client().get('/share/doesnotexist000').status_code == 404

    client = client_for(owner)
    assert client.delete(f'/api/share/{share_id}').status_code == 200
    rows = rows_after(start)
    in_tx = [r for r in rows if r.event != 'share.public.opened']
    assert names(in_tx) == ['share.public.created', 'share.public.changed', 'share.public.changed',
                            'share.public.revoked']
    assert in_tx[0].details == {'share_id': share_id, 'share_summary': True, 'share_notes': False, 'expires': False}
    assert in_tx[1].details == {'share_id': share_id, 'share_summary': False, 'share_notes': False, 'expires': False}
    assert in_tx[2].details == {'share_id': share_id, 'share_summary': False, 'share_notes': True, 'expires': False}
    assert in_tx[3].details == {'share_id': share_id}
    assert all(r.recording_id == recording.id and r.recording_owner_id == owner.id for r in rows)
    opened = [r for r in rows if r.event == 'share.public.opened']
    assert len(opened) == 2
    assert all(r.actor_kind == 'public_link' and r.actor_user_id is None for r in opened)
    assert all(r.details == {'share_id': share_id} for r in opened)
    dump = _dump(rows) + json.dumps([r.dedup_key for r in rows])
    assert public_id not in dump and 'SENTINEL' not in dump


def test_public_link_events_stay_while_views_are_off(created):
    owner = make_user(created, can_share_publicly=True)
    recording = make_recording(owner)
    share = Share(recording_id=recording.id, user_id=owner.id, share_summary=True, share_notes=True)
    db.session.add(share)
    db.session.commit()
    set_setting('audit_log_record_views', 'false')
    start = max_id()
    assert anonymous_client().get(f'/share/{share.public_id}').status_code == 200
    assert names(rows_after(start)) == ['share.public.opened']


def test_api_v1_public_link(created):
    owner = make_user(created, can_share_publicly=True)
    recording = make_recording(owner)
    plain, token_id = api_token(owner)
    start = max_id()
    with _https():
        from unittest import mock
        import src.services.public_shares as ps
        with mock.patch.object(ps, 'link_is_https', return_value=True):
            resp = anonymous_client().post(f'/api/v1/recordings/{recording.id}/share',
                                           json={'expires_in_days': 3}, headers=bearer(plain))
    assert resp.status_code in (200, 201), resp.data[:300]
    rows = rows_after(start, 'share.public.created')
    assert len(rows) == 1 and rows[0].actor_kind == 'api_token' and rows[0].actor_token_id == token_id
    assert rows[0].details['expires'] is True


# --- The copy of share_audit_log (0009) ---------------------------------------------

def _old_rows(owner, reader, recording):
    base = datetime(2026, 3, 1, 10, 0, 0)
    rows = [
        ShareAuditLog(action='created', recording_id=recording.id, actor_id=owner.id, target_user_id=reader.id,
                      permissions_granted={'can_edit': True, 'can_reshare': False},
                      actor_permissions={'can_edit': True}, timestamp=base, share_id=None,
                      notes='SENTINELNOTE Shared by owner', ip_address='203.0.113.5'),
        ShareAuditLog(action='modified', recording_id=recording.id, actor_id=owner.id, target_user_id=reader.id,
                      permissions_granted={'old': {'can_edit': True, 'can_reshare': False},
                                           'new': {'can_edit': False, 'can_reshare': True}},
                      timestamp=base + timedelta(minutes=1), share_id=41, ip_address='203.0.113.5'),
        ShareAuditLog(action='cascade_revoked', recording_id=recording.id, actor_id=owner.id,
                      target_user_id=reader.id, timestamp=base + timedelta(minutes=2), share_id=42,
                      notes='SENTINELNOTE Cascaded from revoking user 5'),
        ShareAuditLog(action='revoked', recording_id=recording.id, actor_id=owner.id, target_user_id=reader.id,
                      timestamp=base + timedelta(minutes=3), share_id=41, ip_address='2001:db8::7'),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return base, [r.id for r in rows]


def test_the_copy_keeps_the_values_and_runs_twice_without_duplicates(created):
    owner = make_user(created)
    reader = make_user(created, prefix='rdr')
    recording = make_recording(owner)
    base, old_ids = _old_rows(owner, reader, recording)
    start = max_id()
    assert copy_share_audit_log(db.engine) == 4
    assert copy_share_audit_log(db.engine) == 4
    rows = rows_after(start)
    assert names(rows) == ['share.internal.created', 'share.internal.changed', 'share.internal.revoked',
                           'share.internal.revoked']
    assert [r.created_at for r in rows] == [base + timedelta(minutes=i) for i in range(4)]
    assert [r.details for r in rows] == [
        {'imported': True, 'can_edit': True, 'can_reshare': False},
        {'imported': True, 'share_id': 41, 'can_edit': False, 'can_reshare': True,
         'old_can_edit': True, 'old_can_reshare': False},
        {'imported': True, 'share_id': 42, 'cascade': True},
        {'imported': True, 'share_id': 41},
    ]
    assert [r.ip for r in rows] == ['203.0.113.5', '203.0.113.5', None, '2001:db8::7']
    for row in rows:
        assert (row.recording_id, row.recording_owner_id, row.actor_user_id, row.target_user_id) == \
            (recording.id, owner.id, owner.id, reader.id)
        assert row.event_group == 'share' and row.actor_kind == 'session' and row.outcome == 'success'
    assert [r.dedup_key for r in rows] == [f'share_audit_log:{i}' for i in old_ids]
    assert 'SENTINELNOTE' not in _dump(rows)
    # The old table is not touched.
    assert ShareAuditLog.query.count() == 4
    db.session.execute(text("DELETE FROM audit_event WHERE dedup_key LIKE 'share_audit_log:%'"))
    db.session.commit()


def test_the_copy_is_registered_once_in_the_ledger():
    source = (ROOT / 'src' / 'init_db.py').read_text(encoding='utf-8')
    assert "run_once(engine, '0009_copy_share_audit_log_to_audit_event'" in source
    assert source.count("'0009_") == 1
    with app.app_context():
        assert db.session.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = "
            "'0009_copy_share_audit_log_to_audit_event'")).scalar() == 1


sqlite_only = pytest.mark.skipif(
    not os.environ.get('SQLALCHEMY_DATABASE_URI', '').startswith('sqlite'),
    reason='boots three processes on a database file of its own')


@sqlite_only
def test_three_concurrent_boots_copy_every_row_once(tmp_path):
    """Three processes run initialize_database() at the same time on a
    database that holds old share rows and no ledger entry for 0009."""
    database = tmp_path / 'boot.db'
    env = dict(os.environ, SQLALCHEMY_DATABASE_URI=f'sqlite:///{database}',
               UPLOAD_FOLDER=str(tmp_path / 'uploads'), SPEAKR_SKIP_BACKGROUND_TASKS='true',
               PYTHONPATH=str(ROOT))
    env.pop('PYTEST_DATABASE_URI', None)
    seed = textwrap.dedent('''
        from datetime import datetime
        from src.app import app, db
        from src.init_db import initialize_database
        from src.models import User, Recording, ShareAuditLog
        from sqlalchemy import text
        with app.app_context():
            initialize_database(app)
            a = User(username='own', email='own@local.test', password='x')
            b = User(username='rdr', email='rdr@local.test', password='x')
            db.session.add_all([a, b]); db.session.commit()
            r = Recording(user_id=a.id, title='t', status='COMPLETED'); db.session.add(r); db.session.commit()
            for i in range(120):
                db.session.add(ShareAuditLog(action='created', recording_id=r.id, actor_id=a.id,
                                             target_user_id=b.id, permissions_granted={'can_edit': bool(i % 2)},
                                             timestamp=datetime(2026, 1, 1, 0, 0, i % 60)))
            db.session.commit()
            db.session.execute(text("DELETE FROM schema_migrations WHERE migration_id LIKE '0009_%'"))
            db.session.execute(text("DELETE FROM audit_event"))
            db.session.commit()
    ''')
    boot = textwrap.dedent('''
        from src.app import app
        from src.init_db import initialize_database
        with app.app_context():
            initialize_database(app)
    ''')
    done = subprocess.run([sys.executable, '-c', seed], cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=100)
    assert done.returncode == 0, done.stderr[-3000:]
    workers = [subprocess.Popen([sys.executable, '-c', boot], cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for worker in workers:
        _out, err = worker.communicate(timeout=100)
        assert worker.returncode == 0, err[-3000:]
    import sqlite3
    conn = sqlite3.connect(str(database))
    try:
        assert conn.execute("SELECT COUNT(*) FROM audit_event WHERE event = 'share.internal.created'").fetchone()[0] == 120
        assert conn.execute("SELECT COUNT(DISTINCT dedup_key) FROM audit_event").fetchone()[0] == 120
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE migration_id LIKE '0009_%'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM share_audit_log").fetchone()[0] == 120
    finally:
        conn.close()
