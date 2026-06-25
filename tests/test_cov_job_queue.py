"""Coverage-focused tests for src/services/job_queue.py (FairJobQueue).

These complement tests/test_job_queue_race_condition.py (which covers the
atomic-claim concurrency path). Here we drive the public + internal methods
directly with workers OFF (conftest sets JOB_QUEUE_WORKERS=SUMMARY_QUEUE_WORKERS=0),
mocking the task functions, storage materialize layer, and webhook emission so
no real audio processing or network I/O happens.

Shared-DB note: the pytest session DB is shared across test files and the
queue singleton is global, so every assertion is scoped to job/recording IDs
this module created (tracked per-test and cleaned up).
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import (
    FairJobQueue,
    job_queue,
    TRANSCRIPTION_JOBS,
    SUMMARY_JOBS,
    MAX_RETRIES,
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bind_queue():
    """Bind the singleton queue to the test app and reset round-robin state."""
    job_queue.init_app(app)
    # Reset fairness cursors so cross-test ordering is deterministic.
    job_queue._last_user_id_transcription = None
    job_queue._last_user_id_summary = None
    yield


_user_counter = {"n": 0}


def _make_user():
    """Create a unique throwaway user, returning its id."""
    _user_counter["n"] += 1
    n = _user_counter["n"]
    suffix = f"{os.getpid()}_{n}_{datetime.utcnow().timestamp()}"
    user = User(username=f"jq_{n}_{os.getpid()}"[:20],
                email=f"jq_{suffix}@example.com")
    if hasattr(user, "password"):
        user.password = "unused"
    db.session.add(user)
    db.session.commit()
    return user.id


def _make_recording(user_id, status="PENDING", audio_path="/tmp/jq_test.mp3",
                    original_filename="orig.mp3"):
    rec = Recording(
        user_id=user_id,
        title="JQ Test Recording",
        audio_path=audio_path,
        original_filename=original_filename,
        status=status,
    )
    db.session.add(rec)
    db.session.commit()
    return rec.id


class _Tracker:
    """Tracks created job/recording/user ids for cleanup."""

    def __init__(self):
        self.job_ids = []
        self.recording_ids = []
        self.user_ids = []

    def cleanup(self):
        for jid in self.job_ids:
            j = db.session.get(ProcessingJob, jid)
            if j:
                db.session.delete(j)
        # also delete any jobs attached to our recordings (e.g. follow-ups)
        for rid in self.recording_ids:
            for j in ProcessingJob.query.filter_by(recording_id=rid).all():
                db.session.delete(j)
        db.session.commit()
        for rid in self.recording_ids:
            r = db.session.get(Recording, rid)
            if r:
                db.session.delete(r)
        db.session.commit()
        for uid in self.user_ids:
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
        db.session.commit()


@pytest.fixture
def track():
    with app.app_context():
        t = _Tracker()
        try:
            yield t
        finally:
            t.cleanup()


def _fake_materialized(path):
    """A storage service whose materialize() yields an object with local_path."""
    storage = MagicMock()
    mat = MagicMock()
    mat.local_path = path

    @contextmanager
    def _materialize(audio_path):
        yield mat

    storage.materialize.side_effect = _materialize
    return storage


# --------------------------------------------------------------------------
# Singleton
# --------------------------------------------------------------------------

def test_singleton_identity():
    assert FairJobQueue() is job_queue
    assert FairJobQueue() is FairJobQueue()


# --------------------------------------------------------------------------
# enqueue
# --------------------------------------------------------------------------

def test_enqueue_creates_transcribe_job_and_sets_recording_queued(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid, status="PENDING"); track.recording_ids.append(rid)

    jid = job_queue.enqueue(uid, rid, "transcribe",
                            params={"language": "en"}, is_new_upload=True)
    track.job_ids.append(jid)

    job = db.session.get(ProcessingJob, jid)
    assert job.job_type == "transcribe"
    assert job.status == "queued"
    assert job.user_id == uid
    assert job.recording_id == rid
    assert job.is_new_upload is True
    assert json.loads(job.params) == {"language": "en"}

    rec = db.session.get(Recording, rid)
    assert rec.status == "QUEUED"


def test_enqueue_summary_job_sets_recording_summarizing(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    jid = job_queue.enqueue(uid, rid, "summarize")
    track.job_ids.append(jid)

    job = db.session.get(ProcessingJob, jid)
    assert job.job_type == "summarize"
    assert job.params is None  # no params -> stored as NULL
    assert db.session.get(Recording, rid).status == "SUMMARIZING"


def test_enqueue_dedupes_same_type_active_job(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    jid1 = job_queue.enqueue(uid, rid, "transcribe")
    track.job_ids.append(jid1)
    jid2 = job_queue.enqueue(uid, rid, "transcribe")

    assert jid1 == jid2  # returns existing job id, no duplicate
    count = ProcessingJob.query.filter_by(recording_id=rid,
                                          job_type="transcribe").count()
    assert count == 1


def test_enqueue_allows_different_job_types_to_coexist(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    jid1 = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid1)
    jid2 = job_queue.enqueue(uid, rid, "summarize"); track.job_ids.append(jid2)

    assert jid1 != jid2
    assert ProcessingJob.query.filter_by(recording_id=rid).count() == 2


def test_enqueue_does_not_autostart_when_workers_zero(track):
    """With JOB_QUEUE_WORKERS=0, enqueue calls start() but no threads spawn."""
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)
    assert job_queue._transcription_workers == []
    assert job_queue._summary_workers == []
    # reset _running flag so other tests' enqueue path keeps exercising start()
    job_queue._running = False


# --------------------------------------------------------------------------
# _claim_next_job
# --------------------------------------------------------------------------

def test_claim_returns_none_when_no_matching_jobs():
    # Use a job_type that no one ever enqueues to guarantee emptiness.
    assert job_queue._claim_next_job(["__nonexistent_type__"], "transcription") is None


def test_claim_marks_job_processing_and_recording_processing(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid, status="QUEUED"); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)

    claimed = job_queue._claim_next_job(["transcribe"], "transcription")
    assert claimed is not None
    assert claimed.id == jid
    assert claimed.status == "processing"
    assert claimed.started_at is not None

    assert db.session.get(Recording, rid).status == "PROCESSING"
    # round-robin cursor updated for transcription queue
    assert job_queue._last_user_id_transcription == uid


def test_claim_picks_oldest_queued_job_for_user(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid1 = _make_recording(uid); track.recording_ids.append(rid1)
    rid2 = _make_recording(uid); track.recording_ids.append(rid2)

    j1 = job_queue.enqueue(uid, rid1, "transcribe"); track.job_ids.append(j1)
    j2 = job_queue.enqueue(uid, rid2, "transcribe"); track.job_ids.append(j2)
    # Force j1 to be older.
    job = db.session.get(ProcessingJob, j1)
    job.created_at = datetime.utcnow() - timedelta(minutes=5)
    db.session.commit()

    claimed = job_queue._claim_next_job(["transcribe"], "transcription")
    assert claimed.id == j1


def test_claim_summary_queue_uses_separate_cursor(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "summarize"); track.job_ids.append(jid)

    claimed = job_queue._claim_next_job(SUMMARY_JOBS, "summary")
    assert claimed.id == jid
    assert job_queue._last_user_id_summary == uid
    assert job_queue._last_user_id_transcription is None


# --------------------------------------------------------------------------
# fairness / round-robin across users
# --------------------------------------------------------------------------

def test_round_robin_across_two_users(track):
    u1 = _make_user(); track.user_ids.append(u1)
    u2 = _make_user(); track.user_ids.append(u2)
    r1 = _make_recording(u1); track.recording_ids.append(r1)
    r2 = _make_recording(u2); track.recording_ids.append(r2)
    r1b = _make_recording(u1); track.recording_ids.append(r1b)

    # u1 enqueues two jobs first (oldest), u2 enqueues one in the middle.
    j1 = job_queue.enqueue(u1, r1, "transcribe"); track.job_ids.append(j1)
    # make u1's jobs the oldest by user-min so u1 sorts first
    db.session.get(ProcessingJob, j1).created_at = datetime.utcnow() - timedelta(minutes=10)
    db.session.commit()
    j2 = job_queue.enqueue(u2, r2, "transcribe"); track.job_ids.append(j2)
    j1b = job_queue.enqueue(u1, r1b, "transcribe"); track.job_ids.append(j1b)

    first = job_queue._claim_next_job(["transcribe"], "transcription")
    # u1 has the oldest min(created_at) so it goes first (cursor was None)
    assert first.user_id == u1
    second = job_queue._claim_next_job(["transcribe"], "transcription")
    # Round-robin: after u1, the next distinct user (u2) should be served.
    assert second.user_id == u2
    third = job_queue._claim_next_job(["transcribe"], "transcription")
    # Back to u1 for its remaining job.
    assert third.user_id == u1


def test_round_robin_cursor_advances_across_three_users(track):
    """The fairness cursor must advance to the user AFTER the last-claimed one.

    Mutation guard for `_claim_next_job` line 188
    (`if last_user_id is not None and last_user_id in user_ids:`):
    the `is not None`->`is None` mutation collapses the round-robin to always
    picking ``user_ids[0]`` (the lowest min(created_at) user). We make the
    just-claimed user u1 *retain* the globally-oldest queued job so it stays at
    ``user_ids[0]``; correct round-robin must still skip past it to u2, while the
    mutant would re-pick u1.
    """
    u1 = _make_user(); track.user_ids.append(u1)
    u2 = _make_user(); track.user_ids.append(u2)
    u3 = _make_user(); track.user_ids.append(u3)
    # u1 owns TWO recordings/jobs so it keeps a queued job after the first claim.
    r1a = _make_recording(u1); track.recording_ids.append(r1a)
    r1b = _make_recording(u1); track.recording_ids.append(r1b)
    r2 = _make_recording(u2); track.recording_ids.append(r2)
    r3 = _make_recording(u3); track.recording_ids.append(r3)

    j1a = job_queue.enqueue(u1, r1a, "transcribe"); track.job_ids.append(j1a)
    j1b = job_queue.enqueue(u1, r1b, "transcribe"); track.job_ids.append(j1b)
    j2 = job_queue.enqueue(u2, r2, "transcribe"); track.job_ids.append(j2)
    j3 = job_queue.enqueue(u3, r3, "transcribe"); track.job_ids.append(j3)

    # Force created_at so the user ordering by min(created_at) is u1 < u2 < u3,
    # AND u1 still holds the oldest queued job even after one of its jobs is
    # claimed (both u1 jobs predate u2/u3).
    now = datetime.utcnow()
    db.session.get(ProcessingJob, j1a).created_at = now - timedelta(minutes=30)
    db.session.get(ProcessingJob, j1b).created_at = now - timedelta(minutes=29)
    db.session.get(ProcessingJob, j2).created_at = now - timedelta(minutes=20)
    db.session.get(ProcessingJob, j3).created_at = now - timedelta(minutes=10)
    db.session.commit()

    first = job_queue._claim_next_job(["transcribe"], "transcription")
    assert first.user_id == u1            # cursor was None -> user_ids[0] == u1
    assert job_queue._last_user_id_transcription == u1

    second = job_queue._claim_next_job(["transcribe"], "transcription")
    # u1 still owns the globally-oldest queued job (j1b), so user_ids stays
    # [u1, u2, u3]. Correct round-robin advances PAST u1 to u2. The mutant
    # (is None) ignores the cursor and re-picks user_ids[0] == u1.
    assert second.user_id == u2
    assert job_queue._last_user_id_transcription == u2


# --------------------------------------------------------------------------
# _process_job dispatch routing
# --------------------------------------------------------------------------

def _claim(rid, job_types, queue_name):
    return job_queue._claim_next_job(job_types, queue_name)


def test_process_job_transcribe_dispatches_task_and_completes(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid, status="QUEUED"); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe",
                            params={"language": "fr", "tag_id": 7})
    track.job_ids.append(jid)
    job = _claim(rid, ["transcribe"], "transcription")

    fake_task = MagicMock()
    storage = _fake_materialized("/tmp/jq_test.mp3")

    with patch("src.tasks.processing.transcribe_audio_task", fake_task), \
         patch("src.services.storage.get_storage_service", return_value=storage), \
         patch("os.path.exists", return_value=True), \
         patch.object(job_queue, "_emit_started_webhook") as started, \
         patch.object(job_queue, "_emit_completion_webhook") as completed:
        job_queue._process_job(job)

    fake_task.assert_called_once()
    _, kwargs = fake_task.call_args
    assert kwargs["language"] == "fr"
    assert kwargs["tag_id"] == 7
    # positional: app_context, recording_id, filepath, filename, start_time
    args = fake_task.call_args[0]
    assert args[1] == rid
    assert args[2] == "/tmp/jq_test.mp3"

    started.assert_called_once_with("transcribe", rid)
    completed.assert_called_once_with("transcribe", rid)

    refreshed = db.session.get(ProcessingJob, jid)
    assert refreshed.status == "completed"
    assert refreshed.completed_at is not None


def test_transcribe_uses_original_filename_when_set(track):
    """When recording.original_filename is set, it is passed to the ASR task.

    Mutation guard for `_run_transcription` line 432
    (`recording.original_filename or os.path.basename(filepath)`): the
    `or`->`and` mutation yields `original_filename and basename` == basename
    whenever original_filename is truthy, wrongly dropping the real filename.
    The materialized basename ("mat_name.mp3") is deliberately different from
    the original_filename ("my recording.mp3") so the two are distinguishable.
    """
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid, status="QUEUED",
                          original_filename="my recording.mp3")
    track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)
    job = _claim(rid, ["transcribe"], "transcription")

    fake_task = MagicMock()
    storage = _fake_materialized("/var/tmp/mat_name.mp3")
    with patch("src.tasks.processing.transcribe_audio_task", fake_task), \
         patch("src.services.storage.get_storage_service", return_value=storage), \
         patch("os.path.exists", return_value=True), \
         patch.object(job_queue, "_emit_started_webhook"), \
         patch.object(job_queue, "_emit_completion_webhook"):
        job_queue._process_job(job)

    fake_task.assert_called_once()
    # positional: app_context, recording_id, filepath, filename_for_asr, start
    args = fake_task.call_args[0]
    assert args[3] == "my recording.mp3"     # original filename, NOT basename


def test_transcribe_falls_back_to_basename_when_no_original_filename(track):
    """When original_filename is empty, the materialized basename is used.

    Same line-432 guard from the other direction: with an empty
    original_filename, the correct `or` expression returns
    basename(filepath); the `and` mutant would return the empty string.
    """
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid, status="QUEUED", original_filename="")
    track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)
    job = _claim(rid, ["transcribe"], "transcription")

    fake_task = MagicMock()
    storage = _fake_materialized("/var/tmp/materialized_basename.mp3")
    with patch("src.tasks.processing.transcribe_audio_task", fake_task), \
         patch("src.services.storage.get_storage_service", return_value=storage), \
         patch("os.path.exists", return_value=True), \
         patch.object(job_queue, "_emit_started_webhook"), \
         patch.object(job_queue, "_emit_completion_webhook"):
        job_queue._process_job(job)

    fake_task.assert_called_once()
    args = fake_task.call_args[0]
    assert args[3] == "materialized_basename.mp3"   # basename fallback, not ""


def test_process_job_summarize_dispatches_summary_task(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "summarize",
                            params={"custom_prompt": "Hi", "user_id": uid})
    track.job_ids.append(jid)
    job = _claim(rid, SUMMARY_JOBS, "summary")

    fake_task = MagicMock()
    with patch("src.tasks.processing.generate_summary_only_task", fake_task), \
         patch.object(job_queue, "_emit_completion_webhook"):
        job_queue._process_job(job)

    fake_task.assert_called_once()
    kwargs = fake_task.call_args[1]
    assert kwargs["custom_prompt_override"] == "Hi"
    assert kwargs["user_id"] == uid
    assert db.session.get(ProcessingJob, jid).status == "completed"


def test_process_job_reprocess_transcription_routes_to_transcribe_task(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "reprocess_transcription",
                            params={"min_speakers": 2})
    track.job_ids.append(jid)
    job = _claim(rid, TRANSCRIPTION_JOBS, "transcription")

    fake_task = MagicMock()
    storage = _fake_materialized("/tmp/jq_test.mp3")
    with patch("src.tasks.processing.transcribe_audio_task", fake_task), \
         patch("src.services.storage.get_storage_service", return_value=storage), \
         patch("os.path.exists", return_value=True), \
         patch.object(job_queue, "_emit_started_webhook"), \
         patch.object(job_queue, "_emit_completion_webhook"):
        job_queue._process_job(job)

    fake_task.assert_called_once()
    assert fake_task.call_args[1]["min_speakers"] == 2
    assert db.session.get(ProcessingJob, jid).status == "completed"


def test_process_job_reprocess_summary_routes_to_summary_task(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "reprocess_summary",
                            params={"custom_prompt_append": True})
    track.job_ids.append(jid)
    job = _claim(rid, SUMMARY_JOBS, "summary")

    fake_task = MagicMock()
    with patch("src.tasks.processing.generate_summary_only_task", fake_task), \
         patch.object(job_queue, "_emit_completion_webhook"):
        job_queue._process_job(job)

    fake_task.assert_called_once()
    assert fake_task.call_args[1]["custom_prompt_append"] is True
    assert db.session.get(ProcessingJob, jid).status == "completed"


def test_process_job_unknown_type_fails(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    # Insert a job with an unknown type directly (enqueue won't validate it,
    # but the claim path filters by type list, so create + claim manually).
    job = ProcessingJob(user_id=uid, recording_id=rid,
                        job_type="bogus_type", status="processing",
                        started_at=datetime.utcnow())
    db.session.add(job); db.session.commit()
    track.job_ids.append(job.id)

    with patch.object(job_queue, "_emit_failure_webhook"):
        job_queue._process_job(job)

    db.session.expire_all()
    refreshed = db.session.get(ProcessingJob, job.id)
    # "Unknown job type" is not in the permanent-error list, so it is a
    # transient failure: re-queued for retry (retry_count < MAX_RETRIES).
    assert refreshed.status == "queued"
    assert refreshed.retry_count == 1
    assert "Unknown job type" in refreshed.error_message


# --------------------------------------------------------------------------
# success / failure / retry transitions
# --------------------------------------------------------------------------

def test_transient_failure_requeues_for_retry(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "summarize"); track.job_ids.append(jid)
    job = _claim(rid, SUMMARY_JOBS, "summary")

    boom = MagicMock(side_effect=RuntimeError("temporary network blip"))
    with patch("src.tasks.processing.generate_summary_only_task", boom):
        job_queue._process_job(job)

    refreshed = db.session.get(ProcessingJob, jid)
    assert refreshed.status == "queued"       # re-queued for retry
    assert refreshed.retry_count == 1
    assert refreshed.started_at is None
    assert "temporary network blip" in refreshed.error_message


def test_permanent_error_fails_without_retry(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "summarize"); track.job_ids.append(jid)
    job = _claim(rid, SUMMARY_JOBS, "summary")

    boom = MagicMock(side_effect=RuntimeError("Error code: 401 - invalid api key"))
    with patch("src.tasks.processing.generate_summary_only_task", boom), \
         patch.object(job_queue, "_emit_failure_webhook") as fail_hook:
        job_queue._process_job(job)

    refreshed = db.session.get(ProcessingJob, jid)
    assert refreshed.status == "failed"
    assert refreshed.retry_count == 1   # incremented once, but not retried
    assert db.session.get(Recording, rid).status == "FAILED"
    fail_hook.assert_called_once()


def test_retry_exhaustion_marks_failed(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "summarize"); track.job_ids.append(jid)
    job = _claim(rid, SUMMARY_JOBS, "summary")
    # Pre-set retry_count so the next failure crosses MAX_RETRIES.
    db.session.get(ProcessingJob, jid).retry_count = MAX_RETRIES - 1
    db.session.commit()

    boom = MagicMock(side_effect=RuntimeError("transient again"))
    with patch("src.tasks.processing.generate_summary_only_task", boom), \
         patch.object(job_queue, "_emit_failure_webhook"):
        job_queue._process_job(job)

    refreshed = db.session.get(ProcessingJob, jid)
    assert refreshed.status == "failed"
    assert refreshed.retry_count == MAX_RETRIES
    assert db.session.get(Recording, rid).status == "FAILED"


def test_process_job_missing_recording_fails(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "summarize"); track.job_ids.append(jid)
    job = _claim(rid, SUMMARY_JOBS, "summary")

    # Simulate the recording lookup returning None inside _process_job without
    # actually deleting the row (FK cascade would remove the job too). Patch
    # db.session.get so Recording lookups miss while ProcessingJob lookups hit.
    real_get = db.session.get

    def fake_get(model, ident, *a, **kw):
        if model is Recording:
            return None
        return real_get(model, ident, *a, **kw)

    with patch.object(db.session, "get", side_effect=fake_get), \
         patch.object(job_queue, "_emit_failure_webhook"):
        job_queue._process_job(job)

    db.session.expire_all()
    refreshed = db.session.get(ProcessingJob, jid)
    # "Recording not found" is transient (not in permanent-error list) -> retry.
    assert refreshed.status == "queued"
    assert refreshed.retry_count == 1
    assert "not found" in refreshed.error_message.lower()


# --------------------------------------------------------------------------
# _is_permanent_error
# --------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "Error code: 400 - bad request",
    "Error code: 413 payload too large",
    "status 404 model not found",
    "Invalid API key provided",
    "quota exceeded for this month",
    "unsupported format",
])
def test_is_permanent_error_true(msg):
    assert job_queue._is_permanent_error(msg) is True


@pytest.mark.parametrize("msg", [
    "Error code: 500 - internal server error",
    "Connection timed out",
    "temporary network blip",
    "",
])
def test_is_permanent_error_false(msg):
    assert job_queue._is_permanent_error(msg) is False


# --------------------------------------------------------------------------
# started-webhook emission inside materialize block
# --------------------------------------------------------------------------

def test_started_webhook_skipped_when_file_missing(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)
    job = _claim(rid, TRANSCRIPTION_JOBS, "transcription")

    fake_task = MagicMock()
    storage = _fake_materialized("/tmp/does_not_exist_xyz.mp3")
    with patch("src.tasks.processing.transcribe_audio_task", fake_task), \
         patch("src.services.storage.get_storage_service", return_value=storage), \
         patch("os.path.exists", return_value=False), \
         patch.object(job_queue, "_emit_started_webhook") as started, \
         patch.object(job_queue, "_emit_completion_webhook"):
        job_queue._process_job(job)

    started.assert_not_called()           # file missing -> no started emit
    fake_task.assert_called_once()        # task still runs


def test_transcription_missing_audio_path_raises_and_fails(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid, audio_path=""); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)
    job = _claim(rid, TRANSCRIPTION_JOBS, "transcription")

    with patch.object(job_queue, "_emit_failure_webhook"):
        job_queue._process_job(job)

    db.session.expire_all()
    refreshed = db.session.get(ProcessingJob, jid)
    # Missing audio_path raises a transient ValueError -> re-queued for retry.
    assert refreshed.status == "queued"
    assert refreshed.retry_count == 1
    assert "audio_path" in refreshed.error_message


def test_emit_started_webhook_dispatches_event(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    with patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        job_queue._emit_started_webhook("transcribe", rid)
    emit.assert_called_once()
    assert emit.call_args[1]["event_type"] == "recording.transcription.started"
    assert emit.call_args[1]["data"]["recording_id"] == rid


def test_emit_started_webhook_noop_for_unmapped_type(track):
    with patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        job_queue._emit_started_webhook("summarize", 123)
    emit.assert_not_called()


def test_emit_completion_webhook_dispatches_event(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    with patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        job_queue._emit_completion_webhook("summarize", rid)
    emit.assert_called_once()
    assert emit.call_args[1]["event_type"] == "recording.summary.completed"


def test_emit_failure_webhook_dispatches_event_and_truncates(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)

    long_err = "x" * 1000
    with patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        job_queue._emit_failure_webhook("transcribe", rid, long_err)
    emit.assert_called_once()
    assert emit.call_args[1]["event_type"] == "recording.transcription.failed"
    assert len(emit.call_args[1]["data"]["error"]) == 500


def test_emit_webhook_noop_when_recording_missing():
    with patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        job_queue._emit_completion_webhook("transcribe", 999999999)
    emit.assert_not_called()


# --------------------------------------------------------------------------
# queue status / counts / position
# --------------------------------------------------------------------------

def test_get_queue_status_counts(track):
    uid = _make_user(); track.user_ids.append(uid)
    rt = _make_recording(uid); track.recording_ids.append(rt)
    rs = _make_recording(uid); track.recording_ids.append(rs)

    base = job_queue.get_queue_status()
    jt = job_queue.enqueue(uid, rt, "transcribe"); track.job_ids.append(jt)
    js = job_queue.enqueue(uid, rs, "summarize"); track.job_ids.append(js)

    status = job_queue.get_queue_status()
    assert status["transcription_queue"]["queued"] >= base["transcription_queue"]["queued"] + 1
    assert status["summary_queue"]["queued"] >= base["summary_queue"]["queued"] + 1
    assert status["transcription_queue"]["workers"] == 0
    assert status["summary_queue"]["workers"] == 0
    assert "is_running" in status


def test_get_position_in_queue(track):
    uid = _make_user(); track.user_ids.append(uid)
    r1 = _make_recording(uid); track.recording_ids.append(r1)
    r2 = _make_recording(uid); track.recording_ids.append(r2)

    j1 = job_queue.enqueue(uid, r1, "transcribe"); track.job_ids.append(j1)
    db.session.get(ProcessingJob, j1).created_at = datetime.utcnow() - timedelta(minutes=30)
    db.session.commit()
    j2 = job_queue.enqueue(uid, r2, "transcribe"); track.job_ids.append(j2)

    pos1 = job_queue.get_position_in_queue(r1)
    pos2 = job_queue.get_position_in_queue(r2)
    assert pos1 is not None and pos2 is not None
    assert pos2 > pos1  # r2 enqueued later -> further back


def test_get_position_in_queue_none_for_unqueued():
    assert job_queue.get_position_in_queue(987654321) is None


def test_get_job_for_recording(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    jid = job_queue.enqueue(uid, rid, "transcribe"); track.job_ids.append(jid)

    job = job_queue.get_job_for_recording(rid)
    assert job is not None and job.id == jid
    assert job_queue.get_job_for_recording(987654321) is None


# --------------------------------------------------------------------------
# orphan recovery / cleanup
# --------------------------------------------------------------------------

def test_recover_orphaned_jobs(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    job = ProcessingJob(user_id=uid, recording_id=rid, job_type="transcribe",
                        status="processing", started_at=datetime.utcnow())
    db.session.add(job); db.session.commit()
    track.job_ids.append(job.id)

    job_queue.recover_orphaned_jobs()

    db.session.expire_all()
    refreshed = db.session.get(ProcessingJob, job.id)
    assert refreshed.status == "queued"
    assert refreshed.started_at is None


def test_cleanup_old_jobs(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    old = ProcessingJob(user_id=uid, recording_id=rid, job_type="transcribe",
                        status="completed",
                        completed_at=datetime.utcnow() - timedelta(hours=48))
    db.session.add(old); db.session.commit()
    old_id = old.id
    track.job_ids.append(old_id)

    job_queue.cleanup_old_jobs(max_age_hours=24)

    db.session.expire_all()
    assert db.session.get(ProcessingJob, old_id) is None
    track.job_ids.remove(old_id)


def test_cleanup_old_jobs_keeps_recent(track):
    uid = _make_user(); track.user_ids.append(uid)
    rid = _make_recording(uid); track.recording_ids.append(rid)
    recent = ProcessingJob(user_id=uid, recording_id=rid, job_type="transcribe",
                           status="completed",
                           completed_at=datetime.utcnow() - timedelta(hours=1))
    db.session.add(recent); db.session.commit()
    track.job_ids.append(recent.id)

    job_queue.cleanup_old_jobs(max_age_hours=24)

    assert db.session.get(ProcessingJob, recent.id) is not None
