"""Every route that names a recording is audited, or is listed here as not
audited with the reason. The same holds for the routes on which an
administrator reaches recordings of other users without an id in the address
(ADMIN_AUDITED, ADMIN_NOT_AUDITED).

A new route with ``recording_id`` in its address fails this test until it is
given an audit event (``audit.access`` for a read, ``audit.emits`` beside the
``audit.record_in_tx`` call of a change) or is added to NOT_AUDITED. The
tables are the design: change them deliberately. Modelled on
tests/test_api_v1_scope_coverage.py.

The last test is the content rule as a whole: marker strings placed in every
field a user can fill reach no row and no fallback line.
"""

import json
import logging
import re
import struct
import wave
from pathlib import Path

import pytest

from tests.audit_log_support import (  # noqa: E402
    anonymous_client, api_token, app, audit, bearer, client_for, db, make_recording, make_user,
    max_id, remove_users, reset_audit_settings, rows_after, set_setting, unfreeze_hot_settings,
)
from src.models import AuditEvent, Share, SystemSetting  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

ROOT = Path(__file__).resolve().parent.parent

VIEWED = {'recording.viewed'}
READ = {'recording.content.read'}
MEDIA = {'recording.media.played', 'recording.media.downloaded'}
DOWNLOADED = {'recording.downloaded'}
EDITED = {'recording.edited'}
REPROCESSED = {'recording.reprocessed'}

# endpoint -> the events its route records
AUDITED = {
    # Web interface
    'recordings.get_recording_detail': VIEWED,
    'recordings.get_audio': MEDIA,
    'speakers.get_snippet_audio': {'recording.media.played'},
    'recordings.download_transcript_with_template': DOWNLOADED,
    'recordings.download_summary_word': DOWNLOADED,
    'recordings.download_chat_word': DOWNLOADED,
    'recordings.download_notes_word': DOWNLOADED,
    'events.download_all_events_ics': DOWNLOADED,
    'recordings.update_speakers': EDITED,
    'recordings.update_transcript': EDITED,
    'recordings.update_transcription': EDITED,
    'recordings.toggle_deletion_exempt': EDITED,
    'recordings.generate_summary_endpoint': REPROCESSED,
    'recordings.reprocess_transcription': REPROCESSED,
    'recordings.reprocess_summary': REPROCESSED,
    'recordings.regenerate_title': REPROCESSED,
    'recordings.translate_transcription': REPROCESSED,
    'recordings.delete_recording': {'recording.deleted'},
    'recordings.delete_recording_audio': {'recording.audio.deleted'},
    'recordings.trim_recording_endpoint': {'recording.trimmed'},
    'shares.create_share': {'share.public.created', 'share.public.changed'},
    'shares.share_recording_internal': {'share.internal.created'},
    # API v1
    'api_v1.get_recording': VIEWED,
    'api_v1.get_transcript': READ,
    'api_v1.get_summary': READ,
    'api_v1.get_notes': READ,
    'api_v1.get_recording_speakers': READ,
    'api_v1.get_recording_events': READ,
    'api_v1.download_events_ics': READ,
    'api_v1.download_audio': MEDIA,
    'api_v1.update_recording': EDITED,
    'api_v1.replace_notes': EDITED,
    'api_v1.replace_summary': EDITED,
    'api_v1.assign_speakers': EDITED,
    'api_v1.start_transcription': REPROCESSED,
    'api_v1.start_summarization': REPROCESSED,
    'api_v1.api_regenerate_title': REPROCESSED,
    'api_v1.delete_recording': {'recording.deleted'},
    'api_v1.delete_recording_audio': {'recording.audio.deleted'},
    'api_v1.create_public_share': {'share.public.created'},
}

