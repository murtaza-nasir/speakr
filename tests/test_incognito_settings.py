"""Incognito uses the same settings chain as an upload (#412 audit P14).

It used to pass the form's empty language straight through (so the user's
language never applied), and it skipped the account and admin hotwords and
initial prompt and the ASR speaker-count defaults. Nothing is stored either way.

An incognito video goes through the real extraction chain with only the
ffmpeg and ffprobe binaries faked, so a signature mismatch between the
wrappers fails here.

SHARED-DB: the user and admin setting are restored afterwards.
"""

import io
import os
import subprocess
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import SystemSetting, User
import src.tasks.processing as proc

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def user():
    with app.app_context():
        old = SystemSetting.get_setting("admin_default_initial_prompt", None)
        SystemSetting.set_setting("admin_default_initial_prompt", "A planning meeting.", setting_type="string")
        s = uuid.uuid4().hex[:8]
        u = User(username=f"inc_{s}", email=f"inc_{s}@local.test", password="x",
                 transcription_language="de", transcription_hotwords="Speakr")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        if old is None:
            SystemSetting.query.filter_by(key="admin_default_initial_prompt").delete()
        else:
            SystemSetting.set_setting("admin_default_initial_prompt", old, setting_type="string")
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def test_incognito_resolves_language_hotwords_and_admin_prompt(user, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 2048)
    connector = MagicMock(PROVIDER_NAME="test", supports_diarization=False, default_diarize=False)
    connector.specifications = MagicMock(max_duration_seconds=1800, handles_chunking_internally=False,
                                         recommended_chunk_seconds=600, unsupported_codecs=[])
    response = MagicMock(text="hello world transcript", segments=None, speakers=None, speaker_embeddings=None)
    response.has_diarization.return_value = False
    connector.transcribe.return_value = response
    with app.app_context():
        u = db.session.get(User, user)
        registry = MagicMock()
        registry.get_active_connector.return_value = connector
        with patch("src.services.transcription.get_registry", return_value=registry), \
             patch.object(proc, "is_video_file", return_value=False), \
             patch.object(proc, "convert_if_needed", return_value=MagicMock(was_converted=False)), \
             patch.object(proc, "chunking_service") as svc, patch.object(proc, "client", None):
            svc.needs_chunking.return_value = False
            svc.get_audio_duration.return_value = 30.0
            proc.transcribe_incognito(str(audio), "a.mp3", language="", user=u)
    call = connector.transcribe.call_args
    request = call.args[0] if call.args else next(iter(call.kwargs.values()))
    assert request.language == "de"
    assert request.hotwords == "Speakr"
    assert request.prompt == "A planning meeting."


def test_incognito_decode_failure_reports_a_fixed_message(user, tmp_path):
    """FFmpeg's output (build banner, temp path) stays out of the result (#482)."""
    from src.utils.ffmpeg_utils import AUDIO_DECODE_FAILED_MESSAGE, FFmpegError
    video = tmp_path / "a.mp4"
    video.write_bytes(b"not a video")
    connector = MagicMock(PROVIDER_NAME="test", supports_diarization=False)
    with app.app_context():
        u = db.session.get(User, user)
        registry = MagicMock()
        registry.get_active_connector.return_value = connector
        with patch("src.services.transcription.get_registry", return_value=registry), \
             patch.object(proc, "is_video_file", return_value=True), \
             patch.object(proc, "VIDEO_PASSTHROUGH_ASR", False), \
             patch.object(proc, "extract_audio_from_video",
                          side_effect=FFmpegError("ffmpeg version n8.1.3 ... Error opening input file /tmp/tmpx.mp4")):
            result = proc.transcribe_incognito(str(video), "a.mp4", language="", user=u)
    assert result["error"] == AUDIO_DECODE_FAILED_MESSAGE
    assert result["error_code"] == "audio_decode_failed"
    assert connector.transcribe.call_count == 0


_FFMPEG_BANNER = (b"ffmpeg version n8.1.3 Copyright (c) 2000-2026 the FFmpeg developers\n"
                  b"  configuration: --enable-gpl\n"
                  b"Error opening input file /tmp/tmpabc.mp4.\n"
                  b"Error opening input files: Invalid data found when processing input\n")


def _fake_probe(filename, cmd='ffprobe', timeout=None):
    """ffprobe's JSON: a video with AAC audio, or the extracted audio file."""
    audio = {"codec_type": "audio", "codec_name": "aac" if filename.endswith(".mp4") else "mp3",
             "sample_rate": "44100", "channels": 1}
    streams = [audio]
    if filename.endswith(".mp4"):
        streams.insert(0, {"codec_type": "video", "codec_name": "h264"})
    return {"streams": streams, "format": {"format_name": "mov,mp4" if filename.endswith(".mp4") else "mp3",
                                           "duration": "30.0"}}


def _post_incognito_video(user_id, ffmpeg):
    """POST a video to the incognito route; only ffmpeg/ffprobe and the ASR service are faked."""
    connector = MagicMock(PROVIDER_NAME="test", supports_diarization=False, default_diarize=False)
    connector.specifications = MagicMock(max_duration_seconds=1800, handles_chunking_internally=False,
                                         recommended_chunk_seconds=600, unsupported_codecs=[],
                                         preserve_original_audio=False)
    response = MagicMock(text="hello world transcript", segments=None, speakers=None, speaker_embeddings=None)
    response.has_diarization.return_value = False
    connector.transcribe.return_value = response
    registry = MagicMock()
    registry.get_active_connector.return_value = connector
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True), \
         patch("src.services.transcription.get_registry", return_value=registry), \
         patch.object(proc, "VIDEO_PASSTHROUGH_ASR", False), \
         patch.object(proc, "chunking_service", None), patch.object(proc, "client", None), \
         patch("src.utils.ffprobe.probe", side_effect=_fake_probe), \
         patch("src.utils.ffmpeg_utils.subprocess.run", side_effect=ffmpeg) as run:
        resp = client.post("/api/recordings/incognito",
                           data={"file": (io.BytesIO(b"\x00" * 4096), "meeting.mp4")},
                           content_type="multipart/form-data")
    return resp, run, connector


def test_incognito_video_is_extracted_through_the_real_chain(user):
    def ffmpeg(cmd, **kwargs):
        with open(cmd[-1], "wb") as out:   # the output file ffmpeg would write
            out.write(b"\xff\xfb" * 1024)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    # allow_debug_copy=False reaches the FFmpeg helper: no debug copy of the audio.
    with patch.dict(os.environ, {"PRESERVE_TEMP_AUDIO": "true"}), patch("shutil.copy2") as copy2:
        resp, run, connector = _post_incognito_video(user, ffmpeg)
    assert copy2.call_count == 0
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["transcription"] == "hello world transcript"
    extraction = run.call_args_list[0].args[0]
    assert extraction[0] == "ffmpeg" and "-vn" in extraction
    sent = connector.transcribe.call_args.args[0]
    assert sent.filename.endswith((".mp3", ".m4a", ".flac", ".opus"))


def test_incognito_video_extraction_failure_keeps_ffmpeg_output_out(user):
    def ffmpeg(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=_FFMPEG_BANNER)

    resp, _, connector = _post_incognito_video(user, ffmpeg)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "audio_decode_failed"
    text = resp.get_data(as_text=True).lower()
    assert "ffmpeg" not in text and "/tmp/" not in text
    assert connector.transcribe.call_count == 0
