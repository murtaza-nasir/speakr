"""A single uploaded file can name the whole seconds to keep.

The upload is answered before the cut. The cut runs in the conversion the
convert job already runs, and the transcription is queued only for the audio
that remains. The fixture is three seconds at 220 Hz and then three seconds
at 880 Hz, so a head cut and a tail cut have the same length and different
content.
"""
import io
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import APIToken, Recording, User
from src.services.storage_quota import storage_used
from src.utils.ffmpeg_utils import FFmpegError
from src.utils.token_auth import hash_token

from tests.test_upload_conversion_job import (  # noqa: F401
    _jobs, _local_path, _run_convert_job, user,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are not installed",
)

HEAD_HZ = 220
TAIL_HZ = 880
# Measured on this fixture: the kept tone is about -22 dB, the other about -48 dB.
LOUD_DB = -35
QUIET_DB = -40


@pytest.fixture(autouse=True)
def _convert_workers():
    with patch("src.services.recording_ingestion.CONVERT_WORKERS", 1):
        yield


def _two_tone_mp3(path):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={HEAD_HZ}:duration=3",
         "-f", "lavfi", "-i", f"sine=frequency={TAIL_HZ}:duration=3",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
        check=True, capture_output=True)
    return path.read_bytes()


def _duration_seconds(path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True)
    return float(probe.stdout.strip())


def _band_mean_db(path, frequency):
    probe = subprocess.run(
        ["ffmpeg", "-i", str(path),
         "-af", f"bandpass=f={frequency}:width_type=h:w=40,volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True)
    for line in probe.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0].strip())
    raise AssertionError(probe.stderr)


def _post_upload(uid, payload, filename, fields=None):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    data = {"file": (io.BytesIO(payload), filename)}
    if fields:
        data.update(fields)
    return client.post("/upload", data=data, content_type="multipart/form-data")


def test_trim_start_alone_keeps_the_tail_of_the_recording(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        res = _post_upload(user, payload, "two.mp3", {"trim_start": "3"})

        assert res.status_code == 202
        rid = res.get_json()["id"]
        rec = db.session.get(Recording, rid)
        assert rec.status == "QUEUED"
        assert _jobs(rid) == [("convert", "queued")]
        assert _duration_seconds(_local_path(rec.audio_path)) == pytest.approx(6, abs=0.2)
        uploaded_size = rec.file_size
        assert storage_used(user) == uploaded_size

        assert _run_convert_job().status == "completed"

        rec = db.session.get(Recording, rid)
        kept = _local_path(rec.audio_path)
        assert _duration_seconds(kept) == pytest.approx(3, abs=0.25)
        assert _band_mean_db(kept, TAIL_HZ) > LOUD_DB
        assert _band_mean_db(kept, HEAD_HZ) < QUIET_DB
        assert rec.file_size == os.path.getsize(kept)
        assert rec.file_size < uploaded_size
        assert storage_used(user) == rec.file_size
        assert rec.status == "QUEUED"
        # The transcribe job is queued only after the kept audio replaced the upload.
        # That file is what transcription receives, so the Transcript starts at
        # its beginning and the Summary is of that Transcript.
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]


def _post_api(token, payload, filename, fields=None):
    client = app.test_client()
    data = {"file": (io.BytesIO(payload), filename)}
    if fields:
        data.update(fields)
    return client.post("/api/v1/recordings/upload", data=data, content_type="multipart/form-data",
                       headers={"X-API-Token": token})


def _token(uid):
    plaintext = f"trim-{secrets.token_urlsafe(16)}"
    db.session.add(APIToken(user_id=uid, token_hash=hash_token(plaintext), name="trim"))
    db.session.commit()
    return plaintext


def _assert_refused(res, code):
    assert res.status_code == 400
    assert res.get_json()["code"] == code
    assert "error" in res.get_json()


