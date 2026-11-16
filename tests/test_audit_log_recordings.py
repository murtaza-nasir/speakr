"""Audit log: access to a recording, changes and deletion, on the web routes
and on API v1.

Each action writes its event once, a repeated read is written once per time
bucket, a refused read of an existing recording is written as denied, and no
title, file name, transcript, note or summary reaches a row.
"""

import json
import os
import struct
import uuid
import wave
from unittest import mock

import pytest

from tests.audit_log_support import (  # noqa: E402
    anonymous_client, api_token, app, audit, bearer, client_for, db, make_recording, make_user,
    max_id, names, remove_users, reset_audit_settings, rows_after, set_setting,
)
from src.models import AuditEvent, InternalShare, ProcessingJob, Recording, SystemSetting  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

SENTINEL = 'SENTINELCONTENT'


@pytest.fixture(autouse=True)
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        yield users
        audit.flush()
        db.session.rollback()
        SystemSetting.set_setting('enable_internal_sharing', None, None, 'boolean')
        remove_users(users)


def _wav(tmp_path):
    path = tmp_path / f'{uuid.uuid4().hex}.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(struct.pack('<h', 0) * 8000)
    return str(path)


def _dump(rows):
    return json.dumps([r.to_dict() for r in rows])


# --- Views --------------------------------------------------------------------

def test_web_view_is_written_once_for_the_eight_refetches(created):
    owner = make_user(created)
    recording = make_recording(owner, title=SENTINEL, original_filename=f'{SENTINEL}.mp3',
                               notes=SENTINEL, summary=SENTINEL)
    client = client_for(owner)
    start = max_id()
    for _ in range(8):
        assert client.get(f'/api/recordings/{recording.id}').status_code == 200
    rows = rows_after(start)
    assert names(rows) == ['recording.viewed']
    row = rows[0]
    assert (row.recording_id, row.recording_owner_id, row.actor_user_id) == (recording.id, owner.id, owner.id)
    assert row.actor_kind == 'session' and row.outcome == 'success' and row.details is None
    assert row.event_group == 'access'
    assert SENTINEL not in _dump(rows)


def test_a_refused_view_of_an_existing_recording_is_denied_and_a_missing_one_is_nothing(created):
    owner = make_user(created)
    other = make_user(created, prefix='oth')
    recording = make_recording(owner)
    client = client_for(other)
    start = max_id()
    for _ in range(3):
        assert client.get(f'/api/recordings/{recording.id}').status_code == 404
    assert client.get('/api/recordings/99999999').status_code == 404
    rows = rows_after(start)
    assert names(rows) == ['recording.viewed']
    assert rows[0].outcome == 'denied' and rows[0].actor_user_id == other.id
    assert rows[0].recording_owner_id == owner.id


def test_a_recipient_of_a_share_is_the_actor_and_the_owner_is_kept(created):
    owner = make_user(created)
    reader = make_user(created, prefix='rdr')
    recording = make_recording(owner)
    db.session.add(InternalShare(recording_id=recording.id, owner_id=owner.id, shared_with_user_id=reader.id,
                                 can_edit=False, can_reshare=False))
    db.session.commit()
    set_setting('enable_internal_sharing', 'true')
    start = max_id()
    assert client_for(reader).get(f'/api/recordings/{recording.id}').status_code == 200
    rows = rows_after(start, 'recording.viewed')
    assert [(r.actor_user_id, r.recording_owner_id, r.outcome) for r in rows] == [(reader.id, owner.id, 'success')]


def test_views_off_drops_the_view_and_keeps_the_download(created):
    owner = make_user(created)
    recording = make_recording(owner)
    set_setting('audit_log_record_views', 'false')
    client = client_for(owner)
    start = max_id()
    assert client.get(f'/api/recordings/{recording.id}').status_code == 200
    assert client.get(f'/recording/{recording.id}/download/summary?format=md').status_code == 200
    assert names(rows_after(start)) == ['recording.downloaded']


# --- Media ----------------------------------------------------------------------

