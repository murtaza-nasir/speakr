"""An aborted upload leaves nothing behind.

While the body is still arriving the server holds only the multipart
parser's temporary file, which goes with the request. After the body has
arrived the server finishes the ingestion without noticing the abort, so the
browser names the upload (client_upload_id) and calls POST /upload/cancel:
the ingestion discards the staging file or the recording when it reads the
mark, and the cancel deletes a recording the ingestion has already stored.

SHARED-DB: every assertion is scoped to the rows the test created.
"""

import io
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ClientUpload, ProcessingJob, Recording, User
from src.services import upload_cancel
from src.services.storage import get_storage_service

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

AUDIO = b'RIFF' + b'\x00' * 2048


@pytest.fixture
def user():
    with app.app_context():
        suffix = uuid.uuid4().hex[:10]
        u = User(username=f"uc_{suffix}", email=f"uc_{suffix}@local.test", password='x')
        db.session.add(u)
        db.session.commit()
        uid = u.id
        yield uid
        db.session.rollback()
        storage = get_storage_service()
        for rec in Recording.query.filter_by(user_id=uid).all():
            try:
                storage.delete(rec.audio_path, missing_ok=True)
            except Exception:
                pass
        ProcessingJob.query.filter_by(user_id=uid).delete(synchronize_session=False)
        Recording.query.filter_by(user_id=uid).delete(synchronize_session=False)
        ClientUpload.query.filter_by(user_id=uid).delete(synchronize_session=False)
        db.session.commit()


def _client(uid):
    # The tests hold one app context, and flask-login keeps the user of the
    # last request on it: a request as someone else starts without it.
    from flask import g, has_app_context
    if has_app_context():
        g.pop('_login_user', None)
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return client


def _upload_id():
    return f"client-{uuid.uuid4().hex}"


def _files_on_disk():
    """Every file under the upload folder: staging copies and stored media."""
    root = app.config['UPLOAD_FOLDER']
    found = set()
    for base, _dirs, names in os.walk(root):
        for name in names:
            found.add(os.path.join(base, name))
    return found


def _upload(uid, client_upload_id=None, during=None, filename='meeting.mp3'):
    """POST /upload with ffprobe and ffmpeg stubbed out.

    during: called while the server works on the stored body (where the
    probe runs), as a cancel that arrives after the bytes were sent.
    """
    def codec_info(path, timeout=None):
        if during:
            during()
        return {'has_video': False, 'audio_codec': 'mp3', 'duration': 60.0}

    def convert(filepath, **kwargs):
        result = MagicMock()
        result.output_path = filepath
        result.was_converted = result.was_compressed = False
        return result

    data = {'file': (io.BytesIO(AUDIO), filename)}
    if client_upload_id:
        data['client_upload_id'] = client_upload_id
    with patch('src.services.recording_ingestion.get_codec_info', side_effect=codec_info), \
         patch('src.services.recording_ingestion.convert_if_needed', side_effect=convert), \
         patch('src.services.recording_ingestion.get_duration', return_value=60.0), \
         patch('src.services.recording_ingestion.get_creation_date', return_value=None), \
         patch('src.services.webhook_dispatch.emit_webhook_event') as webhook:
        response = _client(uid).post('/upload', data=data, content_type='multipart/form-data')
    return response, webhook


def _cancel(uid, client_upload_id):
    return _client(uid).post('/upload/cancel', json={'client_upload_id': client_upload_id})


def _recordings(uid):
    db.session.expire_all()
    return Recording.query.filter_by(user_id=uid).all()


def _jobs(uid):
    return ProcessingJob.query.filter_by(user_id=uid).count()


def _events(webhook):
    return [call.kwargs.get('event_type') for call in webhook.call_args_list]


# --- the upload itself ------------------------------------------------------------

def test_a_named_upload_is_stored_and_linked(user):
    upload_id = _upload_id()
    response, webhook = _upload(user, upload_id)
    assert response.status_code == 202, response.get_data(as_text=True)[:300]
    recording_id = response.get_json()['id']
    assert [r.id for r in _recordings(user)] == [recording_id]
    assert _jobs(user) == 1
    row = ClientUpload.query.filter_by(user_id=user, client_upload_id=upload_id).one()
    assert row.recording_id == recording_id and row.canceled_at is None
    assert 'recording.created' in _events(webhook)


def test_an_upload_without_an_id_writes_no_row(user):
    response, _ = _upload(user)
    assert response.status_code == 202
    assert ClientUpload.query.filter_by(user_id=user).count() == 0
    assert len(_recordings(user)) == 1


# --- aborted while the body arrives ------------------------------------------------

def test_a_body_that_stops_arriving_leaves_nothing(user):
    before = _files_on_disk()
    boundary = 'speakrtestboundary'
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="meeting.mp3"\r\n'
            'Content-Type: audio/mpeg\r\n\r\n').encode()
    partial = head + AUDIO[:512]                     # the connection ends here
    response = _client(user).post(
        '/upload', input_stream=io.BytesIO(partial), content_length=len(partial) + 100000,
        content_type=f'multipart/form-data; boundary={boundary}')
    assert response.status_code == 400
    assert _recordings(user) == []
    assert _jobs(user) == 0
    assert _files_on_disk() == before


# --- aborted after the body arrived ------------------------------------------------

def test_a_cancel_that_arrives_first_refuses_the_upload_before_any_work(user):
    before = _files_on_disk()
    upload_id = _upload_id()
    answer = _cancel(user, upload_id)
    assert answer.status_code == 200
    assert answer.get_json() == {'success': True, 'recording_deleted': False, 'recording_kept': False}

    probed = []
    response, webhook = _upload(user, upload_id, during=lambda: probed.append(1))
    assert response.status_code == 409
    assert response.get_json()['code'] == 'upload_canceled'
    assert probed == []                              # neither probed nor converted
    assert _recordings(user) == []
    assert _jobs(user) == 0
    assert _files_on_disk() == before                # the staging copy is gone
    assert _events(webhook) == []