def test_trim_end_alone_keeps_the_head_of_the_recording(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        res = _post_upload(user, payload, "two.mp3", {"trim_end": "3"})
        assert res.status_code == 202
        rid = res.get_json()["id"]
        assert _run_convert_job().status == "completed"
        kept = _local_path(db.session.get(Recording, rid).audio_path)
        assert _duration_seconds(kept) == pytest.approx(3, abs=0.25)
        assert _band_mean_db(kept, HEAD_HZ) > LOUD_DB
        assert _band_mean_db(kept, TAIL_HZ) < QUIET_DB
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]


def test_trim_start_and_trim_end_keep_that_span(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        res = _post_upload(user, payload, "two.mp3", {"trim_start": "2", "trim_end": "4"})
        assert res.status_code == 202
        rid = res.get_json()["id"]
        assert _run_convert_job().status == "completed"
        kept = _local_path(db.session.get(Recording, rid).audio_path)
        assert _duration_seconds(kept) == pytest.approx(2, abs=0.25)
        assert _band_mean_db(kept, HEAD_HZ) > LOUD_DB
        assert _band_mean_db(kept, TAIL_HZ) > LOUD_DB


def test_the_api_upload_keeps_the_same_span(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        token = _token(user)
        res = _post_api(token, payload, "two.mp3", {"trim_start": "3"})
        assert res.status_code == 202
        rid = res.get_json()["id"]
        assert _jobs(rid) == [("convert", "queued")]
        assert _run_convert_job().status == "completed"
        kept = _local_path(db.session.get(Recording, rid).audio_path)
        assert _duration_seconds(kept) == pytest.approx(3, abs=0.25)
        assert _band_mean_db(kept, TAIL_HZ) > LOUD_DB
        assert _band_mean_db(kept, HEAD_HZ) < QUIET_DB
        APIToken.query.filter_by(user_id=user).delete()
        db.session.commit()


@pytest.mark.parametrize("fields", [None, {"trim_start": "", "trim_end": ""}, {"trim_start": "0", "trim_end": "30"}])
def test_a_range_that_covers_the_whole_file_is_stored_without_a_cut(user, tmp_path, fields):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        res = _post_upload(user, payload, "two.mp3", fields)
        assert res.status_code == 202
        rid = res.get_json()["id"]
        original = db.session.get(Recording, rid).audio_path
        assert _jobs(rid) == [("convert", "queued")]
        assert _run_convert_job().status == "completed"
        rec = db.session.get(Recording, rid)
        assert rec.audio_path == original
        with open(_local_path(rec.audio_path), "rb") as stored:
            assert stored.read() == payload
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]


def test_an_api_upload_with_no_range_keeps_the_whole_file(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        token = _token(user)
        res = _post_api(token, payload, "two.mp3")
        assert res.status_code == 202
        rid = res.get_json()["id"]
        original = db.session.get(Recording, rid).audio_path
        assert _run_convert_job().status == "completed"
        rec = db.session.get(Recording, rid)
        assert rec.audio_path == original
        with open(_local_path(rec.audio_path), "rb") as stored:
            assert stored.read() == payload
        APIToken.query.filter_by(user_id=user).delete()
        db.session.commit()


def test_a_webm_that_needs_conversion_is_cut_in_that_conversion(user, tmp_path):
    webm = tmp_path / "two.webm"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={HEAD_HZ}:duration=3",
         "-f", "lavfi", "-i", f"sine=frequency={TAIL_HZ}:duration=3",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         "-c:a", "libopus", "-f", "webm", str(webm)],
        check=True, capture_output=True)
    with app.app_context():
        res = _post_upload(user, webm.read_bytes(), "two.webm", {"trim_end": "3"})
        assert res.status_code == 202
        rid = res.get_json()["id"]
        assert _run_convert_job().status == "completed"
        kept = _local_path(db.session.get(Recording, rid).audio_path)
        assert not db.session.get(Recording, rid).audio_path.endswith(".webm")
        assert _duration_seconds(kept) == pytest.approx(3, abs=0.25)
        assert _band_mean_db(kept, HEAD_HZ) > LOUD_DB
        assert _band_mean_db(kept, TAIL_HZ) < QUIET_DB


