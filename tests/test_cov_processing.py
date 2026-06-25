"""Coverage-focused tests for src/tasks/processing.py.

These exercise the async pipeline task functions directly (not via HTTP):
title generation, summary generation, event extraction, the Inquire
embeddings hook, status transitions, duration bookkeeping, and the
transcribe-with-connector orchestration (including the video retention /
passthrough branches) — all with the network, LLM client, ASR connector,
storage, ffprobe/ffmpeg, and embeddings mocked at the processing.py import
site so the suite stays fully offline and hermetic.

SHARED-DB NOTE: the session DB is shared across the whole test suite, so
every assertion here is scoped to recording/user IDs this file created.
"""
import os
import sys
import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.app import app, db
from src.models import User, Recording, Event, SystemSetting
import src.tasks.processing as proc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(prefix, **kwargs):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="placeholder-bcrypt-hash",
        **kwargs,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_recording(user_id, transcription="word " * 50, status="PENDING", **kwargs):
    kwargs.setdefault("title", "cov-processing")
    recording = Recording(
        user_id=user_id,
        original_filename="cov.mp3",
        audio_path="local://cov.mp3",
        status=status,
        transcription=transcription,
        **kwargs,
    )
    db.session.add(recording)
    db.session.commit()
    return recording


def _fake_completion(content, reasoning=None):
    """Build a stand-in for an OpenAI ChatCompletion response."""
    msg = MagicMock()
    msg.content = content
    msg.reasoning = reasoning
    choice = MagicMock()
    choice.message = msg
    completion = MagicMock()
    completion.choices = [choice]
    return completion


@contextmanager
def _patch_llm(content="Generated text", reasoning=None, side_effect=None):
    """Patch call_llm_completion and a truthy client at the processing import site."""
    with patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion") as mock_call:
        if side_effect is not None:
            mock_call.side_effect = side_effect
        else:
            mock_call.return_value = _fake_completion(content, reasoning)
        yield mock_call


# ---------------------------------------------------------------------------
# generate_summary_only_task
# ---------------------------------------------------------------------------

def test_summary_success_sets_summary_and_completed():
    with app.app_context():
        user = _make_user("sum_ok")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content="## Minutes\nThis is the summary."), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_summary_only_task(app.app_context(), rid)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.status == "COMPLETED"
        assert "summary" in out.summary.lower()
        assert out.completed_at is not None
        assert out.summarization_duration_seconds is not None


def test_summary_client_not_configured_skips():
    with app.app_context():
        user = _make_user("sum_noclient")
        rec = _make_recording(user.id, status="PENDING")
        rid = rec.id
        with patch.object(proc, "client", None):
            proc.generate_summary_only_task(app.app_context(), rid)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert "OpenRouter client not configured" in out.summary


def test_summary_short_transcription_skipped():
    with app.app_context():
        user = _make_user("sum_short")
        rec = _make_recording(user.id, transcription="hi")
        rid = rec.id
        with _patch_llm(content="should not be used"):
            proc.generate_summary_only_task(app.app_context(), rid)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.status == "COMPLETED"
        assert "short transcription" in out.summary


def test_summary_error_path_sets_failed():
    with app.app_context():
        user = _make_user("sum_err")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(side_effect=RuntimeError("LLM boom")):
            proc.generate_summary_only_task(app.app_context(), rid)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.status == "FAILED"
        assert out.summary  # error message stored as summary


def test_summary_empty_response_marks_not_generated():
    with app.app_context():
        user = _make_user("sum_empty")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content=""), patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_summary_only_task(app.app_context(), rid)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.status == "COMPLETED"
        assert out.summary == "[Summary not generated]"


def test_summary_inquire_mode_calls_process_chunks():
    """Bug #305: the Inquire embeddings hook must run after summary completion
    when ENABLE_INQUIRE_MODE is on."""
    with app.app_context():
        user = _make_user("sum_inquire")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content="A real summary body."), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", True), \
             patch.object(proc, "process_recording_chunks") as mock_chunks:
            proc.generate_summary_only_task(app.app_context(), rid)

        mock_chunks.assert_called_once_with(rid)


def test_summary_inquire_chunk_error_is_swallowed():
    with app.app_context():
        user = _make_user("sum_inq_err")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content="A real summary body."), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", True), \
             patch.object(proc, "process_recording_chunks",
                          side_effect=RuntimeError("embed fail")):
            # Must not raise despite the chunk error.
            proc.generate_summary_only_task(app.app_context(), rid)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.status == "COMPLETED"


def test_summary_custom_prompt_override_replaces():
    with app.app_context():
        user = _make_user("sum_override")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content="Overridden summary") as mock_call, \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_summary_only_task(
                app.app_context(), rid,
                custom_prompt_override="ONLY follow these instructions",
            )
        # The custom override text must appear in the user prompt sent to the LLM.
        sent = mock_call.call_args.kwargs["messages"][1]["content"]
        assert "ONLY follow these instructions" in sent


