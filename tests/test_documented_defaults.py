"""Speakr with the documented WhisperX settings, against the real service rules.

Issue #409 reached users because every test ran with a fully configured
environment. These tests start from what the docs tell a new user to do:
Speakr configured only from config/env.whisperx.example, with no admin
settings (no default transcription model, no model list), talking over HTTP
to a stand-in for whisperx-asr-service that answers with the service's own
model rules (tests/contract/whisperx_model_rules.py, kept in step with the
service by tests/contract/test_whisperx_contract.py).

Every path that sends audio to the service runs for real: the voice
embedding check (startup, Check now, re-baseline), upload, reprocess, bulk
reprocess, API v1 upload and transcribe, incognito and joining two uploads.

The matrix, and the outcome expected in each cell:

    service                         Speakr admin default   outcome
    current, PRELOAD_MODEL unset    none                   works (service default large-v3)
    current, PRELOAD_MODEL unset    large-v3               works (model named)
    0.4.1, PRELOAD_MODEL empty      none                   fails, clearly (#409 on old
                                                           services; recordings FAILED,
                                                           the check logs the hint)
    0.4.1, PRELOAD_MODEL empty      large-v3               works (model named)
    0.4.1, PRELOAD_MODEL=large-v3   none                   works

The voice embedding check also runs with speaker embeddings off, where it
must send nothing at all.

SHARED-DB: users, recordings, jobs and settings are removed afterwards.
"""

import io
import logging
import math
import os
import struct
import sys
import uuid
import wave
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g
from flask.testing import FlaskClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.app import app, db
from src.models import ProcessingJob, Recording, SystemSetting, User
from src.services import voice_embedding_check as vec
from src.services.job_queue import TRANSCRIPTION_JOBS, job_queue
from src.services.transcription import get_registry
from tests.contract.fake_asr_server import FakeASRServer
from tests.contract.whisperx_model_rules import ServiceModelRules

app.config["WTF_CSRF_ENABLED"] = False

EXAMPLE = os.path.join(ROOT, "config", "env.whisperx.example")
MODEL_SETTINGS = ("transcription_default_model", "transcription_models_visible_json")

# Settings in the example that decide what reaches the ASR service. Anything
# else the example adds under these prefixes must be handled here too (the
# guard test below fails otherwise), so the example and these tests cannot
# drift apart.
ASR_KEYS = ("ASR_BASE_URL", "ASR_DIARIZE", "ASR_RETURN_SPEAKER_EMBEDDINGS")
ASR_PREFIXES = ("ASR_", "TRANSCRIPTION_", "WHISPER", "USE_ASR")

SERVICES = {
    # label: (generation, service environment as compose delivers it)
    "current, PRELOAD_MODEL unset": ("fixed", {"PRELOAD_MODEL": ""}),
    "0.4.1, PRELOAD_MODEL empty": ("legacy", {"PRELOAD_MODEL": ""}),
    "0.4.1, PRELOAD_MODEL=large-v3": ("legacy", {"PRELOAD_MODEL": "large-v3"}),
}


def expected_to_work(service, admin_default):
    generation, env = SERVICES[service]
    return bool(admin_default) or bool(ServiceModelRules(env, generation).default_model)


def _fake_llm(messages, response_format=None, **kwargs):
    """The example's language model is out of scope here: titles, summaries
    and events get a fixed answer so only the ASR path decides the outcome."""
    content = '{"events": []}' if response_format else "Test meeting"
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                    finish_reason="stop")], usage=None)


