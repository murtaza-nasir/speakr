"""The summary after a transcript is a job of its own (D#663), and no step after
the transcript fails or retries the transcription job (#684).

Status contract: the transcription job stores the transcript and the title,
queues a 'summarize' job marked after_transcription and ends, so its worker
is free. The recording stays SUMMARIZING with its transcript until the
summary job ends it: COMPLETED, then recording.transcription.completed and
the completion email, once. A failed summary leaves it COMPLETED with the
transcript. A cancel during the transcription queues no summary job; a
cancel of the summary job leaves the recording COMPLETED.

#684: a database timeout in the title step (or anywhere after the transcript
was committed) reached the job queue, whose retry ran the ASR again, and the
log blamed the connector's timeout.

SHARED-DB: every test removes the users, recordings and jobs it made.
"""

import json
import logging
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
import src.tasks.processing as proc
from src.services import job_control
from src.services.job_cancel import cancel_job
from src.services.job_queue import (
    job_queue, SUMMARY_JOBS, SUMMARY_QUEUED, AUTO_SUMMARY_PARAMS, MAX_RETRIES, is_auto_summary,
)

TRANSCRIPT = "Hello there, this is a transcript long enough to summarize."


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


def _recording(made, status='PENDING', transcription=None, **user_fields):
    s = uuid.uuid4().hex[:8]
    user = User(username=f"sa_{s}", email=f"sa_{s}@local.test", password="x", **user_fields)
    db.session.add(user)
    db.session.commit()
    made['users'].append(user.id)
    rec = Recording(user_id=user.id, original_filename="s.mp3", audio_path="local://s.mp3",
                    status=status, title="sa", transcription=transcription)
    db.session.add(rec)
    db.session.commit()
    made['recordings'].append(rec.id)
    return rec


def _fresh(model, pk):
    db.session.expire_all()
    return db.session.get(model, pk)


def _summary_jobs(rid):
    db.session.expire_all()
    return ProcessingJob.query.filter(ProcessingJob.recording_id == rid,
                                      ProcessingJob.job_type == 'summarize').all()


# --- the transcription job ---------------------------------------------------------

def _connector(text):
    connector = MagicMock()
    connector.PROVIDER_NAME = "test-connector"
    connector.supports_diarization = False
    connector.default_diarize = False
    specs = MagicMock()
    specs.max_duration_seconds = 1800
    specs.handles_chunking_internally = False
    specs.recommended_chunk_seconds = 600
    specs.unsupported_codecs = []
    connector.specifications = specs

    def transcribe(request):
        if isinstance(text, BaseException):
            raise text
        response = MagicMock()
        response.text = text
        response.segments = None
        response.speakers = None
        response.speaker_embeddings = None
        response.language = "en"
        response.has_diarization.return_value = False
        return response

    connector.transcribe.side_effect = transcribe
    return connector


def _transcribe(rid, connector, title=None, summary=None):
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"sa_{rid}.mp3")
    with open(path, "wb") as f:
        f.write(b"\x00" * 1024)
    conv = MagicMock()
    conv.was_converted = False
    title = title or MagicMock()
    summary = summary or MagicMock()
    with patch("src.services.transcription.get_connector", return_value=connector), \
            patch.object(proc, "is_video_file", return_value=False), \
            patch.object(proc, "convert_if_needed", return_value=conv), \
            patch.object(proc.llm_config, "is_configured", return_value=True), \
            patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
            patch.object(proc, "generate_title_task", title), \
            patch.object(proc, "generate_summary_only_task", summary), \
            patch.object(proc, "chunking_service") as chunking:
        chunking.needs_chunking.return_value = False
        chunking.get_audio_duration.return_value = 60.0
        outcome = proc.transcribe_with_connector(app.app_context(), rid, path, "s.mp3", time.time(),
                                                 mime_type="audio/mpeg")
    return outcome, title, summary


def test_the_transcription_queues_the_summary_and_ends(made):
    rec = _recording(made)
    outcome, title, summary = _transcribe(rec.id, _connector(TRANSCRIPT))

    assert outcome is SUMMARY_QUEUED
    title.assert_called_once()
    assert title.call_args.kwargs['will_auto_summarize'] is True
    summary.assert_not_called()   # not on the transcription worker any more
    jobs = _summary_jobs(rec.id)
    assert len(jobs) == 1 and jobs[0].status == 'queued'
    assert json.loads(jobs[0].params) == AUTO_SUMMARY_PARAMS
    after = _fresh(Recording, rec.id)
    assert after.status == 'SUMMARIZING'
    assert after.transcription == TRANSCRIPT   # readable while the summary waits


def test_without_auto_summary_no_summary_job_is_queued(made):
    rec = _recording(made, auto_summarization=False)
    outcome, title, _ = _transcribe(rec.id, _connector(TRANSCRIPT))
    assert outcome is None
    assert title.call_args.kwargs['will_auto_summarize'] is False
    assert _summary_jobs(rec.id) == []


