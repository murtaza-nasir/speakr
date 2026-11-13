"""An empty reply from the summary model is a summary failure.

A reasoning model that spends max_tokens on reasoning returns no content.
That reply used to be stored as "[Summary not generated]" with the recording
COMPLETED and recording.summary.completed sent. It is now handled like the
other summary failures of #583, with the finish reason and token usage in
the message and the log.
"""

import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User
from src.services.error_classification import SummaryStepFailed, is_permanent_error
import src.tasks.processing as proc

TRANSCRIPT = "word " * 50
LOCALES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "locales")


def _locale(lang):
    with open(os.path.join(LOCALES, f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def rec_id():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"esr_{s}", email=f"esr_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="t", status="SUMMARIZING", transcription=TRANSCRIPT,
                        original_filename="a.mp3", audio_path="local://a.mp3")
        db.session.add(rec)
        db.session.commit()
        rid, uid = rec.id, user.id
    yield rid
    with app.app_context():
        db.session.rollback()
        ProcessingJob.query.filter_by(recording_id=rid).delete()
        Recording.query.filter_by(id=rid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def _completion(content, finish_reason, completion_tokens=None, reasoning_tokens=None):
    details = SimpleNamespace(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
    usage = SimpleNamespace(completion_tokens=completion_tokens, completion_tokens_details=details) \
        if completion_tokens is not None else None
    message = SimpleNamespace(content=content, reasoning=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=usage)


@contextmanager
def _llm(completion):
    with patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion", return_value=completion), \
         patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        yield emit


def _run(rid):
    with app.app_context():
        proc.generate_summary_only_task(app.app_context(), rid)


def test_a_length_cut_reply_fails_the_summary_with_the_token_advice(rec_id, caplog):
    caplog.set_level(logging.INFO)
    with _llm(_completion(None, "length", 3000, 2990)) as emit:
        with pytest.raises(SummaryStepFailed) as exc:
            _run(rec_id)
    emit.assert_not_called()  # the caller reports the final outcome (#583)
    reason = str(exc.value)
    for part in ("finish_reason=length", "completion_tokens=3000", "reasoning_tokens=2990",
                 "SUMMARY_MAX_TOKENS", "TEXT_MODEL_EXTRA_BODY"):
        assert part in reason
    assert is_permanent_error(reason) is False  # retried like a transient failure
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert rec.status == "FAILED"
        assert rec.transcription == TRANSCRIPT
        assert rec.summary.startswith("[Summary generation failed: ")
        assert _locale("en")["server"]["summaryError"]["emptyReplyLength"] in rec.summary
        assert "finish_reason=length" in rec.summary
        assert rec.error_message == rec.summary
    logged = [r.getMessage() for r in caplog.records]
    assert any("finish_reason=length" in m and "reasoning_tokens=2990" in m and r.levelno >= logging.ERROR
               for m, r in zip(logged, caplog.records))


@pytest.mark.parametrize("content", [None, "", "<think>only reasoning</think>"])
def test_an_empty_reply_fails_the_summary(rec_id, content):
    with _llm(_completion(content, "stop", 12)):
        with pytest.raises(SummaryStepFailed) as exc:
            _run(rec_id)
    assert "finish_reason=stop" in str(exc.value)
    assert "SUMMARY_MAX_TOKENS" not in str(exc.value)
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert rec.status == "FAILED"
        assert _locale("en")["server"]["summaryError"]["emptyReply"] in rec.summary
        assert rec.summary != "[Summary not generated]"


def test_the_message_follows_the_owners_language(rec_id):
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        db.session.get(User, rec.user_id).ui_language = "de"
        db.session.commit()
    with _llm(_completion("", "length", 3000)):
        with pytest.raises(SummaryStepFailed):
            _run(rec_id)
    with app.app_context():
        summary = db.session.get(Recording, rec_id).summary
    assert _locale("de")["server"]["summaryError"]["emptyReplyLength"] in summary


def test_every_locale_has_the_messages():
    for lang in ("en", "de", "es", "fr", "pt-BR", "ru", "zh"):
        messages = _locale(lang)["server"]["summaryError"]
        assert messages["emptyReply"].strip() and messages["emptyReplyLength"].strip(), lang
        assert "SUMMARY_MAX_TOKENS" in messages["emptyReplyLength"], lang
        assert "TEXT_MODEL_EXTRA_BODY" in messages["emptyReplyLength"], lang


def test_a_summary_logs_its_finish_reason(rec_id, caplog):
    caplog.set_level(logging.INFO)
    with _llm(_completion("Minutes of the meeting.", "stop", 40)) as emit:
        _run(rec_id)
    assert any("finish_reason=stop" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert (rec.status, rec.summary) == ("COMPLETED", "Minutes of the meeting.")
    assert [c.kwargs["event_type"] for c in emit.call_args_list] == ["recording.summary.completed"]


def test_a_summary_job_with_an_empty_reply_is_retried_then_fails(rec_id):
    from src.services.job_queue import job_queue, MAX_RETRIES, SUMMARY_JOBS
    job_queue.init_app(app)
    with app.app_context():
        uid = db.session.get(Recording, rec_id).user_id
        jid = job_queue.enqueue(uid, rec_id, "reprocess_summary", params={"user_id": uid})

    def run_once():
        with app.app_context():
            job = job_queue._claim_next_job(SUMMARY_JOBS, "summary")
            assert job is not None and job.id == jid
            with _llm(_completion(None, "length", 3000, 2990)) as emit, \
                    patch.object(job_queue, "_emit_failure_email"):
                job_queue._process_job(job)
            return [c.kwargs["event_type"] for c in emit.call_args_list]

    assert run_once() == []
    with app.app_context():
        job = db.session.get(ProcessingJob, jid)
        assert (job.status, job.retry_count) == ("queued", 1)
        assert "finish_reason=length" in job.error_message
        db.session.get(ProcessingJob, jid).retry_count = MAX_RETRIES - 1
        db.session.commit()

    assert run_once() == ["recording.summary.failed"]
    with app.app_context():
        job = db.session.get(ProcessingJob, jid)
        assert job.status == "failed"
        rec = db.session.get(Recording, rec_id)
        assert rec.status == "FAILED"
        assert rec.transcription == TRANSCRIPT
        assert "TEXT_MODEL_EXTRA_BODY" in rec.summary