@pytest.mark.parametrize("fields", [
    {"trim_start": "1.5"},
    {"trim_end": "2.0"},
    {"trim_start": "nope"},
    {"trim_start": "-1"},
    {"trim_start": "9"},
    {"trim_start": "3", "trim_end": "3"},
    {"trim_start": "4", "trim_end": "2"},
    {"trim_end": "0"},
])
def test_a_range_the_server_cannot_apply_creates_no_recording(user, tmp_path, fields):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        res = _post_upload(user, payload, "two.mp3", fields)
        _assert_refused(res, "invalid_trim_range")
        assert Recording.query.filter_by(user_id=user).count() == 0


def test_the_api_refuses_a_decimal_trim_with_no_recording(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        token = _token(user)
        res = _post_api(token, payload, "two.mp3", {"trim_start": "1.5"})
        _assert_refused(res, "invalid_trim_range")
        assert Recording.query.filter_by(user_id=user).count() == 0
        APIToken.query.filter_by(user_id=user).delete()
        db.session.commit()


def test_a_trim_on_a_file_whose_duration_cannot_be_read_is_refused(user):
    with app.app_context():
        res = _post_upload(user, b"This is not audio, only text.\n" * 64, "notes.mp3", {"trim_start": "1"})
        _assert_refused(res, "audio_decode_failed")
        assert Recording.query.filter_by(user_id=user).count() == 0


def test_a_failed_cut_fails_the_recording_and_does_not_transcribe_the_whole_file(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        rid = _post_upload(user, payload, "two.mp3", {"trim_start": "3"}).get_json()["id"]
        original = _local_path(db.session.get(Recording, rid).audio_path)
        with patch("src.utils.audio_conversion.convert_if_needed", side_effect=FFmpegError("cut failed")):
            job = _run_convert_job()
        assert job.status == "failed"
        rec = db.session.get(Recording, rid)
        assert rec.status == "FAILED"
        assert _jobs(rid) == [("convert", "failed")]
        assert _duration_seconds(original) == pytest.approx(6, abs=0.2)
        assert os.path.exists(original)


def test_the_quota_judges_the_uploaded_file_not_the_kept_span(user, tmp_path):
    long = tmp_path / "long.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=80",
         "-c:a", "libmp3lame", "-b:a", "128k", str(long)],
        check=True, capture_output=True)
    payload = long.read_bytes()
    with app.app_context():
        owner = db.session.get(User, user)
        owner.storage_quota_mb = 1
        db.session.commit()
        assert len(payload) > 1024 * 1024
        res = _post_upload(user, payload, "long.mp3", {"trim_end": "1"})
        assert res.status_code == 507
        assert res.get_json()["code"] == "storage_quota_exceeded"
        assert res.get_json()["file_bytes"] == len(payload)
        assert Recording.query.filter_by(user_id=user).count() == 0


def _sample_root():
    for parent in Path(__file__).resolve().parents:
        root = parent / "test-audio-files"
        if (root / "samples").is_dir():
            return root
    return None


SAMPLE_ROOT = _sample_root()

# Real containers from test-audio-files. The test keeps the first eight
# seconds, so a long sample is not re-encoded in full.
REAL_FORMATS = [
    "samples/es-en-la-peluqueria.mp3",
    "samples/es-en-la-peluqueria.ogg",
    "samples/es-en-la-peluqueria.flac",
    "samples/es-en-la-peluqueria.opus",
    "samples/es-en-la-peluqueria.webm",
    "samples/es-en-la-peluqueria.aac",
    "samples/es-album-familiar.m4a",
    "samples/en-when-you-call-someone.wav",
    "format-matrix/fr-mono.mp4",
]