def test_fifty_range_requests_are_one_play_and_a_download_is_its_own_event(created, tmp_path):
    owner = make_user(created)
    recording = make_recording(owner, audio_path=_wav(tmp_path), mime_type='audio/wav',
                               original_filename=f'{SENTINEL}.wav', title=SENTINEL)
    client = client_for(owner)
    start = max_id()
    for i in range(50):
        resp = client.get(f'/audio/{recording.id}', headers={'Range': f'bytes={i * 10}-{i * 10 + 9}'})
        assert resp.status_code == 206, resp.status_code
        resp.close()
    for _ in range(2):
        resp = client.get(f'/audio/{recording.id}?download=true')
        assert resp.status_code == 200
        resp.close()
    plain, token_id = api_token(owner, scopes='["read"]')
    resp = anonymous_client().get(f'/api/v1/recordings/{recording.id}/audio', headers=bearer(plain))
    assert resp.status_code == 200
    resp.close()
    rows = rows_after(start)
    by_event = sorted((r.event, r.actor_kind) for r in rows if r.event.startswith('recording.'))
    assert by_event == [('recording.media.downloaded', 'session'), ('recording.media.played', 'api_token'),
                        ('recording.media.played', 'session')]
    assert all(r.details is None for r in rows if r.event.startswith('recording.'))
    assert SENTINEL not in _dump(rows)


def test_a_refused_play_is_denied(created, tmp_path):
    owner = make_user(created)
    other = make_user(created, prefix='oth')
    recording = make_recording(owner, audio_path=_wav(tmp_path), mime_type='audio/wav')
    start = max_id()
    assert client_for(other).get(f'/audio/{recording.id}').status_code == 404
    rows = rows_after(start)
    assert [(r.event, r.outcome) for r in rows] == [('recording.media.played', 'denied')]


# --- Downloads --------------------------------------------------------------------

def test_downloads_record_part_and_format_from_fixed_codes(created):
    owner = make_user(created)
    recording = make_recording(owner, title=SENTINEL)
    client = client_for(owner)
    start = max_id()
    assert client.get(f'/recording/{recording.id}/download/transcript?format={SENTINEL}').status_code == 200
    assert client.get(f'/recording/{recording.id}/download/transcript?format=md').status_code == 200
    assert client.get(f'/recording/{recording.id}/download/summary').status_code == 200
    assert client.get(f'/recording/{recording.id}/download/summary?format=md').status_code == 200
    assert client.get(f'/recording/{recording.id}/download/notes').status_code == 200
    rows = rows_after(start, 'recording.downloaded')
    assert [r.details for r in rows] == [
        {'part': 'transcript', 'format': 'txt'}, {'part': 'transcript', 'format': 'md'},
        {'part': 'summary', 'format': 'docx'}, {'part': 'summary', 'format': 'md'},
        {'part': 'notes', 'format': 'docx'}]
    assert all(r.dedup_key is None and r.recording_id == recording.id for r in rows)
    assert SENTINEL not in _dump(rows_after(start))
    other = make_user(created, prefix='oth')
    start = max_id()
    assert client_for(other).get(f'/recording/{recording.id}/download/summary').status_code == 404
    rows = rows_after(start)
    assert [(r.event, r.outcome, r.details) for r in rows] == [
        ('recording.downloaded', 'denied', {'part': 'summary', 'format': 'docx'})]


# --- API v1 reads -------------------------------------------------------------------

def test_api_reads_record_the_token_and_the_part(created):
    owner = make_user(created)
    recording = make_recording(owner, title=SENTINEL, notes=SENTINEL)
    plain, token_id = api_token(owner, scopes='["read"]', name=SENTINEL)
    headers = bearer(plain)
    start = max_id()
    paths = ['', '/transcript', '/transcript?format=text', '/summary', '/notes', '/speakers', '/events']
    for path in paths + paths:
        resp = anonymous_client().get(f'/api/v1/recordings/{recording.id}{path}', headers=headers)
        assert resp.status_code == 200, (path, resp.status_code, resp.data[:200])
    rows = [r for r in rows_after(start) if r.event != 'token.first_used']
    assert sorted((r.event, (r.details or {}).get('part')) for r in rows) == [
        ('recording.content.read', 'events'), ('recording.content.read', 'notes'),
        ('recording.content.read', 'speakers'), ('recording.content.read', 'summary'),
        ('recording.content.read', 'transcript'), ('recording.viewed', None)]
    assert all(r.actor_kind == 'api_token' and r.actor_token_id == token_id for r in rows)
    assert all(r.actor_user_id == owner.id and r.recording_owner_id == owner.id for r in rows)
    assert SENTINEL not in _dump(rows) and plain not in _dump(rows)