def test_a_database_timeout_in_the_title_step_does_not_fail_the_job(made, caplog):
    """#684: the transcript is committed; the job completes, nothing is transcribed again."""
    rec = _recording(made)
    title = MagicMock(side_effect=OperationalError("UPDATE recording", {}, Exception("database timeout")))
    with caplog.at_level(logging.ERROR):
        outcome, _, _ = _transcribe(rec.id, _connector(TRANSCRIPT), title=title)

    assert outcome is SUMMARY_QUEUED
    after = _fresh(Recording, rec.id)
    assert after.transcription == TRANSCRIPT
    assert after.status == 'SUMMARIZING'
    assert len(_summary_jobs(rec.id)) == 1
    assert "Timeout details" not in caplog.text
    assert "Connector transcription FAILED" not in caplog.text


def test_a_failure_after_the_transcript_finishes_a_recording_without_summary(made):
    rec = _recording(made, auto_summarization=False)
    title = MagicMock(side_effect=OperationalError("UPDATE recording", {}, Exception("database timeout")))
    outcome, _, _ = _transcribe(rec.id, _connector(TRANSCRIPT), title=title)

    assert outcome is None
    after = _fresh(Recording, rec.id)
    assert after.status == 'COMPLETED'
    assert after.transcription == TRANSCRIPT


def test_an_asr_timeout_still_fails_with_the_connector_details(made, caplog):
    rec = _recording(made)
    with caplog.at_level(logging.ERROR), pytest.raises(Exception):
        _transcribe(rec.id, _connector(TimeoutError("Read timed out")))
    assert "Timeout details" in caplog.text
    assert _summary_jobs(rec.id) == []


class _CancelAfterTitle(job_control.JobControl):
    def __init__(self):
        super().__init__(None)
        self.canceled = False

    def stage(self, code):
        return None

    def cancel_requested(self):
        return self.canceled


def test_a_cancel_during_the_transcription_queues_no_summary_job(made):
    rec = _recording(made)
    control = _CancelAfterTitle()

    def title(*args, **kwargs):
        control.canceled = True

    with job_control.running(control), pytest.raises(job_control.JobCanceled):
        _transcribe(rec.id, _connector(TRANSCRIPT), title=MagicMock(side_effect=title))
    assert _summary_jobs(rec.id) == []


# --- the queue ----------------------------------------------------------------------

def _summary_job(made, status='SUMMARIZING', retry_count=0):
    rec = _recording(made, status='PROCESSING', transcription=TRANSCRIPT)
    jid = job_queue.enqueue(rec.user_id, rec.id, 'summarize', params=dict(AUTO_SUMMARY_PARAMS))
    if retry_count:
        row = db.session.get(ProcessingJob, jid)
        row.retry_count = retry_count
        db.session.commit()
    return rec.id, jid


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


def test_the_transcription_job_hands_its_notices_to_the_summary_job(made):
    rec = _recording(made, status='QUEUED')
    jid = job_queue.enqueue(rec.user_id, rec.id, 'transcribe')
    job = _claim(jid)

    def runner(*args, **kwargs):
        return proc.queue_auto_summary(rec.id)

    with patch.object(job_queue, '_run_audio_job', side_effect=runner), \
            patch.object(job_queue, '_emit_completion_webhook') as hook, \
            patch.object(job_queue, '_emit_completion_email') as mail:
        job_queue._process_job(job)

    assert _fresh(ProcessingJob, jid).status == 'completed'
    assert _fresh(Recording, rec.id).status == 'SUMMARIZING'
    hook.assert_not_called()
    mail.assert_not_called()
    assert [j.status for j in _summary_jobs(rec.id)] == ['queued']


def test_the_summary_job_completes_the_recording_and_sends_the_notices_once(made):
    rid, jid = _summary_job(made)
    job = _claim(jid)

    def summary_step(*args, **kwargs):
        assert kwargs['after_transcription'] is True
        rec = db.session.get(Recording, rid)
        rec.summary = "A summary."
        db.session.commit()
        proc.finish_processing(rid)

    with patch("src.tasks.processing.generate_summary_only_task", side_effect=summary_step), \
            patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
            patch.object(job_queue, '_emit_completion_webhook') as hook, \
            patch.object(job_queue, '_emit_completion_email') as mail:
        job_queue._process_job(job)

    assert _fresh(Recording, rid).status == 'COMPLETED'
    hook.assert_called_once_with('transcribe', rid)
    mail.assert_called_once_with('transcribe', rid)