def test_summary_custom_prompt_append():
    with app.app_context():
        user = _make_user("sum_append")
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content="Appended summary") as mock_call, \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_summary_only_task(
                app.app_context(), rid,
                custom_prompt_override="EXTRA agenda context",
                custom_prompt_append=True,
            )
        sent = mock_call.call_args.kwargs["messages"][1]["content"]
        assert "Additional context for this recording" in sent
        assert "EXTRA agenda context" in sent


def test_summary_event_extraction_invoked_when_user_extract_events():
    with app.app_context():
        user = _make_user("sum_events", extract_events=True)
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content="A summary body."), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
             patch.object(proc, "extract_events_from_transcript") as mock_extract:
            proc.generate_summary_only_task(app.app_context(), rid)
        mock_extract.assert_called_once()
        assert mock_extract.call_args.args[0] == rid


def test_summary_missing_recording_returns_quietly():
    with app.app_context():
        # An ID that does not exist must not raise.
        with _patch_llm(content="should never run") as mock_call:
            ret = proc.generate_summary_only_task(app.app_context(), 99999999)
        assert ret is None
        # Missing record -> must bail out before doing any LLM work.
        mock_call.assert_not_called()
        assert db.session.get(Recording, 99999999) is None


def test_summary_user_id_viewer_filtering():
    """Passing an explicit user_id drives the tag-visibility viewer path."""
    with app.app_context():
        owner = _make_user("sum_owner")
        viewer = _make_user("sum_viewer")
        rec = _make_recording(owner.id)
        rid = rec.id
        with _patch_llm(content="Viewer summary"), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_summary_only_task(app.app_context(), rid, user_id=viewer.id)
        db.session.expire_all()
        assert db.session.get(Recording, rid).status == "COMPLETED"


# ---------------------------------------------------------------------------
# generate_title_task
# ---------------------------------------------------------------------------

def test_title_sets_title_and_completes():
    with app.app_context():
        user = _make_user("title_ok")
        # Placeholder title so AI title generation kicks in.
        rec = _make_recording(user.id, title="Recording - cov.mp3")
        rid = rec.id
        with _patch_llm(content="Quarterly Budget Review"), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=False)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.title == "Quarterly Budget Review"
        assert out.status == "COMPLETED"
        assert out.completed_at is not None


def test_title_will_auto_summarize_leaves_status_unchanged():
    with app.app_context():
        user = _make_user("title_autosum")
        rec = _make_recording(user.id, title="Recording - cov.mp3", status="PROCESSING")
        rid = rec.id
        with _patch_llm(content="A New Title"), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=True)

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.title == "A New Title"
        # Status must NOT have flipped to COMPLETED (summary task will do that).
        assert out.status == "PROCESSING"


def test_title_inquire_mode_calls_process_chunks():
    with app.app_context():
        user = _make_user("title_inquire")
        rec = _make_recording(user.id, title="Recording - cov.mp3")
        rid = rec.id
        with _patch_llm(content="Inquire Title"), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", True), \
             patch.object(proc, "process_recording_chunks") as mock_chunks:
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=False)
        mock_chunks.assert_called_once_with(rid)


def test_title_user_provided_title_is_kept():
    with app.app_context():
        user = _make_user("title_userset")
        rec = _make_recording(user.id, title="My Important Meeting")
        rid = rec.id
        with _patch_llm(content="AI Suggested") as mock_call, \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=False)
        # User title preserved; no LLM call should have been made.
        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.title == "My Important Meeting"
        assert out.status == "COMPLETED"
        mock_call.assert_not_called()


def test_title_short_transcription_falls_back_to_filename():
    with app.app_context():
        user = _make_user("title_short")
        rec = _make_recording(user.id, title="Recording - cov.mp3", transcription="hi")
        rid = rec.id
        with patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=False)
        db.session.expire_all()
        out = db.session.get(Recording, rid)
        # Falls back to the filename without extension.
        assert out.title == "cov"
        assert out.status == "COMPLETED"


def test_title_client_not_configured_falls_back_to_filename():
    with app.app_context():
        user = _make_user("title_noclient")
        rec = _make_recording(user.id, title="Recording - cov.mp3")
        rid = rec.id
        with patch.object(proc, "client", None), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=False)
        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.title == "cov"


def test_title_missing_recording_returns_quietly():
    with app.app_context():
        with _patch_llm(content="should never run") as mock_call:
            ret = proc.generate_title_task(app.app_context(), 99999999)
        assert ret is None
        # Missing record -> must bail out before doing any LLM work.
        mock_call.assert_not_called()
        assert db.session.get(Recording, 99999999) is None