def test_an_api_read_of_another_users_recording_is_denied(created):
    owner = make_user(created)
    other = make_user(created, prefix='oth')
    recording = make_recording(owner)
    plain, token_id = api_token(other)
    start = max_id()
    resp = anonymous_client().get(f'/api/v1/recordings/{recording.id}/transcript', headers=bearer(plain))
    assert resp.status_code == 404
    rows = [r for r in rows_after(start) if r.event != 'token.first_used']
    assert [(r.event, r.outcome, r.details, r.actor_token_id) for r in rows] == [
        ('recording.content.read', 'denied', {'part': 'transcript'}, token_id)]


# --- Changes ------------------------------------------------------------------------

def test_web_edits_record_the_field_names_only(created):
    owner = make_user(created)
    recording = make_recording(owner)
    client = client_for(owner)
    start = max_id()
    resp = client.post('/save', json={'id': recording.id, 'title': SENTINEL, 'notes': SENTINEL,
                                      'summary': SENTINEL, 'is_inbox': False})
    assert resp.status_code == 200, resp.data[:200]
    # A change of the personal state only is not an edit.
    assert client.post('/save', json={'id': recording.id, 'is_inbox': True}).status_code == 200
    resp = client.post(f'/recording/{recording.id}/update_transcription', json={'transcription': SENTINEL})
    assert resp.status_code == 200
    resp = client.post(f'/api/recordings/{recording.id}/toggle_deletion_exempt')
    assert resp.status_code == 200
    rows = rows_after(start)
    assert names(rows) == ['recording.edited'] * 3
    assert [r.details for r in rows] == [{'fields': ['notes', 'summary', 'title']}, {'fields': ['transcript']},
                                         {'fields': ['deletion_exempt']}]
    assert all(r.event_group == 'change' and r.recording_owner_id == owner.id for r in rows)
    assert SENTINEL not in _dump(rows)


def test_api_edits(created):
    owner = make_user(created)
    recording = make_recording(owner)
    plain, token_id = api_token(owner)
    headers = bearer(plain)
    start = max_id()
    base = f'/api/v1/recordings/{recording.id}'
    assert anonymous_client().patch(base, json={'title': SENTINEL, 'participants': SENTINEL},
                                    headers=headers).status_code == 200
    assert anonymous_client().put(base + '/notes', json={'notes': SENTINEL}, headers=headers).status_code == 200
    assert anonymous_client().put(base + '/summary', json={'summary': SENTINEL}, headers=headers).status_code == 200
    rows = rows_after(start, 'recording.edited')
    assert [r.details for r in rows] == [{'fields': ['participants', 'title']}, {'fields': ['notes']},
                                         {'fields': ['summary']}]
    assert all(r.actor_token_id == token_id for r in rows)
    assert SENTINEL not in _dump(rows_after(start))


def test_an_edit_that_is_rolled_back_leaves_no_row(created):
    owner = make_user(created)
    recording = make_recording(owner)
    client = client_for(owner)
    start = max_id()
    real_commit = db.session.commit
    calls = {'n': 0}

    def failing_commit():
        calls['n'] += 1
        raise RuntimeError('database went away')

    with mock.patch.object(db.session, 'commit', side_effect=failing_commit):
        resp = client.post(f'/recording/{recording.id}/update_transcription', json={'transcription': 'x'})
    assert resp.status_code == 500 and calls['n'] >= 1
    db.session.commit = real_commit
    assert rows_after(start, 'recording.edited') == []


# --- Deletion -------------------------------------------------------------------------

