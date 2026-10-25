"""Uploads with a video extension under VIDEO_RETENTION / VIDEO_PASSTHROUGH_ASR (#482).

A file named .mp4 that ffprobe cannot read used to be kept as a video and
fail later in processing. It is now converted like any other upload and
refused with 400 audio_decode_failed; so is a video without an audio track,
as without these settings. Only a probe that did not finish still trusts the
extension. The real ffprobe and ffmpeg binaries are used where they are
installed.
"""
import io
import os
import shutil
import subprocess
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from src.utils.ffmpeg_utils import AUDIO_DECODE_FAILED_MESSAGE
from src.utils.ffprobe import (
    FFProbeError, FFProbeNotFoundError, FFProbeTimeoutError, has_audio_stream,
    probe, probe_rejected_input,
)
import src.tasks.processing as proc

app.config["WTF_CSRF_ENABLED"] = False

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are not installed",
)

JUNK = b"This is not a video, only text with a video extension.\n" * 64


def _ffmpeg_video(path, with_audio=True):
    """Write a one-second H.264 MP4, with or without an AAC track."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path.read_bytes()


class _Storage:
    def __init__(self, staging):
        self.staging = staging
        self.uploaded = []

    def get_staging_dir(self):
        os.makedirs(self.staging, exist_ok=True)
        return self.staging

    def build_recording_key(self, original_filename, recording_id=None, *, now=None):
        return f"recordings/test/{recording_id}/{original_filename}"

    def upload_local_file(self, local_path, key, *, content_type=None, delete_source=False):
        self.uploaded.append((local_path, key))
        return MagicMock(locator=f"local://{key}", key=key)


@pytest.fixture
def user():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"junk_{s}", email=f"junk_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        Recording.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _upload(user_id, payload, filename, *, retention=False, passthrough=False, probe_error=None):
    staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
    storage = _Storage(staging)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    extra = []
    if probe_error is not None:
        extra.append(patch("src.api.recordings.get_codec_info", side_effect=probe_error))
    with patch("src.api.recordings.get_storage_service", return_value=storage), \
         patch("src.api.recordings.VIDEO_RETENTION", retention), \
         patch("src.api.recordings.VIDEO_PASSTHROUGH_ASR", passthrough), \
         patch("src.api.recordings.chunking_service", None), \
         patch("src.services.job_queue.job_queue.enqueue", return_value=1) as enqueue:
        for p in extra:
            p.start()
        try:
            resp = client.post("/upload", data={"file": (io.BytesIO(payload), filename)},
                               content_type="multipart/form-data")
        finally:
            for p in extra:
                p.stop()
    leftovers = os.listdir(staging) if os.path.isdir(staging) else []
    shutil.rmtree(staging, ignore_errors=True)
    return resp, storage, enqueue, leftovers


def _assert_refused(resp, enqueue, leftovers, user_id):
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["code"] == "audio_decode_failed"
    assert body["error"] == AUDIO_DECODE_FAILED_MESSAGE
    text = resp.get_data(as_text=True).lower()
    assert "ffmpeg" not in text and "ffprobe" not in text and "_staging" not in text
    assert leftovers == []
    assert enqueue.call_count == 0
    with app.app_context():
        assert Recording.query.filter_by(user_id=user_id).count() == 0


# --- helpers ---------------------------------------------------------------

def test_probe_rejected_input_tells_an_unreadable_file_from_a_probe_that_did_not_finish():
    assert probe_rejected_input(FFProbeError("ffprobe failed: Invalid data found when processing input"))
    assert not probe_rejected_input(FFProbeTimeoutError("ffprobe timed out after 10 seconds"))
    assert not probe_rejected_input(FFProbeNotFoundError("ffprobe command not found."))
    assert issubclass(FFProbeTimeoutError, FFProbeError) and issubclass(FFProbeNotFoundError, FFProbeError)


def test_probe_raises_the_timeout_and_not_found_kinds():
    with patch("src.utils.ffprobe.subprocess.Popen") as popen:
        popen.return_value.communicate.side_effect = subprocess.TimeoutExpired("ffprobe", 1)
        with pytest.raises(FFProbeTimeoutError):
            probe("x.mp4", timeout=1)
    with patch("src.utils.ffprobe.subprocess.Popen", side_effect=FileNotFoundError):
        with pytest.raises(FFProbeNotFoundError):
            probe("x.mp4", timeout=1)


def test_has_audio_stream():
    assert has_audio_stream({"has_audio": True})
    assert has_audio_stream({"audio_codec": "aac"})
    assert not has_audio_stream({"has_audio": False, "audio_codec": None, "has_video": True})


# --- /upload -----------------------------------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("retention,passthrough", [(True, False), (False, True), (False, False)])
def test_junk_with_a_video_extension_is_refused(user, retention, passthrough):
    resp, storage, enqueue, leftovers = _upload(user, JUNK, "meeting.mp4",
                                                retention=retention, passthrough=passthrough)
    _assert_refused(resp, enqueue, leftovers, user)
    assert storage.uploaded == []


@needs_ffmpeg
@pytest.mark.parametrize("retention,passthrough", [(True, False), (False, True), (False, False)])
def test_video_without_an_audio_track_is_refused_as_without_retention(user, tmp_path, retention, passthrough):
    payload = _ffmpeg_video(tmp_path / "silent.mp4", with_audio=False)
    resp, storage, enqueue, leftovers = _upload(user, payload, "silent.mp4",
                                                retention=retention, passthrough=passthrough)
    _assert_refused(resp, enqueue, leftovers, user)


@needs_ffmpeg
@pytest.mark.parametrize("retention,passthrough", [(True, False), (False, True)])
def test_real_video_with_audio_is_kept_as_video(user, tmp_path, retention, passthrough):
    payload = _ffmpeg_video(tmp_path / "talk.mp4", with_audio=True)
    resp, storage, enqueue, leftovers = _upload(user, payload, "talk.mp4",
                                                retention=retention, passthrough=passthrough)
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert enqueue.call_count == 1
    assert len(storage.uploaded) == 1
    assert storage.uploaded[0][0].endswith(".mp4")   # the video itself, not extracted audio


@needs_ffmpeg
def test_real_video_with_audio_has_its_audio_extracted_without_retention(user, tmp_path):
    payload = _ffmpeg_video(tmp_path / "talk.mp4", with_audio=True)
    resp, storage, enqueue, leftovers = _upload(user, payload, "talk.mp4")
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert enqueue.call_count == 1
    assert not storage.uploaded[0][0].endswith(".mp4")   # the extracted audio



def _ffmpeg_mp3_with_cover(path):
    """Write a one-second MP3 with an embedded cover picture."""
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-f", "lavfi", "-i", "color=c=red:size=64x64",
                    "-map", "0:a", "-map", "1:v", "-frames:v", "1", "-c:a", "libmp3lame",
                    "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(path)],
                   check=True, capture_output=True)
    return path.read_bytes()


@needs_ffmpeg
def test_cover_art_is_not_a_video_stream(tmp_path):
    from src.utils.ffprobe import get_codec_info
    _ffmpeg_mp3_with_cover(tmp_path / "song.mp3")
    info = get_codec_info(str(tmp_path / "song.mp3"))
    assert info["has_audio"] is True
    assert info["has_video"] is False and info["video_codec"] is None


@needs_ffmpeg
def test_mp3_with_cover_art_is_kept_as_audio_under_retention(user, tmp_path):
    payload = _ffmpeg_mp3_with_cover(tmp_path / "song.mp3")
    resp, storage, enqueue, leftovers = _upload(user, payload, "song.mp3", retention=True)
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert enqueue.call_count == 1
    with app.app_context():
        rec = Recording.query.filter_by(user_id=user).one()
        assert not (rec.mime_type or "").startswith("video/"), rec.mime_type

def test_a_probe_that_timed_out_still_trusts_the_video_extension(user):
    """A large MP4 can be slow to probe: it is kept as a video, as before."""
    with patch("src.api.recordings.convert_if_needed") as convert, \
         patch("src.api.recordings.get_duration", return_value=12.0), \
         patch("src.api.recordings.get_creation_date", return_value=None):
        resp, storage, enqueue, leftovers = _upload(
            user, JUNK, "slow.mp4", retention=True,
            probe_error=FFProbeTimeoutError("ffprobe timed out after 10 seconds"))
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert convert.call_count == 0
    assert enqueue.call_count == 1


# --- incognito ---------------------------------------------------------------

def _incognito(user_id, payload, filename):
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
         patch.object(proc, "VIDEO_PASSTHROUGH_ASR", True), \
         patch.object(proc, "chunking_service", None), patch.object(proc, "client", None):
        resp = client.post("/api/recordings/incognito",
                           data={"file": (io.BytesIO(payload), filename)},
                           content_type="multipart/form-data")
    return resp, connector


@needs_ffmpeg
def test_incognito_passthrough_refuses_junk_with_a_video_extension(user):
    resp, connector = _incognito(user, JUNK, "meeting.mp4")
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "audio_decode_failed"
    assert "ffmpeg" not in resp.get_data(as_text=True).lower()
    assert connector.transcribe.call_count == 0


@needs_ffmpeg
def test_incognito_passthrough_refuses_a_video_without_audio(user, tmp_path):
    resp, connector = _incognito(user, _ffmpeg_video(tmp_path / "s.mp4", with_audio=False), "s.mp4")
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "audio_decode_failed"
    assert connector.transcribe.call_count == 0


@needs_ffmpeg
def test_incognito_passthrough_sends_a_real_video(user, tmp_path):
    resp, connector = _incognito(user, _ffmpeg_video(tmp_path / "t.mp4"), "t.mp4")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert connector.transcribe.call_count == 1