# endpoint -> why no event is recorded
NOT_AUDITED = {
    # Requested on a timer or with every view of a recording; the view itself is recorded
    'recordings.get_recording_status_only': 'status poll (BACKGROUND_ENDPOINTS)',
    'recordings.get_status': 'status poll (BACKGROUND_ENDPOINTS)',
    'api_v1.get_recording_status': 'status of processing, no content',
    'recordings.get_audio_peaks': 'waveform data, requested with every view',
    'events.get_recording_events': 'requested with every view in the web interface (recording.viewed)',
    'recordings.index': 'the page shell; the content is read through get_recording_detail',
    # Organization and personal state, not content
    'recordings.add_tag_to_recording': 'tag assignment',
    'recordings.remove_tag_from_recording': 'tag assignment',
    'recordings.reorder_recording_tags': 'tag order',
    'api_v1.add_tags_to_recording': 'tag assignment',
    'api_v1.replace_recording_tags': 'tag assignment',
    'api_v1.remove_tag_from_recording': 'tag assignment',
    'folders.assign_recording_folder': 'folder assignment',
    'folders.remove_recording_folder': 'folder assignment',
    'recordings.toggle_inbox': 'personal state of the viewer',
    'recordings.toggle_highlight': 'personal state of the viewer',
    'recordings.toggle_archive': 'personal state of the viewer',
    'api_v1.list_external_refs': 'ids of an integration, no content',
    'api_v1.add_external_ref': 'ids of an integration, no content',
    'api_v1.replace_external_refs': 'ids of an integration, no content',
    'api_v1.delete_external_ref': 'ids of an integration, no content',
    # Lists of shares; creating, changing and revoking a share is recorded
    'shares.get_existing_share': 'reads the link of the owner',
    'shares.get_internal_shares': 'list of the shares of a recording',
    'api_v1.list_public_shares': 'list of the links of a recording',
    # Processing helpers that store no content of their own
    'recordings.get_summary_prompt': 'the prompt of the owner, not the recording',
    'recordings.save_summary_prompt_route': 'the prompt of the owner, not the recording',
    'recordings.reset_status': 'resets a stuck status',
    'recordings.trim_recording_status': 'status of a trim, no content',
    'recordings.process_recording_chunks_endpoint': 'rebuilds the search index',
    'speakers.get_speaker_suggestions': 'names of saved speakers for the dialog',
    # Later phase: questions to a model about a recording
    'recordings.auto_identify_speakers': 'phase 2, with chat: a suggestion from the model, nothing is stored',
    'api_v1.identify_speakers': 'phase 2, with chat: a suggestion from the model, nothing is stored',
    'api_v1.chat_with_recording': 'phase 2: chat events',
}


def _marks(view):
    """The audit events a view declares, through its decorators."""
    seen = set()
    while view is not None and id(view) not in seen:
        seen.add(id(view))
        events = getattr(view, '_audit_events', None)
        if events:
            return set(events)
        view = getattr(view, '__wrapped__', None)
    return set()


def _recording_routes():
    found = {}
    for rule in app.url_map.iter_rules():
        if 'recording_id' in rule.arguments:
            found[rule.endpoint] = _marks(app.view_functions[rule.endpoint])
    return found


def test_every_route_with_a_recording_id_is_audited_or_listed():
    routes = _recording_routes()
    unknown = sorted(name for name in routes if name not in AUDITED and name not in NOT_AUDITED)
    assert not unknown, f"routes with recording_id that are neither audited nor listed: {unknown}"
    both = sorted(set(AUDITED) & set(NOT_AUDITED))
    assert not both, f"listed twice: {both}"
    gone = sorted((set(AUDITED) | set(NOT_AUDITED)) - set(routes))
    assert not gone, f"listed routes that no longer exist: {gone}"


def test_audited_routes_carry_their_events():
    routes = _recording_routes()
    wrong = {name: (sorted(routes[name]), sorted(events)) for name, events in AUDITED.items()
             if routes.get(name) != set(events)}
    assert not wrong, f"audit marks differ from the table (actual, expected): {wrong}"
    marked = sorted(name for name in NOT_AUDITED if routes.get(name))
    assert not marked, f"listed as not audited but marked: {marked}"


def test_every_reason_is_stated():
    assert all(isinstance(reason, str) and len(reason) > 8 for reason in NOT_AUDITED.values())


# --- Routes on which an administrator reaches recordings of other users ---------
#
# The tables above cover the routes that name one recording in their address.
# An administrator also reaches recordings through routes that name none: a
# run over every recording, a list of ids in the body, a job id. Every view
# that an administrator enters with more rights than a user (its address is
# under /admin, or its body tests is_admin) and that reads or acts on
# recordings is audited, or is listed here with the reason. A view that
# returns a title, a file name or content of a recording of another user
# to an administrator has no place in ADMIN_NOT_AUDITED.

ADMIN_AUDITED = {
    'admin.run_auto_deletion': {'admin.retention.run'},
    'admin.admin_process_recordings_for_inquire': {'admin.inquire.processed'},
    'admin.migrate_existing_recordings_api': {'admin.inquire.processed'},
    'admin.admin_delete_user': {'admin.user.deleted', 'recording.deleted'},
    'recordings.bulk_delete_recordings': {'recording.deleted'},
    'api_v1.batch_delete_recordings': {'recording.deleted'},
    'recordings.delete_job': {'recording.job.canceled'},
}

