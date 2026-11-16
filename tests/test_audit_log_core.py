"""The audit log service (src/services/audit.py): the two write paths, the
details allowlist, de-duplication, the settings, pruning, the fallback line,
and the append-only rule.

The events of each route are covered in tests/test_audit_log_events.py.
"""

import contextlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import AuditEvent, SystemSetting  # noqa: E402
from src.services import audit  # noqa: E402
from src.utils.incognito_logging import incognito_context  # noqa: E402
from src.utils.system_settings import ENV_BACKED_SETTINGS  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

ROOT = Path(__file__).resolve().parent.parent
AUDIT_KEYS = ('audit_log_enabled', 'audit_log_retention_days', 'audit_log_store_ip', 'audit_log_record_views')


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Default settings, an empty queue and an empty de-duplication map."""
    for key in AUDIT_KEYS:
        monkeypatch.delenv(ENV_BACKED_SETTINGS[key][0], raising=False)
    with app.app_context():
        for key in AUDIT_KEYS:
            SystemSetting.set_setting(key, None, ENV_BACKED_SETTINGS[key][3], ENV_BACKED_SETTINGS[key][2])
    audit.flush()
    audit.reset_memory()
    yield
    audit.flush()
    audit.reset_memory()
    with app.app_context():
        db.session.rollback()
        for key in AUDIT_KEYS:
            SystemSetting.set_setting(key, None, ENV_BACKED_SETTINGS[key][3], ENV_BACKED_SETTINGS[key][2])


def _set(key, value):
    SystemSetting.set_setting(key, value, None, ENV_BACKED_SETTINGS[key][2])


def _max_id():
    return db.session.execute(text('SELECT COALESCE(MAX(id), 0) FROM audit_event')).scalar()


def _rows_after(start, event=None):
    db.session.rollback()
    query = AuditEvent.query.filter(AuditEvent.id > start)
    if event:
        query = query.filter_by(event=event)
    return query.order_by(AuditEvent.id).all()


# --- Registry and settings ---------------------------------------------------

def test_every_event_has_a_known_group_and_fits_the_column():
    for name, (group, keys) in audit.EVENTS.items():
        assert group in audit.EVENT_GROUPS, name
        assert len(name) <= 48, name
        assert isinstance(keys, frozenset)


def test_the_four_settings_are_env_backed_with_the_documented_defaults():
    assert ENV_BACKED_SETTINGS['audit_log_enabled'][:3] == ('AUDIT_LOG_ENABLED', True, 'boolean')
    assert ENV_BACKED_SETTINGS['audit_log_retention_days'][:3] == ('AUDIT_LOG_RETENTION_DAYS', 365, 'integer')
    assert ENV_BACKED_SETTINGS['audit_log_store_ip'][:3] == ('AUDIT_LOG_STORE_IP', 'full', 'string')
    assert ENV_BACKED_SETTINGS['audit_log_record_views'][:3] == ('AUDIT_LOG_RECORD_VIEWS', True, 'boolean')


def test_env_applies_until_a_value_is_saved(monkeypatch):
    with app.app_context():
        assert audit.is_enabled() is True and audit.retention_days() == 365
        assert audit.ip_mode() == 'full' and audit.views_recorded() is True
    monkeypatch.setenv('AUDIT_LOG_ENABLED', 'false')
    monkeypatch.setenv('AUDIT_LOG_RETENTION_DAYS', '2190')
    monkeypatch.setenv('AUDIT_LOG_STORE_IP', 'truncated')
    with app.app_context():
        assert audit.is_enabled() is False and audit.retention_days() == 2190
        assert audit.ip_mode() == 'truncated'
    with app.app_context():
        _set('audit_log_enabled', 'true')
    with app.app_context():
        assert audit.is_enabled() is True


@pytest.mark.parametrize('mode,address,expected', [
    ('full', '203.0.113.77', '203.0.113.77'),
    ('truncated', '203.0.113.77', '203.0.113.0'),
    ('truncated', '2001:db8:abcd:12:34:56:78:9a', '2001:db8:abcd::'),
    ('truncated', '::ffff:203.0.113.77', '203.0.113.0'),
    ('truncated', 'not an address', None),
    ('off', '203.0.113.77', None),
    ('full', None, None),
])
def test_stored_ip(mode, address, expected):
    assert audit.stored_ip(address, mode) == expected


def test_an_unknown_ip_mode_counts_as_full(monkeypatch):
    monkeypatch.setenv('AUDIT_LOG_STORE_IP', 'everything')
    with app.app_context():
        assert audit.ip_mode() == 'full'


# --- Details allowlist -------------------------------------------------------

def test_a_details_key_that_is_not_listed_raises_in_tests():
    with app.app_context():
        with pytest.raises(ValueError):
            audit.record('recording.viewed', recording_id=1, details={'title': 'x'})
        with pytest.raises(ValueError):
            audit.record_in_tx('token.created', details={'name': 'x'})
        db.session.rollback()


def test_a_details_key_that_is_not_listed_is_dropped_in_production():
    with app.app_context():
        start = _max_id()
        app.config['TESTING'] = False
        try:
            audit.record_in_tx('token.created', details={'name': 'SENTINEL', 'token_id': 5})
            db.session.commit()
        finally:
            app.config['TESTING'] = True
        row = _rows_after(start, 'token.created')[0]
        assert row.details == {'token_id': 5}


def test_unknown_event_and_outcome_raise():
    with app.app_context():
        with pytest.raises(ValueError):
            audit.record('recording.opened')
        with pytest.raises(ValueError):
            audit.record('recording.viewed', outcome='ok')


def test_no_event_allows_a_content_key():
    forbidden = {'title', 'name', 'filename', 'file_name', 'original_filename', 'text', 'transcript',
                 'query', 'q', 'public_id', 'email', 'password', 'error', 'message', 'notes', 'summary',
                 'token', 'token_name', 'size', 'duration'}
    for name, (_group, keys) in audit.EVENTS.items():
        assert not (keys & forbidden), name
    assert audit.EVENTS['incognito.used'][1] == frozenset()


# --- The two write paths ------------------------------------------------------

def test_a_queued_event_is_written_by_the_writer_thread():
    with app.app_context():
        start = _max_id()
        assert audit.record('recording.exported', details={'count': 3}) is True
        assert audit.flush()
        row = _rows_after(start, 'recording.exported')[0]
        assert row.event_group == 'access' and row.actor_kind == 'system'
        assert row.outcome == 'success' and row.details == {'count': 3}
        assert row.ip is None and row.dedup_key is None
        assert abs((datetime.utcnow() - row.created_at).total_seconds()) < 60


def test_an_in_tx_event_exists_exactly_when_the_action_was_committed():
    with app.app_context():
        start = _max_id()
        audit.record_in_tx('admin.user.unlocked', target_user_id=987654)
        db.session.rollback()
        assert _rows_after(start) == []
        audit.record_in_tx('admin.user.unlocked', target_user_id=987654)
        db.session.commit()
        rows = _rows_after(start)
        assert [r.event for r in rows] == ['admin.user.unlocked'] and rows[0].target_user_id == 987654


def test_request_values_are_stored_and_trimmed():
    with app.test_request_context('/x', environ_base={'REMOTE_ADDR': '198.51.100.9'},
                                  headers={'User-Agent': 'A' * 400}):
        start = _max_id()
        audit.record_in_tx('auth.login.failed', target_user_id=5, details={'reason': 'bad_password'})
        db.session.commit()
        row = _rows_after(start)[0]
        assert row.actor_kind == 'anonymous' and row.ip == '198.51.100.9'
        assert row.user_agent == 'A' * 160


def test_ids_are_not_given_out_again():
    with app.app_context():
        audit.record_in_tx('admin.user.unlocked')
        db.session.commit()
        top = _max_id()
        db.session.execute(text('DELETE FROM audit_event WHERE id = :id'), {'id': top})
        db.session.commit()
        audit.record_in_tx('admin.user.unlocked')
        db.session.commit()
        assert _max_id() > top


# --- De-duplication -----------------------------------------------------------

def test_fifty_repeats_give_one_row():
    with app.app_context():
        start = _max_id()
        for _ in range(50):
            audit.record('recording.media.played', recording_id=424242, recording_owner_id=1,
                         actor_kind='session', actor_user_id=77, dedup=audit.DEDUP_VIEW)
        audit.flush()
        assert len(_rows_after(start, 'recording.media.played')) == 1


def test_two_workers_give_one_row_and_the_next_bucket_a_second(monkeypatch):
    with app.app_context():
        start = _max_id()
        base = (int(time.time()) // audit.DEDUP_VIEW) * audit.DEDUP_VIEW + 10
        monkeypatch.setattr(audit.time, 'time', lambda: base)
        fields = dict(recording_id=424243, actor_kind='session', actor_user_id=78, dedup=audit.DEDUP_VIEW)
        audit.record('recording.viewed', **fields)
        audit.flush()
        # Another worker has its own memory: the unique key decides.
        audit.reset_memory()
        assert audit.record('recording.viewed', **fields) is True
        audit.flush()
        assert len(_rows_after(start, 'recording.viewed')) == 1
        # 10:29 and 10:31: the buckets are fixed.
        monkeypatch.setattr(audit.time, 'time', lambda: base + audit.DEDUP_VIEW)
        audit.record('recording.viewed', **fields)
        audit.flush()
        assert len(_rows_after(start, 'recording.viewed')) == 2


def test_another_actor_recording_or_outcome_is_a_separate_row():
    with app.app_context():
        start = _max_id()
        common = dict(actor_kind='session', dedup=audit.DEDUP_VIEW)
        audit.record('recording.viewed', recording_id=1, actor_user_id=1, **common)
        audit.record('recording.viewed', recording_id=1, actor_user_id=2, **common)
        audit.record('recording.viewed', recording_id=2, actor_user_id=1, **common)
        audit.record('recording.viewed', recording_id=1, actor_user_id=1, outcome='denied', **common)
        audit.record('recording.content.read', recording_id=1, actor_user_id=1,
                     details={'part': 'transcript'}, dedup_extra='transcript', **common)
        audit.record('recording.content.read', recording_id=1, actor_user_id=1,
                     details={'part': 'summary'}, dedup_extra='summary', **common)
        audit.flush()
        assert len(_rows_after(start)) == 6
        assert all(len(r.dedup_key) <= 80 for r in _rows_after(start))


# --- Settings -----------------------------------------------------------------

def test_off_records_nothing_except_the_log_itself_and_forced_events():
    with app.app_context():
        _set('audit_log_enabled', 'false')
        start = _max_id()
        assert audit.record('recording.exported', details={'count': 1}) is False
        assert audit.record_in_tx('admin.user.unlocked') is None
        audit.record_in_tx('audit.viewed', details={'filters': []})
        audit.record_in_tx('admin.setting.changed', force=True,
                           details={'key': 'audit_log_enabled', 'old': 'true', 'new': 'false'})
        db.session.commit()
        audit.flush()
        assert sorted(r.event for r in _rows_after(start)) == ['admin.setting.changed', 'audit.viewed']


def test_views_off_drops_views_and_plays_only():
    with app.app_context():
        _set('audit_log_record_views', 'false')
        start = _max_id()
        audit.record('recording.viewed', recording_id=1)
        audit.record('recording.media.played', recording_id=1)
        audit.record('recording.media.downloaded', recording_id=1)
        audit.record('recording.downloaded', recording_id=1, details={'part': 'transcript', 'format': 'docx'})
        audit.record('recording.content.read', recording_id=1, details={'part': 'transcript'})
        audit.record('share.public.opened', recording_id=1, actor_kind='public_link', details={'share_id': 4})
        audit.flush()
        assert sorted(r.event for r in _rows_after(start)) == [
            'recording.content.read', 'recording.downloaded', 'recording.media.downloaded',
            'share.public.opened']


@pytest.mark.parametrize('mode,expected', [('full', '198.51.100.9'), ('truncated', '198.51.100.0'), ('off', None)])
def test_ip_modes_apply_to_new_rows(mode, expected):
    with app.app_context():
        _set('audit_log_store_ip', mode)
    with app.test_request_context('/x', environ_base={'REMOTE_ADDR': '198.51.100.9'}):
        start = _max_id()
        audit.record_in_tx('auth.logout')
        db.session.commit()
        assert _rows_after(start)[0].ip == expected


# --- Incognito ----------------------------------------------------------------

def test_inside_an_incognito_request_only_incognito_used_is_recorded():
    with app.app_context():
        start = _max_id()
        with incognito_context():
            assert audit.record('recording.exported', details={'count': 1}) is False
            assert audit.record_in_tx('recording.edited', recording_id=1, details={'fields': ['title']}) is None
            assert audit.record('incognito.used', actor_kind='session', actor_user_id=5) is True
        db.session.commit()
        audit.flush()
        rows = _rows_after(start)
        assert [r.event for r in rows] == ['incognito.used']
        row = rows[0]
        assert (row.actor_user_id, row.outcome, row.details, row.recording_id) == (5, 'success', None, None)


def test_the_fallback_line_is_not_withheld_inside_an_incognito_request(monkeypatch, caplog):
    from src.utils import incognito_logging
    incognito_logging.install()

    class Full:
        def put_nowait(self, item):
            raise audit.queue.Full()

        def qsize(self):
            return 0

        def empty(self):
            return True

    with app.app_context():
        monkeypatch.setattr(audit, '_queue', Full())
        with caplog.at_level(logging.ERROR, logger='speakr.audit'):
            with incognito_context():
                assert audit.record('incognito.used', actor_kind='session', actor_user_id=5,
                                    outcome='failed') is False
                logging.getLogger('some.other').error('secret words')
        lines = [r.getMessage() for r in caplog.records if r.name == 'speakr.audit']
        assert len(lines) == 1 and lines[0].startswith('AUDIT-FALLBACK ')
        payload = json.loads(lines[0][len('AUDIT-FALLBACK '):])
        assert payload['event'] == 'incognito.used' and payload['actor_user_id'] == 5
        assert payload['outcome'] == 'failed'
        assert set(payload) <= {'event', 'event_group', 'actor_kind', 'actor_user_id', 'outcome', 'created_at'}
        assert not any('secret words' in r.getMessage() for r in caplog.records)
        with audit._lost_lock:
            audit._lost.clear()


# --- Pruning ------------------------------------------------------------------

def _old_row(days, marker):
    audit.record_in_tx('admin.user.unlocked', target_user_id=marker,
                       created_at=datetime.utcnow() - timedelta(days=days))


def test_pruning_respects_the_cutoff_and_records_the_run(monkeypatch):
    with app.app_context():
        start = _max_id()
        _old_row(400, 111)
        _old_row(366, 112)
        _old_row(364, 113)
        _old_row(1, 114)
        db.session.commit()
        monkeypatch.setattr(audit, 'PRUNE_BATCH', 1)
        removed = audit.prune_audit_events()
        assert removed >= 2
        rows = _rows_after(start)
        assert [r.target_user_id for r in rows if r.event == 'admin.user.unlocked'] == [113, 114]
        pruned = [r for r in rows if r.event == 'audit.pruned']
        assert len(pruned) == 1 and pruned[0].details['count'] == removed
        assert pruned[0].actor_kind == 'system' and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d', pruned[0].details['cutoff'])
        # Nothing left to remove: no further row.
        assert audit.prune_audit_events() == 0
        assert len([r for r in _rows_after(start) if r.event == 'audit.pruned']) == 1


def test_retention_zero_keeps_everything():
    with app.app_context():
        _set('audit_log_retention_days', '0')
        start = _max_id()
        _old_row(5000, 121)
        db.session.commit()
        assert audit.prune_audit_events() == 0
        assert [r.target_user_id for r in _rows_after(start)] == [121]
        _set('audit_log_retention_days', '30')
        assert audit.prune_audit_events() >= 1
        assert [r.event for r in _rows_after(start)] == ['audit.pruned']


def test_the_daily_maintenance_prunes_the_audit_log(monkeypatch):
    from src.config import startup
    called = []
    monkeypatch.setattr(audit, 'prune_audit_events', lambda: called.append(1) or 0)
    monkeypatch.setattr(startup, 'scheduled_deletion_due', lambda: False)
    import src.services.ownerless_cleanup as oc
    monkeypatch.setattr(oc, 'run_ownerless_cleanup', lambda: None)
    startup.run_daily_maintenance(app)
    assert called == [1]


# --- Append-only --------------------------------------------------------------

def test_the_orm_refuses_update_and_delete():
    with app.app_context():
        row = audit.record_in_tx('admin.user.unlocked')
        db.session.commit()
        row.outcome = 'denied'
        with pytest.raises(RuntimeError):
            db.session.commit()
        db.session.rollback()
        row = db.session.get(AuditEvent, row.id)
        db.session.delete(row)
        with pytest.raises(RuntimeError):
            db.session.commit()
        db.session.rollback()
        assert db.session.get(AuditEvent, row.id).outcome == 'success'


def test_no_other_code_updates_or_deletes_the_table():
    """The only delete is the pruning statement; nothing updates a row."""
    offenders = []
    patterns = [
        re.compile(r'AuditEvent\.query[^\n]*\.(delete|update)\('),
        re.compile(r'query\(AuditEvent\)[^\n]*\.(delete|update)\('),
        re.compile(r'(delete|update)\(\s*AuditEvent\b'),
        re.compile(r'(DELETE\s+FROM|UPDATE)\s+"?audit_event\b', re.I),
        re.compile(r'session\.delete\([^)]*audit_event', re.I),
    ]
    for path in list((ROOT / 'src').rglob('*.py')) + list((ROOT / 'scripts').rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        for pattern in patterns:
            if pattern.search(source):
                offenders.append(f'{path.relative_to(ROOT)}: {pattern.pattern}')
    assert offenders == []
    service = (ROOT / 'src' / 'services' / 'audit.py').read_text(encoding='utf-8')
    assert service.count('delete(table)') == 1
    assert 'update(' not in service
    routes = (ROOT / 'src' / 'api' / 'admin.py').read_text(encoding='utf-8')
    for line in routes.splitlines():
        if 'audit-log' in line and 'route(' in line:
            assert "methods=['GET']" in line, line


# --- A database that cannot be written ---------------------------------------

@contextlib.contextmanager
def _locked_by_another_writer():
    db.session.rollback()
    conn = sqlite3.connect(db.engine.url.database, timeout=1, isolation_level=None)
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
    finally:
        conn.execute('ROLLBACK')
        conn.close()


sqlite_only = pytest.mark.skipif(
    not os.environ.get('SQLALCHEMY_DATABASE_URI', '').startswith('sqlite'),
    reason='holds the single SQLite writer')


@sqlite_only
def test_a_locked_database_does_not_hold_the_request_and_the_row_follows(monkeypatch):
    monkeypatch.setattr(audit, 'RETRY_DELAYS', (0.2, 0.2, 0.2, 0.2, 0.2, 0.2))
    with app.app_context():
        monkeypatch.setattr(audit, 'LOCK_WAIT_SECONDS', 0.2)
        audit._engines.clear()
        start = _max_id()
        with _locked_by_another_writer():
            began = time.monotonic()
            assert audit.record('recording.exported', details={'count': 9}) is True
            assert time.monotonic() - began < 0.5
            time.sleep(0.5)
        assert audit.flush(timeout=15)
        assert [r.details for r in _rows_after(start, 'recording.exported')] == [{'count': 9}]
        assert _rows_after(start, 'audit.write_failed') == []
        audit._engines.clear()


@sqlite_only
def test_after_the_retries_the_fallback_line_and_the_write_failed_row_appear(monkeypatch, caplog):
    monkeypatch.setattr(audit, 'RETRY_DELAYS', (0.05,))
    with app.app_context():
        monkeypatch.setattr(audit, 'LOCK_WAIT_SECONDS', 0.1)
        audit._engines.clear()
        start = _max_id()
        with caplog.at_level(logging.ERROR, logger='speakr.audit'):
            with _locked_by_another_writer():
                audit.record('recording.exported', details={'count': 11})
                audit.record('recording.exported', details={'count': 12})
                assert audit.flush(timeout=15)
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('AUDIT-FALLBACK ')]
        assert len(lines) == 2
        payloads = [json.loads(line[len('AUDIT-FALLBACK '):]) for line in lines]
        assert sorted(p['details']['count'] for p in payloads) == [11, 12]
        assert all(p['event'] == 'recording.exported' for p in payloads)
        assert _rows_after(start) == []
        # The next batch that is written records the gap.
        audit.record('recording.exported', details={'count': 13})
        assert audit.flush(timeout=15)
        rows = _rows_after(start)
        assert sorted(r.event for r in rows) == ['audit.write_failed', 'recording.exported']
        failed = [r for r in rows if r.event == 'audit.write_failed'][0]
        assert failed.details == {'count': 2} and failed.outcome == 'failed'
        # Counted once.
        audit.record('recording.exported', details={'count': 14})
        assert audit.flush(timeout=15)
        assert len(_rows_after(start, 'audit.write_failed')) == 1
        audit._engines.clear()
