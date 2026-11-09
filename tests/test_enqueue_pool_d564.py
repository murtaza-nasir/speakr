"""One pool connection per upload request while it enqueues (D564).

With many uploads in one process (gunicorn --workers 1 --threads 16), each
request held a connection when it reached job_queue.enqueue(), which then
took a second one for its own session while holding _enqueue_lock. With every
connection checked out by threads queued on the lock, the lock holder could
only wait for the 30 s pool timeout, and uploads failed with QueuePool errors.

SHARED-DB: the test user and recording are removed afterwards.
"""
import secrets
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import job_queue
from src.services.recording_ingestion import enqueue_ingestion_jobs


@pytest.fixture
def recording():
    job_queue.init_app(app)
    with app.app_context():
        s = secrets.token_hex(4)
        user = User(username=f"d564_{s}", email=f"d564_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="Pool", audio_path="local://pool.mp3", status="PENDING")
        db.session.add(rec)
        db.session.commit()
        ids = (user.id, rec.id)
    yield ids
    with app.app_context():
        ProcessingJob.query.filter_by(recording_id=ids[1]).delete()
        db.session.delete(db.session.get(Recording, ids[1]))
        db.session.delete(db.session.get(User, ids[0]))
        db.session.commit()


@pytest.mark.parametrize("deferred", [False, True])
def test_the_upload_holds_no_connection_of_its_own_when_it_enqueues(recording, deferred):
    uid, rid = recording
    seen = []
    with app.test_request_context():
        rec = db.session.get(Recording, rid)
        owner = db.session.get(User, uid)
        with patch("src.api.recordings.job_queue") as queue, \
                patch("src.services.webhook_dispatch.emit_recording_created"):
            queue.enqueue.side_effect = lambda **kw: seen.append((db.session().in_transaction(), kw["recording_id"]))
            enqueue_ingestion_jobs(rec, owner=owner, resolved_params={}, conversion_deferred=deferred,
                                   extracted_audio_limit_mb=None)
    assert seen == [(False, rid)]


class _RecordingLock:
    """Stands in for _enqueue_lock and notes whether a connection is already held."""

    def __init__(self):
        self.connection_held_on_entry = []

    def __enter__(self):
        self.connection_held_on_entry.append(db.session().in_transaction())
        return self

    def __exit__(self, *exc):
        return False


def test_enqueue_takes_its_connection_before_the_lock(recording):
    uid, rid = recording
    lock = _RecordingLock()
    with patch.object(job_queue, "_enqueue_lock", lock), patch.object(job_queue, "start"):
        job_id = job_queue.enqueue(uid, rid, "transcribe")
    assert lock.connection_held_on_entry == [True]
    with app.app_context():
        assert db.session.get(ProcessingJob, job_id).recording_id == rid