ADMIN_NOT_AUDITED = {
    'admin.admin_get_users': 'number and size of the recordings per account, no id or title',
    'admin.admin_update_user': 'number and size of the recordings of the account (admin.user.changed is recorded)',
    'admin.admin_get_stats': 'counts and sizes of the instance and per account, no id or title',
    'admin.admin_get_stats_overview': 'counts per day and account; the five latest failed recordings by id, '
                                      'account, time and stored error text, no title or file name',
    'admin.get_auto_deletion_stats': 'counts of the retention rules, no id or title',
    'admin.admin_inquire_status': 'counts of the search index, no id or title',
    'api_v1.get_stats': 'counts; with scope=all the totals of the instance, no id or title',
    'recordings.get_recordings_paginated': 'the own list; is_admin only sets can_delete on own recordings',
}

_RECORDING_DATA = re.compile(r'\bRecording\b|TranscriptChunk|ProcessingJob|\.recordings\b|process_recording_chunks|'
                             r'process_auto_deletion|delete_recording_completely|build_overview')
_ADMIN_GATE = re.compile(r'is_admin|admin_required')
# A title, a file name or text of a recording, read from a row.
_RECORDING_CONTENT = re.compile(
    r'\b(recording|rec|r)\.(title|original_filename|transcription|summary|notes|participants|audio_path)\b')


def _admin_recording_routes():
    """endpoint -> (audit marks, source) of the views described above."""
    import inspect
    found = {}
    for rule in app.url_map.iter_rules():
        if 'recording_id' in rule.arguments:
            continue                      # covered by AUDITED and NOT_AUDITED
        view = app.view_functions[rule.endpoint]
        try:
            source = inspect.getsource(inspect.unwrap(view))
        except (OSError, TypeError):
            continue
        if not ('/admin' in rule.rule or _ADMIN_GATE.search(source)):
            continue
        if _RECORDING_DATA.search(source):
            found[rule.endpoint] = (_marks(view), source)
    return found


def test_every_admin_route_that_reaches_recordings_is_audited_or_listed():
    routes = _admin_recording_routes()
    unknown = sorted(name for name in routes if name not in ADMIN_AUDITED and name not in ADMIN_NOT_AUDITED)
    assert not unknown, f"admin routes that reach recordings and are neither audited nor listed: {unknown}"
    both = sorted(set(ADMIN_AUDITED) & set(ADMIN_NOT_AUDITED))
    assert not both, f"listed twice: {both}"
    gone = sorted((set(ADMIN_AUDITED) | set(ADMIN_NOT_AUDITED)) - set(routes))
    assert not gone, f"listed routes that the scan no longer finds: {gone}"
    wrong = {name: (sorted(routes[name][0]), sorted(events)) for name, events in ADMIN_AUDITED.items()
             if routes[name][0] != set(events)}
    assert not wrong, f"audit marks differ from the table (actual, expected): {wrong}"
    marked = sorted(name for name in ADMIN_NOT_AUDITED if routes[name][0])
    assert not marked, f"listed as not audited but marked: {marked}"
    assert all(isinstance(reason, str) and len(reason) > 8 for reason in ADMIN_NOT_AUDITED.values())


def test_no_view_under_admin_reads_a_title_a_file_name_or_content():
    """A view under /admin is answered from counts and ids. The two requests
    that returned titles of recordings of other users (the preview of
    automatic deletion, and the failed recordings of the Ask Ambrose
    processing) did so through recording.title, and the list of the latest
    failed recordings on the statistics page through r.title."""
    import inspect
    reading = []
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith('admin.'):
            continue
        source = inspect.getsource(inspect.unwrap(app.view_functions[rule.endpoint]))
        if _RECORDING_CONTENT.search(source):
            reading.append(rule.endpoint)
    # The statistics of the admin page are built in a service.
    import src.services.admin_stats as admin_stats
    if _RECORDING_CONTENT.search(inspect.getsource(admin_stats)):
        reading.append('src.services.admin_stats')
    # The older Ask Ambrose route tests `recording.transcription` for presence and returns counts.
    assert sorted(set(reading)) == ['admin.migrate_existing_recordings_api'], sorted(set(reading))


def test_the_auto_deletion_preview_is_gone():
    assert not [rule.rule for rule in app.url_map.iter_rules() if 'auto-deletion/preview' in rule.rule]