def _clip_copy(src, dest):
    """The first eight seconds of a real file, still in that container."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", "8", "-c", "copy", str(dest)],
        check=True, capture_output=True)
    return dest.read_bytes()


@pytest.mark.skipif(SAMPLE_ROOT is None, reason="test-audio-files is not beside this checkout")
@pytest.mark.parametrize("relative", REAL_FORMATS)
def test_a_real_sample_keeps_the_named_span(user, tmp_path, relative):
    src = SAMPLE_ROOT / relative
    clip = tmp_path / src.name
    payload = _clip_copy(src, clip)
    duration = _duration_seconds(clip)
    assert duration > 4
    with app.app_context():
        res = _post_upload(user, payload, src.name, {"trim_start": "1", "trim_end": "4"})
        assert res.status_code == 202, res.get_json()
        rid = res.get_json()["id"]
        assert _jobs(rid) == [("convert", "queued")]
        assert _duration_seconds(_local_path(db.session.get(Recording, rid).audio_path)) == pytest.approx(duration, abs=0.35)
        assert _run_convert_job().status == "completed"
        rec = db.session.get(Recording, rid)
        kept = _local_path(rec.audio_path)
        assert _duration_seconds(kept) == pytest.approx(3, abs=0.35)
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]


@pytest.mark.skipif(SAMPLE_ROOT is None, reason="test-audio-files is not beside this checkout")
def test_a_real_speech_file_drops_the_silenced_head(user, tmp_path):
    """The head of a real clip is silenced, so the kept tail is the speech."""
    src = SAMPLE_ROOT / "samples/zh-hello-mr-lin-i-got-it.mp3"
    shaped = tmp_path / "shaped.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-af", "volume=0:enable='lt(t,3)'", "-t", "6",
         "-c:a", "libmp3lame", str(shaped)],
        check=True, capture_output=True)
    with app.app_context():
        res = _post_upload(user, shaped.read_bytes(), "shaped.mp3", {"trim_start": "3"})
        assert res.status_code == 202
        rid = res.get_json()["id"]
        assert _run_convert_job().status == "completed"
        kept = _local_path(db.session.get(Recording, rid).audio_path)
        assert _duration_seconds(kept) == pytest.approx(3, abs=0.4)
        assert _mean_volume_db(kept) > -40

        res = _post_upload(user, shaped.read_bytes(), "shaped.mp3", {"trim_end": "3"})
        assert res.status_code == 202
        rid = res.get_json()["id"]
        assert _run_convert_job().status == "completed"
        head = _local_path(db.session.get(Recording, rid).audio_path)
        assert _duration_seconds(head) == pytest.approx(3, abs=0.25)
        assert _mean_volume_db(head) < -60


def _mean_volume_db(path):
    probe = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    for line in probe.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0].strip())
    raise AssertionError(probe.stderr)


@pytest.mark.skipif(SAMPLE_ROOT is None, reason="test-audio-files is not beside this checkout")
def test_a_join_encode_keeps_each_span_and_only_audio_from_a_video(tmp_path):
    from src.services.recording_merge import _concat_audio
    speech = tmp_path / "speech.mp3"
    video = tmp_path / "picture.mp4"
    _clip_copy(SAMPLE_ROOT / "samples/zh-hello-mr-lin-i-got-it.mp3", speech)
    _clip_copy(SAMPLE_ROOT / "format-matrix/fr-mono.mp4", video)
    out = tmp_path / "joined.m4a"
    with app.app_context():
        _concat_audio([str(speech), str(video)], str(out), spans=[(1, 4), (2, 5)])
    assert _duration_seconds(out) == pytest.approx(6, abs=0.45)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True)
    assert probe.stdout.strip() == ""


def test_the_upload_size_limit_judges_the_uploaded_file(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    previous = app.config.get("MAX_CONTENT_LENGTH")
    app.config["MAX_CONTENT_LENGTH"] = len(payload) - 1
    try:
        with app.app_context():
            res = _post_upload(user, payload, "two.mp3", {"trim_end": "1"})
            assert res.status_code == 413
            assert Recording.query.filter_by(user_id=user).count() == 0
    finally:
        app.config["MAX_CONTENT_LENGTH"] = previous