def test_title_budget_exceeded_skips_gracefully():
    from src.services.llm import TokenBudgetExceeded
    with app.app_context():
        user = _make_user("title_budget")
        rec = _make_recording(user.id, title="Recording - cov.mp3")
        rid = rec.id
        with _patch_llm(side_effect=TokenBudgetExceeded("over budget")), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), rid, will_auto_summarize=False)
        db.session.expire_all()
        out = db.session.get(Recording, rid)
        # Budget skip → falls back to filename, still completes.
        assert out.status == "COMPLETED"
        assert out.title == "cov"


# ---------------------------------------------------------------------------
# extract_events_from_transcript
# ---------------------------------------------------------------------------

def test_extract_events_creates_events():
    with app.app_context():
        user = _make_user("ev_create", extract_events=True)
        rec = _make_recording(user.id)
        rid = rec.id
        events_json = (
            '{"events": [{"title": "Review Meeting", '
            '"description": "Quarterly review", '
            '"start_datetime": "2025-07-22T14:00:00", '
            '"end_datetime": "2025-07-22T15:30:00", '
            '"location": "Room A", "attendees": ["Jane"], '
            '"reminder_minutes": 30}]}'
        )
        with _patch_llm(content=events_json):
            proc.extract_events_from_transcript(rid, "transcript text", "summary text")

        events = Event.query.filter_by(recording_id=rid).all()
        assert len(events) == 1
        assert events[0].title == "Review Meeting"
        assert events[0].reminder_minutes == 30


def test_extract_events_empty_list():
    with app.app_context():
        user = _make_user("ev_empty", extract_events=True)
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content='{"events": []}'):
            proc.extract_events_from_transcript(rid, "transcript", "summary")
        assert Event.query.filter_by(recording_id=rid).count() == 0


def test_extract_events_disabled_user_no_op():
    with app.app_context():
        user = _make_user("ev_disabled", extract_events=False)
        rec = _make_recording(user.id)
        rid = rec.id
        with _patch_llm(content='{"events": [{"title": "X", "start_datetime": "2025-01-01T09:00:00"}]}') as mock_call:
            proc.extract_events_from_transcript(rid, "transcript", "summary")
        # Returns before calling the LLM.
        mock_call.assert_not_called()
        assert Event.query.filter_by(recording_id=rid).count() == 0


def test_extract_events_bad_date_skipped():
    with app.app_context():
        user = _make_user("ev_baddate", extract_events=True)
        rec = _make_recording(user.id)
        rid = rec.id
        bad = '{"events": [{"title": "Bad", "start_datetime": "not-a-date"}]}'
        with _patch_llm(content=bad):
            proc.extract_events_from_transcript(rid, "transcript", "summary")
        # Unparseable start date → event skipped.
        assert Event.query.filter_by(recording_id=rid).count() == 0


# ---------------------------------------------------------------------------
# format helpers
# ---------------------------------------------------------------------------

def test_format_transcription_for_llm_json_segments():
    out = proc.format_transcription_for_llm(
        '[{"speaker": "SPEAKER_00", "sentence": "Hello"}, '
        '{"speaker": "SPEAKER_01", "sentence": "Hi"}]'
    )
    assert "[SPEAKER_00]: Hello" in out
    assert "[SPEAKER_01]: Hi" in out


def test_format_transcription_for_llm_plain_passthrough():
    assert proc.format_transcription_for_llm("just plain text") == "just plain text"


def test_clean_llm_response_strips_think_tags():
    out = proc.clean_llm_response("<think>reasoning here</think>Final answer")
    assert out == "Final answer"


def test_clean_llm_response_empty():
    assert proc.clean_llm_response("") == ""


def test_resolve_hotwords():
    assert proc.resolve_hotwords("explicit", "default") == "explicit"
    assert proc.resolve_hotwords("", "admin_default") == "admin_default"
    assert proc.resolve_hotwords(None, None) is None


def test_sanitize_error_message_redacts_and_caps():
    msg = proc._sanitize_error_message("failed at /tmp/secret/file.mp3   with   spaces")
    assert "/tmp/<path>" in msg
    assert "  " not in msg
    long = proc._sanitize_error_message("x" * 1000)
    assert len(long) <= proc._ERROR_MESSAGE_MAX_CHARS


# ---------------------------------------------------------------------------
# transcribe_with_connector — orchestration with mocked connector/probe/storage
# ---------------------------------------------------------------------------

def _make_connector(text="Hello world transcript", segments=None, speakers=None):
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
    response = MagicMock()
    response.text = text
    response.segments = segments
    response.speakers = speakers
    response.speaker_embeddings = None
    response.has_diarization.return_value = bool(segments)
    connector.transcribe.return_value = response
    return connector