def test_public_link_routes_record_their_events():
    source = (ROOT / 'src' / 'api' / 'shares.py').read_text(encoding='utf-8')
    opened = source[source.index('def view_shared_recording('):source.index('def get_shared_audio(')]
    played = source[source.index('def get_shared_audio('):source.index('def get_existing_share(')]
    assert "_audit_public_link('share.public.opened', share)" in opened
    assert played.count("_audit_public_link('share.public.media.played', share)") == 2


def test_every_event_of_the_registry_is_recorded_somewhere():
    """An event that no code records is a promise the documentation makes
    and the application does not keep."""
    sources = '\n'.join(path.read_text(encoding='utf-8') for path in (ROOT / 'src').rglob('*.py')
                        if path.name != 'audit.py' or path.parent.name != 'services')
    service = (ROOT / 'src' / 'services' / 'audit.py').read_text(encoding='utf-8')
    registry_end = service.index('EVENT_GROUPS = ')
    after_registry = service[registry_end:]
    missing = []
    for name in audit.EVENTS:
        if name.startswith('share.internal.'):
            assert "f'share.internal.{action}'" in sources
            continue
        if f"'{name}'" not in sources and f"'{name}'" not in after_registry:
            missing.append(name)
    assert not missing, f"events that nothing records: {missing}"


def test_every_event_is_documented():
    page = (ROOT / 'docs' / 'admin-guide' / 'audit-log.md').read_text(encoding='utf-8')
    missing = [name for name in audit.EVENTS if f'`{name}`' not in page]
    assert not missing, f"events missing from docs/admin-guide/audit-log.md: {missing}"
    for key in ('audit_log_enabled', 'audit_log_retention_days', 'audit_log_store_ip', 'audit_log_record_views'):
        assert f'`{key}`' in page


# --- No content, in rows and in fallback lines ----------------------------------

MARK = 'ZZSENTINELZZ'


@pytest.fixture
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        # The tests set sharing in System Settings; an earlier test file may
        # have left the switch as a module attribute.
        unfreeze_hot_settings()
        yield users
        audit.flush()
        with audit._lost_lock:
            audit._lost.clear()
        db.session.rollback()
        for key in ('enable_internal_sharing', 'enable_public_sharing', 'user_search_scope'):
            SystemSetting.set_setting(key, None, None, None)
        remove_users(users)


def _wav(tmp_path):
    path = tmp_path / 'audio.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(struct.pack('<h', 0) * 4000)
    return str(path)


def _tour(owner, reader, recording, public_id, token):
    """Reads, changes and shares that carry the marker wherever a user can type."""
    client = client_for(owner)
    base = f'/api/v1/recordings/{recording.id}'
    headers = bearer(token)
    client.get(f'/api/recordings/{recording.id}')
    client.get(f'/audio/{recording.id}').close()
    client.get(f'/audio/{recording.id}?download=true&name={MARK}').close()
    client.get(f'/recording/{recording.id}/download/transcript?format={MARK}')
    client.get(f'/recording/{recording.id}/download/summary?format={MARK}')
    client.post('/save', json={'id': recording.id, 'title': f'{MARK} title', 'notes': f'{MARK} notes',
                               'summary': f'{MARK} summary', 'participants': f'{MARK} person'})
    client.post(f'/recording/{recording.id}/update_transcription', json={'transcription': f'{MARK} text'})
    client.post(f'/api/recordings/{recording.id}/share-internal', json={'user_id': reader.id})
    client.post('/api/tokens', json={'name': f'{MARK} token'})
    client.post('/admin/settings', json={'key': f'{MARK}_key', 'value': f'{MARK} value'})
    client.post('/admin/settings', json={'key': 'help_center_label', 'value': f'{MARK} label'})
    client.post('/api/admin/groups', json={'name': f'{MARK}grp', 'description': f'{MARK} description'})
    client.get(f'/admin/audit-log?user={MARK}')
    anonymous_client().get(f'{base}/transcript?format=text&q={MARK}', headers=headers)
    anonymous_client().patch(base, json={'title': f'{MARK} api title'}, headers=headers)
    anonymous_client().put(f'{base}/notes', json={'notes': f'{MARK} api notes'}, headers=headers)
    visitor = anonymous_client()
    visitor.get(f'/share/{public_id}', headers={'User-Agent': 'visitor'})
    visitor.get(f'/share/audio/{public_id}', headers={'User-Agent': 'visitor'}).close()
    stranger = anonymous_client()
    stranger.post('/login', data={'email': f'{MARK}@example.com', 'password': f'{MARK}-Passw0rd!'})
    stranger.post('/login', data={'email': owner.email, 'password': f'{MARK}-Passw0rd!'})
    client_for(reader).get(f'/recording/{recording.id}/download/notes?x={MARK}')


