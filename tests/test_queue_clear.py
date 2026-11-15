"""Clearing rows from the processing queue: one failed or finished row, all
finished rows, all failed rows.

SHARED-DB: every assertion is scoped to the rows the test created.
"""

import json
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


@pytest.fixture(autouse=True)
def ctx():
    with app.app_context():
        yield
        db.session.rollback()


def _user():
    suffix = uuid.uuid4().hex[:10]
    user = User(username=f"qc_{suffix}", email=f"qc_{suffix}@local.test", password='x')
    db.session.add(user)
    db.session.commit()
    return user.id


def _recording(user_id, status='COMPLETED', **fields):
    recording = Recording(user_id=user_id, title='Weekly sync', original_filename='w.wav',
                          audio_path='local://w.wav', status=status, **fields)
    db.session.add(recording)
    db.session.commit()
    return recording.id


def _job(user_id, recording_id, status, job_type='transcribe', **fields):
    job = ProcessingJob(user_id=user_id, recording_id=recording_id, job_type=job_type, status=status,
                        completed_at=datetime.utcnow() if status in ('completed', 'failed') else None,
                        **fields)
    db.session.add(job)
    db.session.commit()
    return job.id


def _client(user_id):
    # The tests hold one app context, and flask-login keeps the user of the
    # last request on it: a request as someone else starts without it.
    from flask import g, has_app_context
    if has_app_context():
        g.pop('_login_user', None)
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    return client


def _status(job_id):
    with db.engine.connect() as conn:
        row = conn.execute(text('SELECT status FROM processing_job WHERE id = :id'), {'id': job_id}).first()
    return row[0] if row else None


def _recording_exists(recording_id):
    with db.engine.connect() as conn:
        return conn.execute(text('SELECT 1 FROM recording WHERE id = :id'), {'id': recording_id}).first() is not None


# --- clear failed -----------------------------------------------------------------

def test_clear_failed_removes_the_failed_rows_of_the_user_only():
    me, other = _user(), _user()
    mine = [_job(me, _recording(me, 'FAILED'), 'failed', is_new_upload=True),
            _job(me, _recording(me, 'FAILED'), 'failed', job_type='reprocess_summary')]
    kept = [_job(me, _recording(me), 'completed'),
            _job(me, _recording(me, 'QUEUED'), 'queued'),
            _job(me, _recording(me, 'PROCESSING'), 'processing')]
    theirs = _job(other, _recording(other, 'FAILED'), 'failed')
    recordings = [db.session.get(ProcessingJob, j).recording_id for j in mine]

    response = _client(me).post('/api/recordings/jobs/clear-failed')
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'deleted': 2}
    assert [_status(j) for j in mine] == [None, None]
    assert [_status(j) for j in kept] == ['completed', 'queued', 'processing']
    assert _status(theirs) == 'failed'
    # The recordings stay, a failed new upload among them.
    assert all(_recording_exists(r) for r in recordings)
    db.session.expire_all()
    assert db.session.get(Recording, recordings[0]).status == 'FAILED'

    # Nothing left: the call still succeeds.
    assert _client(me).post('/api/recordings/jobs/clear-failed').get_json()['deleted'] == 0


def test_clear_failed_removes_a_leftover_asr_job(monkeypatch):
    import src.services.transcription.connectors.asr_endpoint as asr
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append(job_id) or True)
    me = _user()
    _job(me, _recording(me, 'FAILED'), 'failed', provider_task=json.dumps(
        {'provider': 'asr_endpoint', 'audio_sha256': 'x',
         'task': {'job_id': 'job0077', 'base_url': 'http://asr.test:9000'}}))
    assert _client(me).post('/api/recordings/jobs/clear-failed').status_code == 200
    assert cancelled == ['job0077']


def test_clear_failed_needs_a_session():
    assert app.test_client().post('/api/recordings/jobs/clear-failed').status_code in (302, 401)


def test_clear_completed_still_removes_completed_rows_only():
    me = _user()
    done = _job(me, _recording(me), 'completed')
    failed = _job(me, _recording(me, 'FAILED'), 'failed')
    response = _client(me).post('/api/recordings/jobs/clear-completed')
    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'deleted': 1}
    assert _status(done) is None
    assert _status(failed) == 'failed'


# --- remove one row, by its state --------------------------------------------------

def test_removing_a_completed_row_leaves_the_recording():
    me = _user()
    recording_id = _recording(me)
    job_id = _job(me, recording_id, 'completed', is_new_upload=True)
    response = _client(me).delete(f'/api/recordings/jobs/{job_id}')
    assert response.status_code == 200
    assert 'canceled' not in response.get_json()
    assert _status(job_id) is None
    assert _recording_exists(recording_id)


def test_removing_a_failed_reprocess_row_leaves_the_recording():
    me = _user()
    recording_id = _recording(me, 'FAILED')
    job_id = _job(me, recording_id, 'failed', job_type='reprocess_transcription')
    assert _client(me).delete(f'/api/recordings/jobs/{job_id}').status_code == 200
    assert _status(job_id) is None
    assert _recording_exists(recording_id)


def test_removing_a_failed_new_upload_row_deletes_its_recording_as_before():
    me = _user()
    recording_id = _recording(me, 'FAILED')
    job_id = _job(me, recording_id, 'failed', is_new_upload=True)
    sibling = _job(me, recording_id, 'completed', job_type='convert')
    storage = MagicMock()
    with patch('src.api.recordings.get_storage_service', return_value=storage):
        assert _client(me).delete(f'/api/recordings/jobs/{job_id}').status_code == 200
    storage.delete.assert_called_once_with('local://w.wav', missing_ok=True)
    assert _status(job_id) is None and _status(sibling) is None
    assert not _recording_exists(recording_id)


def test_removing_a_row_twice_answers_404_the_second_time():
    me = _user()
    job_id = _job(me, _recording(me), 'completed')
    client = _client(me)
    assert client.delete(f'/api/recordings/jobs/{job_id}').status_code == 200
    second = client.delete(f'/api/recordings/jobs/{job_id}')
    assert second.status_code == 404
    assert second.get_json()['code'] == 'job_not_found'


def test_a_finished_row_of_another_user_is_refused():
    me, other = _user(), _user()
    job_id = _job(me, _recording(me), 'completed')
    response = _client(other).delete(f'/api/recordings/jobs/{job_id}')
    assert response.status_code == 403
    assert _status(job_id) == 'completed'


def test_a_failed_row_can_still_be_retried_and_starts_clean():
    me = _user()
    job_id = _job(me, _recording(me, 'FAILED'), 'failed', progress_done=2, progress_total=5,
                  progress_kind='chunks', stage='titling', stage_detail='aligning',
                  cancel_requested_at=datetime.utcnow())
    assert _client(me).post(f'/api/recordings/jobs/{job_id}/retry').status_code == 200
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job_id)
    assert job.status == 'queued'
    assert (job.progress_done, job.progress_total, job.progress_kind, job.stage, job.stage_detail,
            job.cancel_requested_at) == (None, None, None, None, None, None)