def test_web_and_api_deletion_are_recorded_once_and_the_rows_survive(created):
    owner = make_user(created)
    first = make_recording(owner, title=SENTINEL)
    second = make_recording(owner, title=SENTINEL)
    first_id, second_id = first.id, second.id
    client = client_for(owner)
    assert client.get(f'/api/recordings/{first_id}').status_code == 200
    start = max_id()
    assert client.delete(f'/recording/{first_id}').status_code == 200
    plain, token_id = api_token(owner)
    assert anonymous_client().delete(f'/api/v1/recordings/{second_id}', headers=bearer(plain)).status_code == 200
    rows = rows_after(start, 'recording.deleted')
    assert [(r.recording_id, r.recording_owner_id, r.details, r.actor_kind) for r in rows] == [
        (first_id, owner.id, {'reason': 'user'}, 'session'),
        (second_id, owner.id, {'reason': 'user'}, 'api_token')]
    assert db.session.get(Recording, first_id) is None
    # The view before the deletion is still there.
    assert AuditEvent.query.filter_by(event='recording.viewed', recording_id=first_id).count() >= 1
    assert SENTINEL not in _dump(rows)


def test_rows_survive_the_deletion_of_the_account(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    user = make_user(created)
    recording = make_recording(user)
    user_id, recording_id = user.id, recording.id
    assert client_for(user).get(f'/api/recordings/{recording_id}').status_code == 200
    start = max_id()
    viewed = rows_after(0, 'recording.viewed', recording_id=recording_id, actor_user_id=user_id)
    assert viewed
    resp = client_for(admin).delete(f'/admin/users/{user_id}')
    assert resp.status_code == 200, resp.data[:300]
    created.remove(user_id)
    rows = rows_after(start, 'recording.deleted')
    assert [(r.recording_id, r.recording_owner_id, r.details, r.actor_user_id) for r in rows] == [
        (recording_id, user_id, {'reason': 'account_deleted'}, admin.id)]
    assert db.session.get(AuditEvent, viewed[-1].id) is not None


def test_retention_deletion_is_a_system_event(created):
    from src.services.recording_deletion import delete_recording_completely
    owner = make_user(created)
    recording = make_recording(owner)
    recording_id = recording.id
    start = max_id()
    delete_recording_completely(recording, reason='retention')
    rows = rows_after(start, 'recording.deleted')
    assert [(r.recording_id, r.actor_kind, r.actor_user_id, r.details, r.ip) for r in rows] == [
        (recording_id, 'system', None, {'reason': 'retention'}, None)]


def test_audio_deletion(created, tmp_path):
    from src.services.retention import remove_recording_audio
    owner = make_user(created)
    recording = make_recording(owner, audio_path=_wav(tmp_path))
    kept = make_recording(owner, audio_path=_wav(tmp_path))
    client = client_for(owner)
    start = max_id()
    assert client.post(f'/recording/{recording.id}/delete_audio').status_code == 200
    remove_recording_audio(db.session.get(Recording, kept.id), reason='retention')
    rows = rows_after(start, 'recording.audio.deleted')
    assert [(r.recording_id, r.details, r.actor_kind) for r in rows] == [
        (recording.id, {'reason': 'user'}, 'session'), (kept.id, {'reason': 'retention'}, 'system')]


# --- Reprocessing and job cancel ------------------------------------------------------

def test_reprocessing_and_cancel(created, tmp_path):
    from src.services import reprocessing
    from src.services.job_cancel import cancel_job
    owner = make_user(created)
    recording = make_recording(owner, audio_path=_wav(tmp_path))
    start = max_id()
    with app.test_request_context('/x'):
        from flask_login import login_user
        login_user(owner)
        job_id = reprocessing.queue_summary_reprocess(recording, owner)
        assert cancel_job(job_id) == 'removed'
        reprocessing.queue_transcription_reprocess(db.session.get(Recording, recording.id), owner)
    rows = rows_after(start)
    wanted = [r for r in rows if r.event in ('recording.reprocessed', 'recording.job.canceled')]
    assert [(r.event, r.details) for r in wanted] == [
        ('recording.reprocessed', {'kind': 'summary'}),
        ('recording.job.canceled', {'kind': 'reprocess_summary'}),
        ('recording.reprocessed', {'kind': 'transcription'})]
    assert all(r.recording_id == recording.id and r.recording_owner_id == owner.id for r in wanted)
    assert all(r.actor_user_id == owner.id for r in wanted)
    ProcessingJob.query.filter_by(recording_id=recording.id).delete()
    db.session.commit()
