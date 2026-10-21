"""A paid submit-and-poll task is saved on its processing job and resumed on a retry.

AssemblyAI and Tencent charge for each job they accept. A retry of the same
job (after a polling timeout, an error or a restart) polls the saved task; a
new job, such as a reprocess, submits afresh.
"""
import io
import os
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import job_queue
from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.assemblyai import AssemblyAITranscriptionConnector
from src.services.transcription.connectors.tencent_asr import TencentASRConnector
from src.services.transcription.exceptions import ProviderError, TranscriptionError
from src.services.transcription.provider_tasks import provider_task_for
import src.tasks.processing as proc


def _recording():
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"ptask_{suffix}", email=f"ptask_{suffix}@local.test",
                password="placeholder-bcrypt-hash")
    db.session.add(user)
    db.session.commit()
    recording = Recording(user_id=user.id, original_filename="m.webm", audio_path="local://m.webm",
                          status="PROCESSING", title="provider task")
    db.session.add(recording)
    db.session.commit()
    return recording


def _job(recording, status="processing", job_type="transcribe"):
    job = ProcessingJob(user_id=recording.user_id, recording_id=recording.id,
                        job_type=job_type, status=status)
    db.session.add(job)
    db.session.commit()
    return job


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.webm"
    path.write_bytes(b"original bytes")
    return str(path)


# --- saving on the job --------------------------------------------------------

def test_a_retry_of_the_same_job_gets_the_saved_task(audio, tmp_path):
    with app.app_context():
        recording = _recording()
        _job(recording)
        saved, save = provider_task_for(recording.id, "tencent_asr", audio)
        assert saved is None
        save({"task_id": 7})
        db.session.expire_all()
        assert provider_task_for(recording.id, "tencent_asr", audio)[0] == {"task_id": 7}
        # Another service, or other audio bytes, never get it.
        assert provider_task_for(recording.id, "assemblyai", audio)[0] is None
        other = tmp_path / "other.webm"
        other.write_bytes(b"other bytes")
        assert provider_task_for(recording.id, "tencent_asr", str(other))[0] is None
        save(None)
        db.session.expire_all()
        assert provider_task_for(recording.id, "tencent_asr", audio)[0] is None


def test_a_new_job_submits_afresh(audio):
    with app.app_context():
        recording = _recording()
        first = _job(recording)
        provider_task_for(recording.id, "tencent_asr", audio)[1]({"task_id": 7})
        first.status = "completed"
        db.session.commit()
        _job(recording, job_type="reprocess_transcription")
        saved, save = provider_task_for(recording.id, "tencent_asr", audio)
        assert saved is None and save is not None


def test_nothing_is_saved_without_a_running_job(audio):
    with app.app_context():
        recording = _recording()
        assert provider_task_for(recording.id, "tencent_asr", audio) == (None, None)


def test_processing_hands_the_saved_task_to_the_connector(tmp_path):
    with app.app_context():
        recording = _recording()
        rid = recording.id
        _job(recording)
        path = os.path.join(app.config["UPLOAD_FOLDER"], f"ptask_{rid}.webm")
        with open(path, "wb") as f:
            f.write(b"original bytes")
        provider_task_for(rid, "tencent_asr", path)[1]({"task_id": 9, "submitted_at": time.time()})

        connector = MagicMock()
        connector.PROVIDER_NAME = "tencent_asr"
        connector.SUPPORTS_TASK_RESUME = True
        connector.supports_diarization = False
        connector.default_diarize = False
        connector.specifications = MagicMock(max_duration_seconds=None, handles_chunking_internally=True,
                                             unsupported_codecs=[])
        response = MagicMock(text="resumed text", segments=None, speakers=None, speaker_embeddings=None)
        response.has_diarization.return_value = False
        connector.transcribe.return_value = response
        conversion = MagicMock(was_converted=False)
        try:
            with patch("src.services.transcription.get_connector", return_value=connector), \
                 patch.object(proc, "is_video_file", return_value=False), \
                 patch.object(proc, "convert_if_needed", return_value=conversion), \
                 patch.object(proc, "client", MagicMock()), \
                 patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
                 patch.object(proc, "generate_title_task"), \
                 patch.object(proc, "generate_summary_only_task"), \
                 patch.object(proc, "chunking_service") as chunking:
                chunking.needs_chunking.return_value = False
                chunking.get_audio_duration.return_value = 60.0
                proc.transcribe_with_connector(app.app_context(), rid, path, "m.webm", time.time(),
                                               mime_type="audio/webm")
        finally:
            os.remove(path)
        sent = connector.transcribe.call_args.args[0]
        assert sent.provider_task["task_id"] == 9
        assert callable(sent.save_provider_task)


# --- Tencent ------------------------------------------------------------------