def test_transcribe_with_connector_audio_success():
    import time as _time
    with app.app_context():
        user = _make_user("trans_audio")
        rec = _make_recording(user.id, transcription=None, status="PENDING")
        rid = rec.id

        connector = _make_connector(text="A transcribed body of text here.")

        # Provide a real audio file on disk for open()/getsize.
        tmp_path = os.path.join(app.config["UPLOAD_FOLDER"], f"trans_{rid}.mp3")
        with open(tmp_path, "wb") as f:
            f.write(b"\x00" * 1024)

        conv_result = MagicMock()
        conv_result.was_converted = False

        with patch("src.services.transcription.get_connector", return_value=connector), \
             patch.object(proc, "is_video_file", return_value=False), \
             patch.object(proc, "convert_if_needed", return_value=conv_result), \
             patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
             patch.object(proc, "generate_title_task") as mock_title, \
             patch.object(proc, "generate_summary_only_task") as mock_summary, \
             patch.object(proc, "chunking_service") as mock_chunk_svc:
            mock_chunk_svc.needs_chunking.return_value = False
            mock_chunk_svc.get_audio_duration.return_value = 120.0
            proc.transcribe_with_connector(
                app.app_context(), rid, tmp_path, "cov.mp3", _time.time(),
                mime_type="audio/mpeg",
            )

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.transcription == "A transcribed body of text here."
        assert out.transcription_duration_seconds is not None
        # Title + summary tasks were invoked downstream.
        mock_title.assert_called_once()
        mock_summary.assert_called_once()


def test_transcribe_with_connector_budget_exceeded_sets_failed():
    import time as _time
    with app.app_context():
        user = _make_user("trans_budget")
        rec = _make_recording(user.id, transcription=None, status="PENDING")
        rid = rec.id
        connector = _make_connector()

        with patch("src.services.transcription.get_connector", return_value=connector), \
             patch.object(proc.transcription_tracker, "check_budget",
                          return_value=(False, 100, "Budget exceeded for month")):
            proc.transcribe_with_connector(
                app.app_context(), rid, "/nonexistent.mp3", "cov.mp3", _time.time(),
                mime_type="audio/mpeg",
            )

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.status == "FAILED"
        assert "Budget exceeded" in out.error_message


def test_transcribe_with_connector_missing_recording():
    import time as _time
    with app.app_context():
        # Must return quietly, not raise.
        with patch("src.services.transcription.get_connector") as mock_get_conn:
            ret = proc.transcribe_with_connector(
                app.app_context(), 99999999, "/x.mp3", "x.mp3", _time.time(),
            )
        assert ret is None
        # Missing record -> must bail out before resolving any connector.
        mock_get_conn.assert_not_called()
        assert db.session.get(Recording, 99999999) is None


def test_transcribe_with_connector_error_reraises():
    import time as _time
    with app.app_context():
        user = _make_user("trans_err")
        rec = _make_recording(user.id, transcription=None, status="PENDING")
        rid = rec.id
        connector = _make_connector()
        connector.transcribe.side_effect = RuntimeError("connector blew up")

        tmp_path = os.path.join(app.config["UPLOAD_FOLDER"], f"err_{rid}.mp3")
        with open(tmp_path, "wb") as f:
            f.write(b"\x00" * 512)

        conv_result = MagicMock()
        conv_result.was_converted = False

        with patch("src.services.transcription.get_connector", return_value=connector), \
             patch.object(proc, "is_video_file", return_value=False), \
             patch.object(proc, "convert_if_needed", return_value=conv_result), \
             patch.object(proc, "chunking_service") as mock_chunk_svc:
            mock_chunk_svc.needs_chunking.return_value = False
            mock_chunk_svc.get_audio_duration.return_value = 60.0
            with pytest.raises(RuntimeError):
                proc.transcribe_with_connector(
                    app.app_context(), rid, tmp_path, "cov.mp3", _time.time(),
                    mime_type="audio/mpeg",
                )


# ---------------------------------------------------------------------------
# transcribe_audio_task — thin wrapper that sets processing_time
# ---------------------------------------------------------------------------

def test_transcribe_audio_task_sets_processing_time():
    from datetime import datetime, timedelta
    with app.app_context():
        user = _make_user("task_wrap")
        rec = _make_recording(user.id, transcription="word " * 30, status="PENDING")
        rid = rec.id
        start = datetime.utcnow() - timedelta(seconds=5)

        def _fake_transcribe(app_context, recording_id, *a, **k):
            with app_context:
                r = db.session.get(Recording, recording_id)
                r.status = "COMPLETED"
                db.session.commit()

        with patch.object(proc, "transcribe_with_connector", side_effect=_fake_transcribe):
            proc.transcribe_audio_task(
                app.app_context(), rid, "/x.mp3", "x.mp3", start,
            )

        db.session.expire_all()
        out = db.session.get(Recording, rid)
        assert out.processing_time_seconds is not None
        assert out.processing_time_seconds >= 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
