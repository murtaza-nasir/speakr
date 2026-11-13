"""An upload converted from WebM to MP3 is not converted again (#587).

The request converted an Opus WebM to MP3 and stored the MP3 under the
upload's .webm name. Every transcription then saw a .webm path and encoded the
MP3 again. The seek conversion now looks at the container ffprobe reports,
and a converted upload is stored under the extension of the stored file.
"""

import logging
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from src.services.recording_ingestion import store_recording
from src.utils.audio_conversion import convert_if_needed

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def mp3_named_webm(tmp_path):
    mp3 = tmp_path / "source.mp3"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", str(mp3))
    named = tmp_path / "clip.webm"
    shutil.copy(mp3, named)
    return str(named)


@pytest.fixture
def opus_webm(tmp_path):
    path = tmp_path / "clip.webm"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libopus", "-f", "webm", str(path))
    return str(path)


@needs_ffmpeg
def test_mp3_bytes_named_webm_are_not_converted(mp3_named_webm, caplog):
    caplog.set_level(logging.INFO)
    with app.app_context():
        result = convert_if_needed(mp3_named_webm, original_filename="clip.webm", delete_original=False)
    assert result.was_converted is False
    assert result.output_path == mp3_named_webm
    assert not any("lacks seek support" in r.getMessage() for r in caplog.records)
    assert any("Codec mp3 is supported, no conversion needed" in r.getMessage() for r in caplog.records)


@needs_ffmpeg
def test_opus_webm_is_still_converted(opus_webm):
    with app.app_context():
        result = convert_if_needed(opus_webm, original_filename="clip.webm", delete_original=False)
    try:
        assert result.was_converted is True
        assert result.final_codec == "mp3"
        assert result.output_path.endswith(".mp3")
    finally:
        if result.output_path != opus_webm and os.path.exists(result.output_path):
            os.remove(result.output_path)


def test_a_webm_without_a_known_container_is_still_converted(tmp_path):
    """Without a container name the seek conversion keeps its old rule."""
    path = tmp_path / "clip.webm"
    path.write_bytes(b"\x00" * 64)
    out = str(tmp_path / "clip.mp3")
    with app.app_context(), \
            patch("src.utils.audio_conversion.compress_audio", return_value=(out, "audio/mpeg", None)) as compress, \
            patch("os.path.getsize", return_value=64):
        result = convert_if_needed(str(path), original_filename="clip.webm", delete_original=False,
                                   codec_info={"audio_codec": "opus", "has_video": False})
    compress.assert_called_once()
    assert result.was_converted is True


# --- the storage key of a converted upload -------------------------------------

class _Storage:
    def __init__(self):
        self.keys = []

    def build_recording_key(self, original_filename, recording_id, *, now):
        self.keys.append(original_filename)
        return f"recordings/{recording_id}/{original_filename}"

    def upload_local_file(self, filepath, key, *, content_type, delete_source):
        os.remove(filepath)
        return SimpleNamespace(locator=f"local://{key}")


@pytest.fixture
def owner():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"wsc_{s}", email=f"wsc_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        Recording.query.filter_by(user_id=uid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def _store(uid, filepath, original_filename):
    with patch("src.services.storage_quota.reserve_room"):
        return store_recording(
            owner=db.session.get(User, uid), storage=_Storage(), filepath=filepath,
            now=datetime(2026, 5, 6, 7, 8, 9), recorded_in_app=False, original_filename=original_filename,
            user_title=None, final_file_size=4, meeting_date=None, mime_type="audio/mpeg",
            audio_duration_seconds=2.0, notes=None, selected_folder=None, selected_tags=[],
            processing_source="upload", file_hash=None, prompt_variables=None,
            effective_audio_only=True, prepare=None,
        ).recording


@pytest.mark.parametrize("staged_name,original,expected_key_name", [
    # Converted in the request: the stored file is an MP3.
    ("20260506_abc_meeting.mp3", "meeting.webm", "meeting.mp3"),
    # Not converted: the uploaded name is kept.
    ("20260506_abc_memo.mp3", "memo.mp3", "memo.mp3"),
    ("20260506_abc_Talk.WAV", "Talk.WAV", "Talk.WAV"),
    # secure_filename dropped the extension of a non-ASCII name.
    ("20260506_abc_webm", "会议.webm", "会议.webm"),
])
def test_store_recording_key_uses_the_stored_files_extension(owner, tmp_path, staged_name, original,
                                                              expected_key_name):
    staged = tmp_path / staged_name
    staged.write_bytes(b"data")
    with app.app_context():
        rec = _store(owner, str(staged), original)
        assert rec.audio_path == f"local://recordings/{rec.id}/{expected_key_name}"
        assert rec.original_filename == original
