"""Every exit of the processing pipeline runs the same finishing step (#412).

finish_processing marks the recording COMPLETED, then applies team-tag
auto-shares, writes the auto-export and builds the Inquire chunks. Before
#412 three exits skipped part of it:
- a user-titled recording with auto-summarization off was never indexed for
  Inquire;
- with auto-summarization on and no LLM client, the recording stayed PROCESSING;
- a too-short transcription was marked COMPLETED without shares, export or chunks.

SHARED-DB: the users and recordings created here are removed afterwards.
"""

import os
import sys
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
import src.tasks.processing as proc


@pytest.fixture
def made():
    created = []
    yield created
    with app.app_context():
        db.session.rollback()
        for model, oid in reversed(created):
            obj = db.session.get(model, oid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _user(made, **kwargs):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"finish_{s}", email=f"finish_{s}@local.test", password="x", **kwargs)
    db.session.add(u)
    db.session.commit()
    made.append((User, u.id))
    return u


def _rec(made, user_id, **kwargs):
    kwargs.setdefault("title", "Recording - fin.mp3")
    kwargs.setdefault("transcription", "word " * 50)
    kwargs.setdefault("status", "PROCESSING")
    r = Recording(user_id=user_id, original_filename="fin.mp3", audio_path="local://fin.mp3", **kwargs)
    db.session.add(r)
    db.session.commit()
    made.append((Recording, r.id))
    return r


def _side_effects():
    return (patch.object(proc, "apply_team_tag_auto_shares"),
            patch.object(proc, "export_recording"),
            patch.object(proc, "process_recording_chunks"),
            patch.object(proc, "ENABLE_AUTO_EXPORT", True),
            patch.object(proc, "ENABLE_INQUIRE_MODE", True))


def _completion(text):
    msg = MagicMock(content=text, reasoning=None)
    return MagicMock(choices=[MagicMock(message=msg)])


def test_user_title_without_summary_is_indexed_for_inquire(made):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id, title="Board meeting")
        shares, export, chunks, *flags = _side_effects()
        with shares as m_shares, export as m_export, chunks as m_chunks, flags[0], flags[1]:
            proc.generate_title_task(app.app_context(), r.id, will_auto_summarize=False)
        db.session.expire_all()
        assert db.session.get(Recording, r.id).status == "COMPLETED"
        m_chunks.assert_called_once_with(r.id)
        m_shares.assert_called_once_with(r.id)
        m_export.assert_called_once_with(r.id)


def test_short_transcription_still_runs_the_finishing_steps(made):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id, transcription="hi")
        shares, export, chunks, *flags = _side_effects()
        with shares as m_shares, export as m_export, chunks as m_chunks, flags[0], flags[1], \
             patch.object(proc, "client", MagicMock()):
            proc.generate_summary_only_task(app.app_context(), r.id)
        db.session.expire_all()
        out = db.session.get(Recording, r.id)
        assert out.status == "COMPLETED"
        assert out.summary == "[Summary skipped due to short transcription]"
        m_shares.assert_called_once_with(r.id)
        m_export.assert_called_once_with(r.id)
        m_chunks.assert_called_once_with(r.id)


def test_normal_summary_runs_each_finishing_step_once(made):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id)
        shares, export, chunks, *flags = _side_effects()
        with shares as m_shares, export as m_export, chunks as m_chunks, flags[0], flags[1], \
             patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "call_llm_completion", return_value=_completion("## Summary\nDone.")):
            proc.generate_summary_only_task(app.app_context(), r.id)
        db.session.expire_all()
        out = db.session.get(Recording, r.id)
        assert out.status == "COMPLETED" and out.summary.startswith("## Summary")
        assert out.summarization_duration_seconds is not None
        m_shares.assert_called_once_with(r.id)
        m_export.assert_called_once_with(r.id)
        m_chunks.assert_called_once_with(r.id)


def test_a_failing_export_leaves_the_recording_completed(made):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id)
        shares, export, chunks, *flags = _side_effects()
        with shares, export as m_export, chunks as m_chunks, flags[0], flags[1]:
            m_export.side_effect = RuntimeError("disk full")
            proc.finish_processing(r.id)
        db.session.expire_all()
        assert db.session.get(Recording, r.id).status == "COMPLETED"
        m_chunks.assert_called_once_with(r.id)


def _make_connector(text):
    connector = MagicMock()
    connector.PROVIDER_NAME = "test-connector"
    connector.supports_diarization = False
    connector.default_diarize = False
    specs = MagicMock(max_duration_seconds=1800, handles_chunking_internally=False,
                      recommended_chunk_seconds=600, unsupported_codecs=[])
    connector.specifications = specs
    response = MagicMock(text=text, segments=None, speakers=None, speaker_embeddings=None)
    response.has_diarization.return_value = False
    connector.transcribe.return_value = response
    return connector


def test_without_an_llm_client_the_recording_is_completed_not_stuck(made):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id, transcription=None, status="PROCESSING")
        path = os.path.join(app.config["UPLOAD_FOLDER"], f"finish_{r.id}.mp3")
        with open(path, "wb") as f:
            f.write(b"\x00" * 1024)
        conv = MagicMock(was_converted=False)
        shares, export, chunks, *flags = _side_effects()
        try:
            with patch("src.services.transcription.get_connector", return_value=_make_connector("A full transcript of the meeting.")), \
                 patch.object(proc, "is_video_file", return_value=False), \
                 patch.object(proc, "convert_if_needed", return_value=conv), \
                 patch.object(proc, "client", None), \
                 patch.object(proc, "chunking_service") as svc, \
                 shares, export, chunks, flags[0], flags[1]:
                svc.needs_chunking.return_value = False
                svc.get_audio_duration.return_value = 60.0
                proc.transcribe_with_connector(app.app_context(), r.id, path, "fin.mp3", time.time(),
                                               mime_type="audio/mpeg")
        finally:
            if os.path.exists(path):
                os.remove(path)
        db.session.expire_all()
        out = db.session.get(Recording, r.id)
        assert out.transcription == "A full transcript of the meeting."
        assert out.status == "COMPLETED"