def test_a_failed_summary_leaves_the_recording_completed_with_its_transcript(made):
    rid, jid = _summary_job(made)
    job = _claim(jid)
    llm = MagicMock(side_effect=Exception("Error code: 403 - forbidden"))

    with patch.object(proc.llm_config, "is_configured", return_value=True), \
            patch.object(proc, "call_llm_completion", llm), \
            patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
            patch.object(job_queue, '_emit_failure_webhook') as failed_hook, \
            patch.object(job_queue, '_emit_completion_webhook') as done_hook, \
            patch.object(job_queue, '_emit_completion_email') as mail:
        job_queue._process_job(job)

    assert _fresh(ProcessingJob, jid).status == 'failed'
    after = _fresh(Recording, rid)
    assert after.status == 'COMPLETED'
    assert after.completed_at is not None
    assert after.transcription == TRANSCRIPT
    assert after.summary and after.summary.startswith('[')   # the stored summary error
    failed_hook.assert_called_once()
    assert failed_hook.call_args.args[0] == 'summarize'
    done_hook.assert_called_once_with('transcribe', rid)
    mail.assert_called_once_with('transcribe', rid)


def test_a_transient_summary_failure_is_retried_and_stays_summarizing(made):
    rid, jid = _summary_job(made)
    job = _claim(jid)
    llm = MagicMock(side_effect=Exception("connection reset by peer"))

    with patch.object(proc.llm_config, "is_configured", return_value=True), \
            patch.object(proc, "call_llm_completion", llm), \
            patch.object(job_queue, '_emit_failure_webhook') as failed_hook:
        job_queue._process_job(job)

    row = _fresh(ProcessingJob, jid)
    assert row.status == 'queued' and row.retry_count == 1
    assert _fresh(Recording, rid).status == 'SUMMARIZING'
    failed_hook.assert_not_called()


def test_canceling_the_waiting_summary_job_completes_the_recording(made):
    rid, jid = _summary_job(made)
    with patch.object(proc, "ENABLE_INQUIRE_MODE", False):
        assert cancel_job(jid) == 'removed'
    after = _fresh(Recording, rid)
    assert after.status == 'COMPLETED'
    assert after.transcription == TRANSCRIPT
    assert _fresh(ProcessingJob, jid) is None


def test_canceling_the_running_summary_job_completes_the_recording(made):
    rid, jid = _summary_job(made)
    job = _claim(jid)
    assert cancel_job(jid) == 'cancel_requested'

    def summary_step(*args, **kwargs):
        job_control.current().check_cancel()

    with patch("src.tasks.processing.generate_summary_only_task", side_effect=summary_step), \
            patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
            patch.object(job_queue, '_emit_completion_webhook') as hook:
        job_queue._process_job(job)

    assert _fresh(Recording, rid).status == 'COMPLETED'
    assert _fresh(ProcessingJob, jid) is None
    hook.assert_not_called()


def test_a_handed_off_recording_whose_summary_job_is_gone_is_finished(made):
    """A cancel of the transcription after its last safe point removed the summary job."""
    rec = _recording(made, status='QUEUED', transcription=TRANSCRIPT)
    jid = job_queue.enqueue(rec.user_id, rec.id, 'transcribe')
    job = _claim(jid)

    def runner(*args, **kwargs):
        outcome = proc.queue_auto_summary(rec.id)
        ProcessingJob.query.filter_by(recording_id=rec.id, job_type='summarize').delete()
        db.session.commit()
        return outcome

    with patch.object(job_queue, '_run_audio_job', side_effect=runner), \
            patch.object(proc, "ENABLE_INQUIRE_MODE", False):
        job_queue._process_job(job)

    assert _fresh(Recording, rec.id).status == 'COMPLETED'


def test_a_restart_resumes_the_summary_job(made):
    rid, jid = _summary_job(made)
    _claim(jid)
    assert _fresh(ProcessingJob, jid).status == 'processing'

    job_queue.recover_orphaned_jobs()

    row = _fresh(ProcessingJob, jid)
    assert row.status == 'queued'
    assert is_auto_summary(row.job_type, json.loads(row.params))
    assert _fresh(Recording, rid).status == 'SUMMARIZING'


def test_a_manual_summary_still_fails_the_recording(made):
    """Only the summary after a transcript keeps the recording COMPLETED."""
    rec = _recording(made, status='COMPLETED', transcription=TRANSCRIPT)
    jid = job_queue.enqueue(rec.user_id, rec.id, 'summarize')
    job = _claim(jid)
    llm = MagicMock(side_effect=Exception("Error code: 403 - forbidden"))
    with patch.object(proc.llm_config, "is_configured", return_value=True), \
            patch.object(proc, "call_llm_completion", llm), \
            patch.object(job_queue, '_emit_failure_webhook'), \
            patch.object(job_queue, '_emit_completion_webhook') as done_hook:
        job_queue._process_job(job)
    assert _fresh(ProcessingJob, jid).status == 'failed'
    assert _fresh(Recording, rec.id).status == 'FAILED'
    done_hook.assert_not_called()
