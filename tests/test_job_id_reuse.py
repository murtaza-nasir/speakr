"""A worker never writes onto a job that received its id after a delete (#675),
and a recording deleted while a failure is stored ends the job quietly (#681).

Deleting a recording deletes its jobs, and SQLite gives the next new job the
id of a deleted row that held the highest id. The worker of the old job
re-reads its row by id: without a check of the row's identity it marked the
new job completed without running it, raised its retry count or deleted it.
The new job is inserted with the old id explicitly, so the tests behave the
same on PostgreSQL, whose ids are not reused.
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


def test_completion_leaves_a_new_job_with_the_reused_id_alone(made):
    uid, rid, jid, job = _claimed_summary_job(made)
    other_rid = _recording(made, uid)

    def runner(*args, **kwargs):
        _delete_recording_rows(rid)
        _insert_job_with_id(jid, uid, other_rid)

    with patch.object(job_queue, '_run_summarization', side_effect=runner), \
            patch.object(job_queue, '_emit_completion_webhook') as hook, \
            patch.object(job_queue, '_emit_completion_email') as mail:
        job_queue._process_job(job)

    new_job = _fresh(jid)
    assert new_job is not None
    assert new_job.recording_id == other_rid
    assert new_job.status == 'queued'
    assert new_job.completed_at is None
    hook.assert_not_called()
    mail.assert_not_called()


def test_a_failure_does_not_delete_a_new_job_with_the_reused_id(made):
    uid, rid, jid, job = _claimed_summary_job(made)
    other_rid = _recording(made, uid)

    def runner(*args, **kwargs):
        _delete_recording_rows(rid)
        _insert_job_with_id(jid, uid, other_rid)
        raise RuntimeError("provider went away")

    with patch.object(job_queue, '_run_summarization', side_effect=runner), \
            patch.object(job_queue, '_emit_failure_webhook') as hook:
        job_queue._process_job(job)

    new_job = _fresh(jid)
    assert new_job is not None and new_job.recording_id == other_rid
    assert new_job.status == 'queued'
    hook.assert_not_called()


def test_a_failure_does_not_retry_a_new_job_of_the_same_recording(made):
    """Same recording, same id, later creation time: another job."""
    uid, rid, jid, job = _claimed_summary_job(made)

    def runner(*args, **kwargs):
        with db.engine.begin() as conn:
            conn.execute(db.text("DELETE FROM processing_job WHERE id = :j"), {"j": jid})
        _insert_job_with_id(jid, uid, rid)
        raise RuntimeError("provider went away")

    with patch.object(job_queue, '_run_summarization', side_effect=runner), \
            patch.object(job_queue, '_emit_failure_webhook'):
        job_queue._process_job(job)

    new_job = _fresh(jid)
    assert new_job.status == 'queued'
    assert new_job.retry_count == 0
    assert new_job.error_message is None


def test_a_cancel_does_not_delete_a_new_job_with_the_reused_id(made):
    uid, rid, jid, job = _claimed_summary_job(made)

    def runner(*args, **kwargs):
        with db.engine.begin() as conn:
            conn.execute(db.text("DELETE FROM processing_job WHERE id = :j"), {"j": jid})
        _insert_job_with_id(jid, uid, rid)
        raise job_control.JobCanceled(jid)

    with patch.object(job_queue, '_run_summarization', side_effect=runner):
        job_queue._process_job(job)

    assert _fresh(jid) is not None


def test_the_control_of_an_old_job_does_not_touch_the_new_one(made):
    uid, rid, jid, job = _claimed_summary_job(made)
    created_at = job.created_at
    with db.engine.begin() as conn:
        conn.execute(db.text("DELETE FROM processing_job WHERE id = :j"), {"j": jid})
    _insert_job_with_id(jid, uid, rid)
    with db.engine.begin() as conn:
        conn.execute(db.text("UPDATE processing_job SET status = 'processing', cancel_requested_at = :t "
                             "WHERE id = :j"), {"j": jid, "t": datetime.utcnow()})

    control = job_control.JobControl(jid, created_at=created_at)
    assert control.cancel_requested() is False
    control.stage('titling')
    assert _fresh(jid).stage is None
    # Without the creation time the id alone decides, as before.
    assert job_control.JobControl(jid).cancel_requested() is True


def test_own_job_returns_the_claimed_row(made):
    uid, rid, jid, job = _claimed_summary_job(made)
    assert job_queue._own_job(jid, rid, job.created_at).id == jid
    assert job_queue._own_job(jid, rid + 100000, job.created_at) is None
    assert job_queue._own_job(jid, rid, job.created_at - timedelta(seconds=5)) is None