def _tencent():
    return TencentASRConnector({"secret_id": "id", "secret_key": "secret", "poll_interval": .001, "timeout": .5})


def _request(saved=None, store=None):
    return TranscriptionRequest(audio_file=io.BytesIO(b"original bytes"), filename="m.webm", diarize=True,
                                provider_task=saved,
                                save_provider_task=store.append if store is not None else None)


def test_tencent_saves_the_task_before_and_after_submission():
    connector, store = _tencent(), []
    connector._call = Mock(return_value={"TaskId": 123})
    connector._poll = Mock(return_value={"Status": 2, "Result": "ok"})
    assert connector.transcribe(_request(store=store)).text == "ok"
    assert store[0] == {"submitting": True, "staging_locator": None}
    assert store[1]["task_id"] == 123 and store[1]["submitted_at"] > 0


def test_tencent_retry_polls_the_saved_task():
    connector = _tencent()
    connector._call = Mock(side_effect=AssertionError("must not submit"))
    connector._poll = Mock(return_value={"Status": 2, "Result": "recovered"})
    saved = {"task_id": 123, "submitted_at": time.time(), "staging_locator": None}
    assert connector.transcribe(_request(saved)).text == "recovered"
    assert connector._poll.call_args.args[1] == 123


def test_tencent_resume_deletes_the_staged_audio(monkeypatch):
    import src.services.storage as storage_module
    storage = Mock()
    monkeypatch.setattr(storage_module, "get_storage_service", lambda: SimpleNamespace(s3=storage))
    connector = _tencent()
    connector._poll = Mock(return_value={"Status": 2, "Result": "ok"})
    saved = {"task_id": 5, "submitted_at": time.time(), "staging_locator": "s3://bucket/tencent-asr/a.webm"}
    connector.transcribe(_request(saved))
    assert storage.delete.call_args.args[0].key == "tencent-asr/a.webm"


def test_tencent_uncertain_submission_stops_retries_until_reprocess():
    connector, store = _tencent(), []
    connector._call = Mock(side_effect=httpx.ReadTimeout("private"))
    with pytest.raises(TranscriptionError, match="not retried"):
        connector.transcribe(_request(store=store))
    assert store == [{"submitting": True, "staging_locator": None}]
    retry = _tencent()
    retry._call = Mock(side_effect=AssertionError("duplicate submission"))
    with pytest.raises(TranscriptionError, match="no clear outcome") as exc:
        retry.transcribe(_request(store[-1]))
    # The job queue does not retry it again; a reprocess (a new job) submits.
    assert job_queue._is_permanent_error(str(exc.value))


def test_tencent_rejection_forgets_the_task():
    connector, store = _tencent(), []
    connector._call = Mock(side_effect=ProviderError("Tencent CreateRecTask: AuthFailure.",
                                                     provider="tencent_asr", status_code=400))
    with pytest.raises(ProviderError):
        connector.transcribe(_request(store=store))
    assert store == [{"submitting": True, "staging_locator": None}, None]


def test_tencent_task_older_than_a_day_is_submitted_again():
    connector = _tencent()
    connector._call = Mock(return_value={"TaskId": 124})
    connector._poll = Mock(return_value={"Status": 2, "Result": "fresh"})
    saved = {"task_id": 123, "submitted_at": 0, "staging_locator": None}
    assert connector.transcribe(_request(saved)).text == "fresh"
    assert connector._poll.call_args.args[1] == 124


# --- AssemblyAI ---------------------------------------------------------------

class _AssemblyAIClient:
    def __init__(self):
        self.posted, self.polled = [], []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, path, content=None, json=None, headers=None):
        self.posted.append(path)
        if path == "/v2/upload":
            return SimpleNamespace(status_code=200, json=lambda: {"upload_url": "https://cdn/x"}, text="")
        return SimpleNamespace(status_code=200, json=lambda: {"id": "tid-1"}, text="")
    def get(self, path):
        self.polled.append(path)
        return SimpleNamespace(status_code=200, json=lambda: {"status": "completed", "text": "hello"}, text="")


def test_assemblyai_saves_the_transcript_and_a_retry_polls_it():
    connector = AssemblyAITranscriptionConnector({"api_key": "k", "poll_interval": 0})
    store, client = [], _AssemblyAIClient()
    with patch.object(connector, "_client", return_value=client):
        assert connector.transcribe(_request(store=store)).text == "hello"
    assert store == [{"transcript_id": "tid-1"}]
    retry_client = _AssemblyAIClient()
    with patch.object(connector, "_client", return_value=retry_client):
        assert connector.transcribe(_request(store[0])).text == "hello"
    assert retry_client.posted == [] and retry_client.polled == ["/v2/transcript/tid-1"]