def read_example():
    values = {}
    with open(EXAMPLE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.split(" #")[0].strip().strip('"').strip("'")
    return values


def test_the_example_sets_nothing_else_that_reaches_the_asr_service():
    extra = [k for k in read_example() if k.startswith(ASR_PREFIXES) and k not in ASR_KEYS]
    assert not extra, f"handle these in tests/test_documented_defaults.py: {extra}"


def test_the_example_names_no_transcription_model():
    """The realistic new install: nothing in Speakr names a model."""
    assert not any("MODEL" in k and k.startswith(ASR_PREFIXES) for k in read_example())


# --------------------------------------------------------------- set-up

class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@contextmanager
def documented_speakr(service, admin_default=None, embeddings=None):
    """Speakr on the example's ASR settings, pointed at a fake service."""
    generation, service_env = SERVICES[service]
    example = read_example()
    with FakeASRServer(ServiceModelRules(service_env, generation)) as asr, app.app_context():
        saved_env = {k: os.environ.get(k) for k in ASR_KEYS + ("TRANSCRIPTION_BASE_URL", "TRANSCRIPTION_API_KEY")}
        saved_settings = {k: SystemSetting.get_setting(k, None) for k in MODEL_SETTINGS}
        try:
            for key in ASR_KEYS:
                os.environ[key] = example.get(key, "")
            os.environ["ASR_BASE_URL"] = asr.url
            if embeddings is not None:
                os.environ["ASR_RETURN_SPEAKER_EMBEDDINGS"] = "true" if embeddings else "false"
            # The example configures no other transcription service.
            os.environ["TRANSCRIPTION_BASE_URL"] = ""
            os.environ["TRANSCRIPTION_API_KEY"] = ""
            for key in MODEL_SETTINGS:
                SystemSetting.set_setting(key, "", setting_type="string")
            if admin_default:
                SystemSetting.set_setting("transcription_default_model", admin_default, setting_type="string")
            with patch("src.config.app_config.TRANSCRIPTION_MODELS_AVAILABLE", []), \
                    patch("src.tasks.processing.call_llm_completion", side_effect=_fake_llm):
                assert get_registry().reinitialize().__class__.__name__ == "ASREndpointConnector"
                yield asr
        finally:
            db.session.rollback()
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for key, value in saved_settings.items():
                SystemSetting.set_setting(key, value or "", setting_type="string")
            get_registry().reinitialize()


@pytest.fixture
def clean_reference():
    with app.app_context():
        saved = SystemSetting.get_setting(vec.SETTING_KEY, None)
        SystemSetting.query.filter_by(key=vec.SETTING_KEY).delete()
        db.session.commit()
    yield
    with app.app_context():
        SystemSetting.query.filter_by(key=vec.SETTING_KEY).delete()
        db.session.commit()
        if saved:
            SystemSetting.set_setting(vec.SETTING_KEY, saved, setting_type="string")


def _user(admin=False):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"dd_{s}", email=f"dd_{s}@local.test", password="x", is_admin=admin)
    db.session.add(u)
    db.session.commit()
    return u


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _wav_bytes(seconds=4, freq=330):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(16000 * seconds)))
    return buf.getvalue()


def _drain(recording_ids):
    """Run the queued transcription-side jobs of these recordings, in order."""
    for _ in range(12):
        jobs = (ProcessingJob.query
                .filter(ProcessingJob.recording_id.in_(recording_ids),
                        ProcessingJob.status == "queued",
                        ProcessingJob.job_type.in_(TRANSCRIPTION_JOBS))
                .order_by(ProcessingJob.id).all())
        if not jobs:
            return
        for job in jobs:
            job.status = "processing"
            job.started_at = datetime.utcnow()
            db.session.commit()
            job_queue._process_job(job)
            db.session.expire_all()


