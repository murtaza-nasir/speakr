"""GET /api/v1/recordings/<id>/status: the stage and progress of the job.

The fields of earlier releases (status, queue_position, stage,
error_message, completed_at) are unchanged; processing_stage, stage_detail,
progress, progress_kind, job_started_at and cancel_requested are added.
"""

import os
import secrets
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, ProcessingJob, Recording, User
from src.utils.token_auth import hash_token


@pytest.fixture
def world():
    with app.app_context():
        name = f"v1st_{secrets.token_hex(4)}"
        user = User(username=name, email=f"{name}@local.test", password='x')
        db.session.add(user)
        db.session.commit()
        plain = f"test-token-{secrets.token_urlsafe(16)}"
        db.session.add(APIToken(user_id=user.id, token_hash=hash_token(plain), name='status'))
        db.session.commit()
        uid = user.id
        yield uid, plain
        db.session.rollback()
        ProcessingJob.query.filter_by(user_id=uid).delete(synchronize_session=False)
        Recording.query.filter_by(user_id=uid).delete(synchronize_session=False)
        APIToken.query.filter_by(user_id=uid).delete(synchronize_session=False)
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _recording(uid, status):
    recording = Recording(user_id=uid, title='Sync', status=status, original_filename='s.wav')
    db.session.add(recording)
    db.session.commit()
    return recording.id


def _job(uid, recording_id, status, job_type='transcribe', **fields):
    job = ProcessingJob(user_id=uid, recording_id=recording_id, job_type=job_type, status=status, **fields)
    db.session.add(job)
    db.session.commit()
    return job.id


def _status(plain, recording_id):
    with app.test_client() as client:
        response = client.get(f'/api/v1/recordings/{recording_id}/status',
                              headers={'Authorization': f'Bearer {plain}'})
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    return response.get_json()


def test_a_waiting_recording(world):
    uid, plain = world
    rid = _recording(uid, 'QUEUED')
    _job(uid, rid, 'queued')
    body = _status(plain, rid)
    assert body['status'] == 'QUEUED' and body['stage'] == 'transcription'
    assert body['queue_position'] >= 1
    assert body['processing_stage'] == 'queued'
    assert body['progress'] is None and body['progress_kind'] is None
    assert body['job_started_at'] is None
    assert body['cancel_requested'] is False


def test_a_chunked_transcription_reports_chunks(world):
    uid, plain = world
    rid = _recording(uid, 'PROCESSING')
    _job(uid, rid, 'processing', started_at=datetime(2026, 11, 15, 9, 30, 5),
         progress_done=3, progress_total=8, progress_kind='chunks')
    body = _status(plain, rid)
    assert body['processing_stage'] == 'transcribing'
    assert body['progress'] == {'done': 3, 'total': 8}
    assert body['progress_kind'] == 'chunks'
    assert body['job_started_at'] == '2026-11-15T09:30:05Z'
    assert body['queue_position'] is None


def test_a_measured_fraction_and_the_service_stage(world):
    uid, plain = world
    rid = _recording(uid, 'PROCESSING')
    _job(uid, rid, 'processing', started_at=datetime.utcnow(), progress_done=42, progress_total=100,
         progress_kind='fraction', stage_detail='aligning', cancel_requested_at=datetime.utcnow())
    body = _status(plain, rid)
    assert body['progress'] == {'fraction': 0.42}
    assert body['progress_kind'] == 'fraction'
    assert body['stage_detail'] == 'aligning'
    assert body['cancel_requested'] is True


def test_a_summary_has_a_stage_and_no_number(world):
    uid, plain = world
    rid = _recording(uid, 'SUMMARIZING')
    _job(uid, rid, 'processing', job_type='summarize', started_at=datetime.utcnow())
    body = _status(plain, rid)
    assert body['stage'] == 'summary'
    assert body['processing_stage'] == 'summarizing'
    assert body['progress'] is None and body['progress_kind'] is None


def test_the_summary_step_inside_a_transcription_job(world):
    uid, plain = world
    rid = _recording(uid, 'SUMMARIZING')
    _job(uid, rid, 'processing', started_at=datetime.utcnow(), stage='summarizing')
    body = _status(plain, rid)
    assert body['stage'] == 'transcription'          # the queue of the job, as before
    assert body['processing_stage'] == 'summarizing'  # the step it has reached


def test_a_recording_without_a_job_and_a_translation_beside_it(world):
    uid, plain = world
    rid = _recording(uid, 'COMPLETED')
    # A translation runs beside the recording and is not its stage.
    _job(uid, rid, 'processing', job_type='translate_transcription', started_at=datetime.utcnow(),
         progress_done=1, progress_total=3)
    body = _status(plain, rid)
    assert body['status'] == 'COMPLETED'
    for key in ('stage', 'processing_stage', 'stage_detail', 'progress', 'progress_kind', 'job_started_at',
                'queue_position'):
        assert body[key] is None, key
    assert body['cancel_requested'] is False
    assert set(body) >= {'id', 'status', 'queue_position', 'stage', 'error_message', 'completed_at'}