def test_no_marker_reaches_a_row_or_a_fallback_line(created, tmp_path, monkeypatch, caplog):
    owner = make_user(created, prefix='own', is_admin=True, can_share_publicly=True)
    reader = make_user(created, prefix='rdr')
    set_setting('enable_internal_sharing', 'true')
    set_setting('enable_public_sharing', 'true')
    set_setting('user_search_scope', 'everyone')
    recording = make_recording(owner, title=f'{MARK} title', original_filename=f'{MARK}.wav',
                               notes=f'{MARK} notes', summary=f'{MARK} summary', participants=f'{MARK} person',
                               transcription=f'[{{"speaker": "SPEAKER_00", "sentence": "{MARK} words", '
                                             f'"start_time": 0, "end_time": 1}}]',
                               audio_path=_wav(tmp_path), mime_type='audio/wav')
    share = Share(recording_id=recording.id, user_id=owner.id, share_summary=True, share_notes=True)
    db.session.add(share)
    db.session.commit()
    public_id = share.public_id
    token, _ = api_token(owner, name=f'{MARK} token')
    secrets = [MARK, public_id, token, owner.email, reader.email, reader.username]

    # First with a database the writer cannot write: every queued event
    # becomes a fallback line.
    start = max_id()
    real_write = audit._write_batch
    monkeypatch.setattr(audit, 'RETRY_DELAYS', ())
    monkeypatch.setattr(audit, '_write_batch',
                        lambda main, rows: (_ for _ in ()).throw(RuntimeError(f'{MARK} database error')))
    with caplog.at_level(logging.ERROR, logger='speakr.audit'):
        _tour(owner, reader, recording, public_id, token)
        assert audit.flush(timeout=20)
    lines = [r.getMessage() for r in caplog.records if r.name == 'speakr.audit']
    fallback = [line for line in lines if line.startswith('AUDIT-FALLBACK ')]
    assert len(fallback) >= 8, lines
    events_in_lines = {json.loads(line[len('AUDIT-FALLBACK '):])['event'] for line in fallback}
    assert {'recording.viewed', 'recording.media.played', 'recording.downloaded', 'share.public.opened',
            'auth.login.failed', 'recording.content.read', 'audit.viewed'} <= events_in_lines
    for line in lines:
        for secret in secrets:
            assert secret not in line, f'{secret!r} in a log line of the audit log'

    # Then with the writer working again, in new time buckets.
    monkeypatch.setattr(audit, '_write_batch', real_write)
    with audit._lost_lock:
        audit._lost.clear()
    audit.reset_memory()
    later = audit.time.time() + 7200
    monkeypatch.setattr(audit, '_epoch', lambda: later)
    recording.title = f'{MARK} title'
    db.session.commit()
    _tour(owner, reader, recording, public_id, token)
    client_for(owner).delete(f'/recording/{recording.id}')

    rows = rows_after(start)
    names = {row.event for row in rows}
    assert {'recording.viewed', 'recording.media.played', 'recording.media.downloaded', 'recording.downloaded',
            'recording.edited', 'recording.content.read', 'recording.deleted', 'share.internal.created',
            'share.public.opened', 'share.public.media.played', 'token.created', 'admin.setting.changed',
            'group.created', 'auth.login.failed', 'audit.viewed'} <= names, sorted(names)
    dump = json.dumps([dict(row.to_dict(), dedup_key=row.dedup_key) for row in rows])
    for secret in secrets:
        assert secret not in dump, f'{secret!r} in an audit row'
    # The only names in the rows of this tour are event names and codes.
    values = [value for row in rows for value in (row.details or {}).values() if isinstance(value, str)]
    assert all(re.fullmatch(r'[a-z0-9_.\- ]{1,40}', value) for value in values), values
    from src.models import Group
    for group in Group.query.filter(Group.name.like(f'{MARK}%')).all():
        db.session.delete(group)
    SystemSetting.query.filter(SystemSetting.key.like(f'{MARK}%')).delete(synchronize_session=False)
    SystemSetting.set_setting('help_center_label', None, None, None)
    db.session.commit()
    assert AuditEvent.query.filter(AuditEvent.id > start).count() == len(rows)
