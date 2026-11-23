"""A recording deleted while its job's failure is stored ends the job quietly (#681).

The rows go between the worker's re-read and its commit, and the commit
raised StaleDataError out of the worker loop.
"""

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services import job_control
from src.services.job_queue import job_queue, SUMMARY_JOBS, MAX_RETRIES


@pytest.fixture(autouse=True)
def _bind_queue():
    job_queue.init_app(app)
    yield


@pytest.fixture
def made():
    with app.app_context():
        ids = {'users': [], 'recordings': []}
        yield ids
        db.session.rollback()
        for rid in ids['recordings']:
            ProcessingJob.query.filter_by(recording_id=rid).delete()
            Recording.query.filter_by(id=rid).delete()
        for uid in ids['users']:
            User.query.filter_by(id=uid).delete()
        db.session.commit()


_n = {'n': 0}


def _user(made):
    _n['n'] += 1
    stamp = f"{os.getpid()}_{_n['n']}_{datetime.utcnow().timestamp()}"
    user = User(username=f"jr_{_n['n']}_{os.getpid()}"[:20], email=f"jr_{stamp}@example.com")
    db.session.add(user)
    db.session.commit()
    made['users'].append(user.id)
    return user.id


def _recording(made, uid):
    rec = Recording(user_id=uid, title="Reuse", audio_path="/tmp/jr.mp3", status="PENDING",
                    transcription="Hello there, this is the transcript.")
    db.session.add(rec)
    db.session.commit()
    made['recordings'].append(rec.id)
    return rec.id


def _delete_recording_rows(rid):
    """What delete_recording_completely does to the rows, from another session."""
    with db.engine.begin() as conn:
        conn.execute(db.text("DELETE FROM processing_job WHERE recording_id = :r"), {"r": rid})
        conn.execute(db.text("DELETE FROM recording WHERE id = :r"), {"r": rid})


def _insert_job_with_id(job_id, uid, rid, job_type='summarize'):
    """A new job that received the id of the deleted one."""
    with db.engine.begin() as conn:
        conn.execute(ProcessingJob.__table__.insert().values(
            id=job_id, user_id=uid, recording_id=rid, job_type=job_type, status='queued',
            retry_count=0, is_new_upload=False, created_at=datetime.utcnow() + timedelta(seconds=1)))


def _claim(job_id):
    """Claim this job as a worker does. The turn order of the shared test
    database could hand out another test's waiting job."""
    from datetime import datetime as _dt
    from sqlalchemy import update
    db.session.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == 'queued')
                       .values(status='processing', started_at=_dt.utcnow()))
    job = db.session.get(ProcessingJob, job_id)
    db.session.refresh(job)
    rec = db.session.get(Recording, job.recording_id)
    if rec is not None and rec.status == 'QUEUED':
        rec.status = 'PROCESSING'
    db.session.commit()
    db.session.refresh(job)
    return job


def _claimed_summary_job(made):
    uid = _user(made)
    rid = _recording(made, uid)
    jid = job_queue.enqueue(uid, rid, 'summarize')
    job = _claim(jid)
    assert job.id == jid
    return uid, rid, jid, job


def _fresh(job_id):
    db.session.expire_all()
    return db.session.get(ProcessingJob, job_id)



def test_recording_deleted_while_a_final_failure_is_stored(made):
    """The rows went between the re-read and the commit: StaleDataError, caught."""
    uid, rid, jid, job = _claimed_summary_job(made)
    row = db.session.get(ProcessingJob, jid)
    row.retry_count = MAX_RETRIES - 1
    db.session.commit()

    def release(job_row):
        _delete_recording_rows(rid)

    with patch.object(job_queue, '_run_summarization', side_effect=RuntimeError("boom")), \
            patch.object(job_queue, '_release_remote_task', side_effect=release), \
            patch.object(job_queue, '_emit_failure_webhook') as hook, \
            patch.object(job_queue, '_emit_failure_email') as mail:
        job_queue._process_job(job)   # does not raise

    assert _fresh(jid) is None
    assert db.session.get(Recording, rid) is None
    hook.assert_not_called()
    mail.assert_not_called()


def test_recording_deleted_while_a_retry_is_stored(made):
    uid, rid, jid, job = _claimed_summary_job(made)
    from src.utils import error_text

    real = error_text.user_error_text

    def scrub_then_delete(*args, **kwargs):
        text = real(*args, **kwargs)
        _delete_recording_rows(rid)
        return text

    with patch.object(job_queue, '_run_summarization', side_effect=RuntimeError("boom")), \
            patch('src.services.job_queue.user_error_text', side_effect=scrub_then_delete):
        job_queue._process_job(job)

    assert _fresh(jid) is None


def test_final_failure_notices_are_sent_after_the_failure_is_stored(made):
    uid, rid, jid, job = _claimed_summary_job(made)
    row = db.session.get(ProcessingJob, jid)
    row.retry_count = MAX_RETRIES - 1
    db.session.commit()
    seen = []

    def hook(job_type, recording_id, error):
        with db.engine.connect() as conn:
            seen.append(conn.execute(db.text("SELECT status FROM processing_job WHERE id = :j"),
                                     {"j": jid}).scalar())

    with patch.object(job_queue, '_run_summarization', side_effect=RuntimeError("boom")), \
            patch.object(job_queue, '_emit_failure_webhook', side_effect=hook):
        job_queue._process_job(job)

    assert seen == ['failed']