def test_a_cancel_while_the_server_works_on_the_file_discards_the_recording(user):
    before = _files_on_disk()
    upload_id = _upload_id()
    canceled = []

    def cancel_now():
        if not canceled:
            canceled.append(_cancel(user, upload_id).get_json())

    response, webhook = _upload(user, upload_id, during=cancel_now)
    assert canceled == [{'success': True, 'recording_deleted': False, 'recording_kept': False}]
    assert response.status_code == 409
    assert response.get_json()['code'] == 'upload_canceled'
    assert _recordings(user) == []                   # no half-created row
    assert _jobs(user) == 0                          # nothing was queued
    assert _files_on_disk() == before                # neither staging copy nor stored media
    assert _events(webhook) == []                    # never announced, so no webhook at all


def test_a_cancel_after_the_answer_was_lost_deletes_the_recording(user):
    before = _files_on_disk()
    upload_id = _upload_id()
    response, _ = _upload(user, upload_id)
    assert response.status_code == 202
    assert len(_recordings(user)) == 1 and _jobs(user) == 1
    assert _files_on_disk() != before

    with patch('src.services.webhook_dispatch.emit_webhook_event') as webhook:
        answer = _cancel(user, upload_id)
    assert answer.status_code == 200
    assert answer.get_json()['recording_deleted'] is True
    assert _recordings(user) == []
    assert _jobs(user) == 0
    assert _files_on_disk() == before
    # It was announced as created, so its deletion is announced as well.
    assert _events(webhook) == ['recording.deleted']

    # A second cancel finds nothing more to do.
    again = _cancel(user, upload_id)
    assert again.status_code == 200 and again.get_json()['recording_deleted'] is False


# --- what a cancel never deletes ----------------------------------------------------

def test_a_completed_recording_is_kept(user):
    upload_id = _upload_id()
    recording_id = _upload(user, upload_id)[0].get_json()['id']
    recording = db.session.get(Recording, recording_id)
    recording.status = 'COMPLETED'
    db.session.commit()
    answer = _cancel(user, upload_id).get_json()
    assert answer == {'success': True, 'recording_deleted': False, 'recording_kept': True}
    assert [r.id for r in _recordings(user)] == [recording_id]


def test_a_recording_past_the_window_is_kept(user):
    upload_id = _upload_id()
    recording_id = _upload(user, upload_id)[0].get_json()['id']
    row = ClientUpload.query.filter_by(user_id=user, client_upload_id=upload_id).one()
    row.created_at = datetime.utcnow() - upload_cancel.CANCEL_WINDOW - timedelta(minutes=1)
    db.session.commit()
    assert _cancel(user, upload_id).get_json()['recording_kept'] is True
    assert [r.id for r in _recordings(user)] == [recording_id]


def test_a_recording_that_took_over_the_id_is_not_this_uploads(user):
    upload_id = _upload_id()
    recording_id = _upload(user, upload_id)[0].get_json()['id']
    row = ClientUpload.query.filter_by(user_id=user, client_upload_id=upload_id).one()
    row.recording_created_at = datetime(2020, 1, 1)  # the row describes an earlier recording
    db.session.commit()
    answer = _cancel(user, upload_id).get_json()
    assert answer['recording_deleted'] is False and answer['recording_kept'] is False
    assert [r.id for r in _recordings(user)] == [recording_id]


def test_the_id_of_another_user_cancels_nothing(user):
    upload_id = _upload_id()
    recording_id = _upload(user, upload_id)[0].get_json()['id']
    suffix = uuid.uuid4().hex[:10]
    other = User(username=f"uc_{suffix}", email=f"uc_{suffix}@local.test", password='x')
    db.session.add(other)
    db.session.commit()
    other_id = other.id
    answer = _cancel(other_id, upload_id)
    assert answer.status_code == 200 and answer.get_json()['recording_deleted'] is False
    assert [r.id for r in _recordings(user)] == [recording_id]
    ClientUpload.query.filter_by(user_id=other_id).delete()
    db.session.delete(db.session.get(User, other_id))
    db.session.commit()


@pytest.mark.parametrize('bad', [None, '', 'short', 'has spaces in it', 'x' * 81, '../../etc/passwd', 12345678])
def test_an_invalid_id_is_refused(user, bad):
    response = _client(user).post('/upload/cancel', json={'client_upload_id': bad})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_client_upload_id'


def test_cancel_needs_a_session():
    response = app.test_client().post('/upload/cancel', json={'client_upload_id': _upload_id()})
    assert response.status_code in (302, 401)


def test_an_invalid_id_in_the_upload_form_is_ignored(user):
    response, _ = _upload(user, 'bad id!')
    assert response.status_code == 202
    assert ClientUpload.query.filter_by(user_id=user).count() == 0


def test_old_rows_are_removed_with_the_next_upload(user):
    stale_upload_id = _upload_id()
    db.session.add(ClientUpload(user_id=user, client_upload_id=stale_upload_id,
                                created_at=datetime.utcnow() - upload_cancel.ROW_TTL - timedelta(hours=1)))
    db.session.commit()
    db.session.expunge_all()
    assert _upload(user, _upload_id())[0].status_code == 202
    assert ClientUpload.query.filter_by(user_id=user, client_upload_id=stale_upload_id).count() == 0
    assert ClientUpload.query.filter_by(user_id=user).count() == 1
