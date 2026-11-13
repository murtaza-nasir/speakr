"""A recording deleted during its ASR call (#531).

The post-ASR steps used to flush the deleted recording, which raised
StaleDataError and then PendingRollbackError in the error handler, with two
tracebacks before the runner dropped the job. The worker now stops with
RecordingDeletedDuringJob and logs no error; the runner drops the job.

SHARED-DB: each test uses its own user and removes it.
"""
import logging
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
def rec_id():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"del_{s}", email=f"del_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, original_filename="d.mp3", audio_path="local://d.mp3",
                        status="PENDING", title="del")
        db.session.add(rec)
        db.session.commit()
        rid, uid = rec.id, user.id
    yield rid
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(id=rid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def _connector(transcribe):
    connector = MagicMock()
    connector.PROVIDER_NAME = "test-connector"
    connector.supports_diarization = False
    connector.default_diarize = False
    specs = MagicMock()
    specs.max_duration_seconds = 1800
    specs.handles_chunking_internally = False
    specs.recommended_chunk_seconds = 600
    specs.unsupported_codecs = []
    specs.preserve_original_audio = False
    connector.specifications = specs
    connector.transcribe.side_effect = transcribe
    return connector


def _response():
    response = MagicMock()
    response.text = "Some words were said."
    response.segments = None
    response.speakers = None
    response.speaker_embeddings = None
    response.language = "en"
    response.has_diarization.return_value = False
    return response


def _run(rid, connector):
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"del_{rid}.mp3")
    with open(path, "wb") as f:
        f.write(b"\x00" * 1024)
    conv = MagicMock()
    conv.was_converted = False
    try:
        with patch("src.services.transcription.get_connector", return_value=connector), \
             patch.object(proc, "is_video_file", return_value=False), \
             patch.object(proc, "convert_if_needed", return_value=conv), \
             patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
             patch.object(proc, "generate_title_task"), \
             patch.object(proc, "generate_summary_only_task"), \
             patch.object(proc, "chunking_service") as chunking:
            chunking.needs_chunking.return_value = False
            chunking.get_audio_duration.return_value = 60.0
            with app.app_context():
                proc.transcribe_with_connector(app.app_context(), rid, path, "d.mp3", time.time(),
                                               mime_type="audio/mpeg")
    finally:
        if os.path.exists(path):
            os.remove(path)


def _delete_elsewhere(rid):
    """Delete the row on another connection, as a concurrent DELETE would."""
    with db.engine.begin() as conn:
        conn.execute(Recording.__table__.delete().where(Recording.__table__.c.id == rid))


def test_a_recording_deleted_during_the_asr_call_stops_without_an_error(rec_id, caplog):
    def transcribe(request):
        _delete_elsewhere(rec_id)
        return _response()

    caplog.set_level(logging.INFO)
    with patch("src.services.voice_profiles.forget_recording_samples") as forget:
        with pytest.raises(proc.RecordingDeletedDuringJob):
            _run(rec_id, _connector(transcribe))
    forget.assert_not_called()
    errors = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert errors == [], [r.getMessage() for r in errors]
    assert any("was deleted during transcription" in r.getMessage() for r in caplog.records)
    with app.app_context():
        assert db.session.get(Recording, rec_id) is None


def test_a_live_recording_still_resets_its_voice_samples(rec_id):
    with patch("src.services.voice_profiles.forget_recording_samples") as forget:
        _run(rec_id, _connector(lambda request: _response()))
    forget.assert_called_once()
    with app.app_context():
        assert db.session.get(Recording, rec_id).transcription == "Some words were said."


# --- a deleted recording whose id a new upload received (#597) ----------------

def _delete_and_reuse_elsewhere(rid):
    """Delete the row and insert a later upload under the same id, as SQLite
    does when the deleted row held the highest id."""
    from datetime import datetime, timedelta
    with db.engine.begin() as conn:
        user_id = conn.execute(db.text("SELECT user_id FROM recording WHERE id = :r"), {"r": rid}).scalar()
        conn.execute(Recording.__table__.delete().where(Recording.__table__.c.id == rid))
        conn.execute(Recording.__table__.insert().values(
            id=rid, user_id=user_id, title="New upload", status="QUEUED",
            transcription="new upload", original_filename="new.mp3",
            created_at=datetime.utcnow() + timedelta(seconds=5)))


def _empty_response():
    response = _response()
    response.text = ""
    return response


def test_a_reused_id_keeps_the_new_upload(rec_id):
    def transcribe(request):
        _delete_and_reuse_elsewhere(rec_id)
        return _response()

    with pytest.raises(proc.RecordingDeletedDuringJob):
        _run(rec_id, _connector(transcribe))
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert (rec.title, rec.status, rec.transcription) == ("New upload", "QUEUED", "new upload")
        assert rec.error_message is None


def test_an_empty_result_does_not_fail_the_reused_upload(rec_id):
    def transcribe(request):
        _delete_and_reuse_elsewhere(rec_id)
        return _empty_response()

    with pytest.raises(proc.RecordingDeletedDuringJob):
        _run(rec_id, _connector(transcribe))
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert rec.status == "QUEUED"
        assert rec.transcription == "new upload"
        assert rec.error_message != proc.EMPTY_TRANSCRIPT_MESSAGE


def test_an_empty_result_still_fails_the_recording_it_belongs_to(rec_id):
    _run(rec_id, _connector(lambda request: _empty_response()))
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert rec.status == "FAILED"
        assert rec.error_message == proc.EMPTY_TRANSCRIPT_MESSAGE


def test_a_summary_for_a_reused_id_is_not_stored_on_the_new_upload(rec_id):
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        rec.transcription = "A transcript long enough to summarise."
        db.session.commit()

    def llm(**kwargs):
        _delete_and_reuse_elsewhere(rec_id)
        return MagicMock(choices=[MagicMock(message=MagicMock(content="Old summary", reasoning=None))])

    with patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion", side_effect=llm), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        with app.app_context():
            with pytest.raises(proc.RecordingDeletedDuringJob):
                proc.generate_summary_only_task(app.app_context(), rec_id)
    emit.assert_not_called()
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert (rec.status, rec.summary, rec.error_message) == ("QUEUED", None, None)


def test_a_title_for_a_reused_id_is_not_stored_on_the_new_upload(rec_id):
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        rec.transcription = "A transcript long enough to title."
        rec.title_source = "auto"
        db.session.commit()

    def compute(recording):
        _delete_and_reuse_elsewhere(rec_id)
        return "Old title"

    with patch("src.services.titling.compute_title", side_effect=compute), \
         patch.object(proc, "finish_processing") as finish:
        with app.app_context():
            with pytest.raises(proc.RecordingDeletedDuringJob):
                proc.generate_title_task(app.app_context(), rec_id)
    finish.assert_not_called()
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert (rec.title, rec.status) == ("New upload", "QUEUED")