def _cleanup(user_ids):
    from src.models import SpeakerVoiceSample, Speaker
    from src.models.upload_join import UploadJoinPart
    rec_ids = [r.id for r in Recording.query.filter(Recording.user_id.in_(user_ids)).all()]
    if rec_ids:
        ProcessingJob.query.filter(ProcessingJob.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        SpeakerVoiceSample.query.filter(SpeakerVoiceSample.recording_id.in_(rec_ids)).delete(synchronize_session=False)
    UploadJoinPart.query.filter(UploadJoinPart.user_id.in_(user_ids)).delete(synchronize_session=False)
    if rec_ids:
        Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
    Speaker.query.filter(Speaker.user_id.in_(user_ids)).delete(synchronize_session=False)
    User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.session.commit()


def _assert_requests(asr, path, works, admin_default):
    assert asr.requests, f"{path}: nothing reached the ASR service"
    for req in asr.requests:
        if admin_default:
            assert req["model_sent"] and req["model"] == admin_default, (path, req)
        else:
            assert not req["model_sent"], (path, req)  # the service decides
        if works:
            assert req["status"] == 200, (path, req)
        else:
            assert req["status"] != 200, (path, req)


def _assert_outcome(recording, path, works):
    recording = db.session.get(Recording, recording.id)
    if works:
        assert recording.status in ("COMPLETED", "SUMMARIZING"), (path, recording.status, recording.error_message)
        assert "Hello from the test service" in (recording.transcription or ""), path
    else:
        assert recording.status == "FAILED", (path, recording.status)
        message = f"{recording.error_message or ''} {recording.transcription or ''}".lower()
        assert "model" in message, (path, message)


# ------------------------------------------------ voice embedding check

CHECK_CELLS = [(s, a) for s in SERVICES for a in (None, "large-v3")]


@pytest.mark.parametrize("service,admin_default", CHECK_CELLS)
def test_the_startup_voice_embedding_check(service, admin_default, clean_reference, caplog):
    works = expected_to_work(service, admin_default)
    with documented_speakr(service, admin_default) as asr:
        caplog.clear()
        with caplog.at_level(logging.INFO):
            result = vec.check_voice_embeddings(app)
        _assert_requests(asr, "startup check", works, admin_default)
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        if works:
            assert result["status"] == vec.STATUS_OK, result
            assert not errors, errors
        else:
            assert result["status"] == vec.STATUS_UNKNOWN, result
            warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
            assert "voice embedding check" in warnings.lower() and "PRELOAD_MODEL" in warnings, warnings


@pytest.mark.parametrize("service,admin_default", CHECK_CELLS)
def test_check_now_and_rebaseline_from_the_admin_page(service, admin_default, clean_reference):
    works = expected_to_work(service, admin_default)
    with documented_speakr(service, admin_default) as asr:
        admin = _user(admin=True)
        try:
            client = _client(admin)
            for path in ("/admin/voice-embeddings/check", "/admin/voice-embeddings/rebaseline"):
                asr.reset()
                response = client.post(path)
                assert response.status_code < 500, (path, response.get_data(as_text=True))
                _assert_requests(asr, path, works, admin_default)
        finally:
            _cleanup([admin.id])


@pytest.mark.parametrize("service", list(SERVICES))
def test_the_check_sends_nothing_when_speaker_embeddings_are_off(service, clean_reference):
    with documented_speakr(service, None, embeddings=False) as asr:
        result = vec.check_voice_embeddings(app)
        assert result.get("supported") is False
        assert asr.requests == []


# ------------------------------------------------ recordings

UPLOAD_CELLS = [
    ("current, PRELOAD_MODEL unset", None),
    ("current, PRELOAD_MODEL unset", "large-v3"),
    ("0.4.1, PRELOAD_MODEL empty", None),
    ("0.4.1, PRELOAD_MODEL empty", "large-v3"),
    ("0.4.1, PRELOAD_MODEL=large-v3", None),
]


@pytest.mark.parametrize("service,admin_default", UPLOAD_CELLS)
def test_every_recording_path(service, admin_default):
    works = expected_to_work(service, admin_default)
    with documented_speakr(service, admin_default) as asr:
        user = _user()
        try:
            client = _client(user)

            # Upload through the web app.
            asr.reset()
            r = client.post("/upload", data={"file": (io.BytesIO(_wav_bytes()), "meeting.wav")},
                            content_type="multipart/form-data")
            assert r.status_code == 202, r.get_data(as_text=True)
            recording = db.session.get(Recording, r.get_json()["id"])
            _drain([recording.id])
            _assert_requests(asr, "upload", works, admin_default)
            _assert_outcome(recording, "upload", works)
            if works:
                embeddings = db.session.get(Recording, recording.id).speaker_embeddings or {}
                assert "SPEAKER_00" in embeddings, "speaker embeddings were not stored"

            # Reprocess the transcription.
            asr.reset()
            r = client.post(f"/recording/{recording.id}/reprocess_transcription", json={})
            assert r.status_code < 300, r.get_data(as_text=True)
            _drain([recording.id])
            _assert_requests(asr, "reprocess", works, admin_default)
            _assert_outcome(recording, "reprocess", works)

            # Bulk reprocess.
            asr.reset()
            r = client.post("/api/recordings/bulk-reprocess",
                            json={"recording_ids": [recording.id], "type": "transcription"})
            assert r.status_code < 300, r.get_data(as_text=True)
            _drain([recording.id])
            _assert_requests(asr, "bulk reprocess", works, admin_default)
            _assert_outcome(recording, "bulk reprocess", works)

            # API v1 transcribe.
            asr.reset()
            r = client.post(f"/api/v1/recordings/{recording.id}/transcribe", json={})
            assert r.status_code < 300, r.get_data(as_text=True)
            _drain([recording.id])
            _assert_requests(asr, "API v1 transcribe", works, admin_default)
            _assert_outcome(recording, "API v1 transcribe", works)

            # API v1 upload.
            asr.reset()
            r = client.post("/api/v1/recordings/upload",
                            data={"file": (io.BytesIO(_wav_bytes()), "api.wav")},
                            content_type="multipart/form-data")
            assert r.status_code < 300, r.get_data(as_text=True)
            body = r.get_json()
            api_rec = db.session.get(Recording, body.get("id") or body.get("recording_id")
                                     or (body.get("recording") or {}).get("id"))
            _drain([api_rec.id])
            _assert_requests(asr, "API v1 upload", works, admin_default)
            _assert_outcome(api_rec, "API v1 upload", works)

            # Two files joined into one recording.
            asr.reset()
            group = uuid.uuid4().hex
            joined = None
            for index in range(2):
                r = client.post("/upload", data={
                    "file": (io.BytesIO(_wav_bytes(seconds=2, freq=300 + 100 * index)), f"part{index}.wav"),
                    "join_group": group, "join_index": str(index), "join_count": "2",
                }, content_type="multipart/form-data")
                assert r.status_code == 202, r.get_data(as_text=True)
                joined = r.get_json().get("id") or joined
            assert joined, "the join produced no recording"
            _drain([joined])
            _assert_requests(asr, "upload join", works, admin_default)
            _assert_outcome(db.session.get(Recording, joined), "upload join", works)

            # Incognito (not enabled by the example; switched on for this step).
            asr.reset()
            with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True):
                r = client.post("/api/recordings/incognito",
                                data={"file": (io.BytesIO(_wav_bytes()), "private.wav")},
                                content_type="multipart/form-data")
            _assert_requests(asr, "incognito", works, admin_default)
            if works:
                assert r.status_code == 200, r.get_data(as_text=True)
                assert "Hello from the test service" in r.get_data(as_text=True)
            else:
                assert r.status_code >= 400
                assert "model" in r.get_data(as_text=True).lower()
        finally:
            _cleanup([user.id])
