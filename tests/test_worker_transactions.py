"""Worker transactions and empty transcripts (#405, #406).

#405: the transcription worker must not hold a database transaction open
while it waits on the ASR service or the LLM; PostgreSQL's
idle_in_transaction_session_timeout would drop the connection on long jobs.

#406: a transcription that comes back empty is a failure, never COMPLETED.

SHARED-DB: every test uses its own user and removes its recordings.
"""
import json
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
from src.services.transcription.connectors.mossland import MosslandTranscriptionConnector as MosslandConnector
from src.services.transcription.exceptions import TranscriptionError

_created = []


@pytest.fixture
def ctx():
    with app.app_context():
        yield
        db.session.rollback()
        for rid in _created:
            Recording.query.filter_by(id=rid).delete()
        db.session.commit()
        _created.clear()


def _recording():
    s = uuid.uuid4().hex[:8]
    user = User(username=f"wt_{s}", email=f"wt_{s}@local.test", password="x")
    db.session.add(user)
    db.session.commit()
    rec = Recording(user_id=user.id, original_filename="w.mp3", audio_path="local://w.mp3",
                    status="PENDING", title="wt")
    db.session.add(rec)
    db.session.commit()
    _created.append(rec.id)
    return rec


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
    connector.specifications = specs
    connector.transcribe.side_effect = transcribe
    return connector


def _response(text, segments=None):
    response = MagicMock()
    response.text = text
    response.segments = segments
    response.speakers = None
    response.speaker_embeddings = None
    response.language = "en"
    response.has_diarization.return_value = bool(segments)
    return response


def _run(rid, connector):
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"wt_{rid}.mp3")
    with open(path, "wb") as f:
        f.write(b"\x00" * 1024)
    conv = MagicMock()
    conv.was_converted = False
    with patch("src.services.transcription.get_connector", return_value=connector), \
         patch.object(proc, "is_video_file", return_value=False), \
         patch.object(proc, "convert_if_needed", return_value=conv), \
         patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
         patch.object(proc, "generate_title_task") as title, \
         patch.object(proc, "generate_summary_only_task") as summary, \
         patch.object(proc, "chunking_service") as chunking:
        chunking.needs_chunking.return_value = False
        chunking.get_audio_duration.return_value = 60.0
        proc.transcribe_with_connector(app.app_context(), rid, path, "w.mp3", time.time(),
                                       mime_type="audio/mpeg")
    return title, summary


# ------------------------------------------------------------ #405

def test_no_transaction_is_open_during_the_asr_call(ctx):
    rec = _recording()
    seen = {}

    def transcribe(request):
        seen["in_transaction"] = db.session().in_transaction()
        return _response("Some words were said.")

    _run(rec.id, _connector(transcribe))
    assert seen == {"in_transaction": False}
    db.session.expire_all()
    out = db.session.get(Recording, rec.id)
    assert out.transcription == "Some words were said."
    assert out.transcription_duration_seconds is not None


def test_no_transaction_is_open_during_the_summary_llm_call(ctx):
    rec = _recording()
    rec.transcription = "word " * 60
    db.session.commit()
    seen = {}

    def fake_llm(*args, **kwargs):
        seen["in_transaction"] = db.session().in_transaction()
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = "A summary."
        completion.choices[0].message.reasoning = None
        return completion

    with patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion", side_effect=fake_llm), \
         patch.object(proc, "ENABLE_INQUIRE_MODE", False):
        proc.generate_summary_only_task(app.app_context(), rec.id)
    assert seen.get("in_transaction") is False
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).summary


def test_ending_the_transaction_keeps_loaded_values_and_later_writes(ctx):
    rec = _recording()
    rec.title = "before"
    proc._end_transaction_before_external_call(rec)
    assert not db.session().in_transaction()
    assert rec.title == "before"
    rec.title = "after"
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).title == "after"


# ------------------------------------------------------------ #406

@pytest.mark.parametrize("text,segments", [("", None), ("   ", None), (None, None)])
def test_an_empty_transcript_fails_the_recording(ctx, text, segments):
    rec = _recording()
    title, summary = _run(rec.id, _connector(lambda request: _response(text, segments)))
    db.session.expire_all()
    out = db.session.get(Recording, rec.id)
    assert out.status == "FAILED"
    assert out.error_message == proc.EMPTY_TRANSCRIPT_MESSAGE
    assert not out.transcription
    title.assert_not_called()
    summary.assert_not_called()


def test_a_real_transcript_still_completes_the_pipeline(ctx):
    rec = _recording()
    title, summary = _run(rec.id, _connector(lambda request: _response("Hello there.")))
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).status != "FAILED"
    title.assert_called_once()


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("  \n", False), ("hello", True),
    (json.dumps([{"speaker": "A", "sentence": ""}]), False),
    (json.dumps([{"speaker": "A", "sentence": " hi "}]), True),
    ("[not json", True),
])
def test_has_transcript_content(value, expected):
    assert proc._has_transcript_content(value) is expected


def test_mossland_task_that_finishes_empty_is_an_error():
    connector = MosslandConnector.__new__(MosslandConnector)
    connector.poll_timeout = 5
    connector.poll_interval = 0
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"status": "SUCCESS", "text": "", "segments": []}
    client.get.return_value = response
    with pytest.raises(TranscriptionError, match="without any transcript"):
        connector._poll_task(client, "task-1", "model")

    response.json.return_value = {"status": "COMPLETED", "text": "",
                                  "segments": [{"text": "Hello", "start": 0, "end": 1}]}
    result = connector._poll_task(client, "task-2", "model")
    assert result.text == "Hello"
