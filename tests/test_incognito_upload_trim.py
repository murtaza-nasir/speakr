"""Incognito transcribes the kept span and stores no Recording.

The range is the one a normal upload accepts. The cut runs on the temporary
file before transcription, so the file handed to ASR already starts at the
beginning of the kept audio. No range, and a range that still covers the
whole file, leave that file as it arrived. The request folder is removed
either way.

SHARED-DB: the user is removed afterwards.
"""
import io
import os
import shutil
import subprocess
import sys
import uuid
import wave
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
import src.api.recordings as rec_api

app.config["WTF_CSRF_ENABLED"] = False

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are not installed",
)


def _wav_bytes(seconds, rate=16000):
    data = io.BytesIO()
    with wave.open(data, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x01" * rate * seconds)
    return data.getvalue()


def _duration(path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True)
    return float(probe.stdout.strip())


@pytest.fixture
def incognito(tmp_path, monkeypatch):
    root = tmp_path / "incognito"
    monkeypatch.setenv("INCOGNITO_TEMP_DIR", str(root))
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"inctrim_{suffix}", email=f"inctrim_{suffix}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    yield client, uid, root
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _post(client, payload, fields=None):
    data = {"file": (io.BytesIO(payload), "clip.wav"), "language": "en"}
    if fields:
        data.update(fields)
    heard = {}

    def transcribe(filepath, **kwargs):
        heard["path"] = filepath
        heard["duration"] = _duration(filepath)
        heard["size"] = os.path.getsize(filepath)
        return {
            "transcription": "kept audio",
            "title": "Incognito Recording",
            "audio_duration_seconds": heard["duration"],
        }

    with patch.object(rec_api, "ENABLE_INCOGNITO_MODE", True), \
         patch("src.tasks.processing.transcribe_incognito", side_effect=transcribe):
        response = client.post("/api/recordings/incognito", data=data, content_type="multipart/form-data")
    return response, heard


def _no_recording(uid, root):
    with app.app_context():
        assert Recording.query.filter_by(user_id=uid).count() == 0
    leftover = list(root.rglob("*")) if root.exists() else []
    assert leftover == []


def test_a_real_cut_is_what_transcription_hears(incognito):
    client, uid, root = incognito
    payload = _wav_bytes(6)
    response, heard = _post(client, payload, {"trim_start": "2", "trim_end": "4"})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["transcription"] == "kept audio"
    assert heard["duration"] == pytest.approx(2, abs=0.4)
    assert heard["duration"] < 4
    assert heard["size"] != len(payload)
    _no_recording(uid, root)


@pytest.mark.parametrize("fields", [
    None,
    {"trim_start": "0", "trim_end": "6"},
    {"trim_end": "9"},
])
def test_no_range_and_a_whole_file_range_transcribe_the_upload(incognito, fields):
    client, uid, root = incognito
    payload = _wav_bytes(6)
    response, heard = _post(client, payload, fields)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["transcription"] == "kept audio"
    assert heard["duration"] == pytest.approx(6, abs=0.05)
    assert heard["size"] == len(payload)
    assert heard["path"].endswith(".wav")
    _no_recording(uid, root)


@pytest.mark.parametrize("fields", [
    {"trim_start": "1.5"},
    {"trim_start": "-1"},
    {"trim_start": "9"},
    {"trim_start": "3", "trim_end": "3"},
    {"trim_end": "0"},
])
def test_a_range_the_server_cannot_apply_creates_no_recording(incognito, fields):
    client, uid, root = incognito
    response, heard = _post(client, _wav_bytes(6), fields)

    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "invalid_trim_range"
    assert "error" in body
    assert heard == {}
    _no_recording(uid, root)


def test_a_trim_on_a_file_whose_duration_cannot_be_read_is_refused(incognito):
    client, uid, root = incognito
    response, heard = _post(client, b"This is not audio, only text.\n" * 64, {"trim_start": "1"})

    assert response.status_code == 400
    assert response.get_json()["code"] == "audio_decode_failed"
    assert heard == {}
    _no_recording(uid, root)
