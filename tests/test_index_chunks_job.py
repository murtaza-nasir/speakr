"""Inquire chunks are built by their own workers after a recording is completed (#541)."""

import os
import uuid
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import INDEX_JOBS, MAX_RETRIES, SUMMARY_JOBS, TRANSCRIPTION_JOBS, job_queue
import src.tasks.processing as proc


@pytest.fixture
def recording():
    job_queue.init_app(app)
    with app.app_context():
        stale = ProcessingJob.query.filter(ProcessingJob.status == 'queued').all()
        for job in stale:
            job.status = 'completed'
        db.session.commit()
        s = uuid.uuid4().hex[:8]
        user = User(username=f"index_{s}", email=f"index_{s}@local.test")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title='Board meeting', audio_path='local://index.mp3',
                        transcription='word ' * 50, status='PROCESSING')
        db.session.add(rec)
        db.session.commit()
        yield rec
        for job in ProcessingJob.query.filter_by(recording_id=rec.id).all():
            db.session.delete(job)
        db.session.commit()
        db.session.delete(db.session.get(Recording, rec.id))
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


def _index_jobs(recording_id):
    return ProcessingJob.query.filter_by(recording_id=recording_id, job_type='index_chunks').all()


def test_a_completed_recording_queues_its_chunks_without_building_them(recording):
    with patch.object(proc, 'ENABLE_INQUIRE_MODE', True), \
         patch.object(proc, 'apply_team_tag_auto_shares'), \
         patch('src.services.embeddings.process_recording_chunks') as build:
        proc.finish_processing(recording.id)

    build.assert_not_called()
    assert [job.status for job in _index_jobs(recording.id)] == ['queued']
    assert db.session.get(Recording, recording.id).status == 'COMPLETED'


def test_only_the_index_workers_claim_a_chunk_job(recording):
    job_queue.enqueue(recording.user_id, recording.id, 'index_chunks')

    assert job_queue._claim_next_job(TRANSCRIPTION_JOBS, 'transcription') is None
    assert job_queue._claim_next_job(SUMMARY_JOBS, 'summary') is None
    assert job_queue._claim_next_job(INDEX_JOBS, 'index').recording_id == recording.id


def _run_index_job(recording, built):
    recording.status = 'COMPLETED'
    db.session.commit()
    job_queue.enqueue(recording.user_id, recording.id, 'index_chunks')
    job = job_queue._claim_next_job(INDEX_JOBS, 'index')
    # The last try, so a failure is final.
    db.session.get(ProcessingJob, job.id).retry_count = MAX_RETRIES - 1
    db.session.commit()
    with patch('src.services.embeddings.process_recording_chunks', return_value=built) as build, \
         patch.object(job_queue, '_emit_completion_webhook') as done_hook, \
         patch.object(job_queue, '_emit_failure_webhook') as fail_hook:
        job_queue._process_job(job)
    db.session.expire_all()
    return build, done_hook, fail_hook, db.session.get(ProcessingJob, job.id)


def test_a_chunk_job_builds_the_chunks_and_leaves_the_recording_alone(recording):
    build, done_hook, _, job = _run_index_job(recording, built=True)

    build.assert_called_once_with(recording.id)
    assert job.status == 'completed'
    assert db.session.get(Recording, recording.id).status == 'COMPLETED'
    done_hook.assert_not_called()


def test_a_failed_chunk_job_keeps_the_recording_completed(recording):
    transcript = recording.transcription
    _, _, fail_hook, job = _run_index_job(recording, built=False)

    assert job.status == 'failed'
    rec = db.session.get(Recording, recording.id)
    assert rec.status == 'COMPLETED'
    assert rec.transcription == transcript
    fail_hook.assert_not_called()


def test_the_job_list_leaves_out_chunk_jobs(recording):
    job_queue.enqueue(recording.user_id, recording.id, 'index_chunks')
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(recording.user_id)

    res = client.get('/api/recordings/job-queue-status')

    assert res.status_code == 200
    assert [job for job in res.get_json()['jobs'] if job['recording_id'] == recording.id] == []
