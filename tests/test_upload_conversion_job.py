"""Uploads are converted after the upload is answered, by a convert job (#543).

The real ffprobe and ffmpeg binaries and the real local storage are used, so
these tests run each step as it runs in production: the upload stores the
file as it arrived, the convert job replaces it with the converted audio and
queues the transcription.
"""
import io
import json
import os
import shutil
import subprocess
import uuid
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import CONVERT_JOBS, job_queue
from src.services.storage import get_storage_service
from src.utils.ffmpeg_utils import AUDIO_DECODE_FAILED_MESSAGE, FFmpegError, FFmpegTimeoutError
from src.utils.localization import translate

app.config["WTF_CSRF_ENABLED"] = False

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are not installed",
)


def _ffmpeg(path, *args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(path)], check=True, capture_output=True)
    return path.read_bytes()


def _webm(tmp_path):
    """Two seconds of Opus in WebM, as a browser recorder writes it."""
    return _ffmpeg(tmp_path / "in.webm", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libopus")


@pytest.fixture(autouse=True)
def _convert_workers():
    # The test run has no workers; the jobs are run by hand below.
    with patch("src.api.recordings.CONVERT_WORKERS", 1):
        yield


@pytest.fixture
def user():
    job_queue.init_app(app)
    with app.app_context():
        stale = ProcessingJob.query.filter(ProcessingJob.status == 'queued').all()
        for job in stale:
            job.status = 'completed'
        db.session.commit()
        s = uuid.uuid4().hex[:8]
        u = User(username=f"conv_{s}", email=f"conv_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        storage = get_storage_service()
        for rec in Recording.query.filter_by(user_id=uid).all():
            storage.delete(rec.audio_path)
        ProcessingJob.query.filter_by(user_id=uid).delete(synchronize_session=False)
        Recording.query.filter_by(user_id=uid).delete(synchronize_session=False)
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _upload(uid, payload, filename):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return client.post("/upload", data={"file": (io.BytesIO(payload), filename)},
                       content_type="multipart/form-data")


def _jobs(rid):
    return [(j.job_type, j.status) for j in ProcessingJob.query.filter_by(recording_id=rid).order_by(ProcessingJob.id)]


def _local_path(locator):
    storage = get_storage_service()
    with storage.materialize(locator) as materialized:
        return materialized.local_path


def _run_convert_job():
    job = job_queue._claim_next_job(CONVERT_JOBS, 'convert')
    assert job is not None
    job_queue._process_job(job)
    db.session.expire_all()
    return db.session.get(ProcessingJob, job.id)


def test_an_upload_is_answered_before_its_conversion(user, tmp_path):
    with app.app_context():
        res = _upload(user, _webm(tmp_path), "meeting.webm")

        assert res.status_code == 202
        rid = res.get_json()["id"]
        rec = db.session.get(Recording, rid)
        assert rec.status == "QUEUED"
        assert _jobs(rid) == [("convert", "queued")]
        # Stored as it arrived until the job runs.
        assert rec.audio_path.endswith(".webm")
        assert os.path.exists(_local_path(rec.audio_path))


def test_the_convert_job_stores_the_converted_audio_and_queues_the_transcription(user, tmp_path):
    with app.app_context():
        rid = _upload(user, _webm(tmp_path), "meeting.webm").get_json()["id"]
        original = db.session.get(Recording, rid).audio_path
        original_path = _local_path(original)
        convert_params = json.loads(ProcessingJob.query.filter_by(recording_id=rid).one().params)

        assert _run_convert_job().status == "completed"

        rec = db.session.get(Recording, rid)
        assert rec.audio_path != original
        assert not rec.audio_path.endswith(".webm")
        assert not os.path.exists(original_path)
        stored = _local_path(rec.audio_path)
        assert os.path.getsize(stored) == rec.file_size
        assert rec.mime_type.startswith("audio/") and rec.mime_type != "audio/webm"
        assert 1.5 < rec.audio_duration_seconds < 2.5
        assert rec.status == "QUEUED"
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]
        transcribe = ProcessingJob.query.filter_by(recording_id=rid, job_type="transcribe").one()
        assert json.loads(transcribe.params or "{}") == convert_params["transcribe"]
        assert transcribe.is_new_upload


def test_an_mp3_that_needs_no_conversion_keeps_its_file(user, tmp_path):
    with app.app_context():
        payload = _ffmpeg(tmp_path / "in.mp3", "-f", "lavfi", "-i", "sine=duration=2", "-c:a", "libmp3lame")
        rid = _upload(user, payload, "memo.mp3").get_json()["id"]
        original = db.session.get(Recording, rid).audio_path

        _run_convert_job()

        rec = db.session.get(Recording, rid)
        assert rec.audio_path == original
        assert os.path.exists(_local_path(original))
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]


def test_a_file_ffprobe_cannot_read_is_still_refused_at_once(user):
    with app.app_context():
        res = _upload(user, b"This is not audio, only text.\n" * 64, "notes.mp3")

        assert res.status_code == 400
        assert res.get_json()["code"] == "audio_decode_failed"
        assert Recording.query.filter_by(user_id=user).count() == 0


def test_a_video_without_audio_is_still_refused_at_once(user, tmp_path):
    with app.app_context():
        payload = _ffmpeg(tmp_path / "in.mp4", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p")
        res = _upload(user, payload, "silent.mp4")

        assert res.status_code == 400
        assert res.get_json()["code"] == "audio_decode_failed"
        assert Recording.query.filter_by(user_id=user).count() == 0


def _fail_conversion(user, tmp_path, error):
    rid = _upload(user, _webm(tmp_path), "meeting.webm").get_json()["id"]
    original_path = _local_path(db.session.get(Recording, rid).audio_path)
    with patch("src.utils.audio_conversion.convert_if_needed", side_effect=error), \
         patch.object(job_queue, "_emit_failure_webhook") as webhook, \
         patch.object(job_queue, "_emit_failure_email") as email:
        job = _run_convert_job()
    return rid, original_path, job, webhook, email


def test_a_failed_conversion_fails_the_recording_without_retries(user, tmp_path):
    with app.app_context():
        rid, original_path, job, webhook, email = _fail_conversion(
            user, tmp_path, FFmpegError("ffmpeg 7.1 failed on /data/uploads/secret/in.webm"))

        assert (job.status, job.retry_count) == ("failed", 1)
        rec = db.session.get(Recording, rid)
        assert rec.status == "FAILED"
        assert AUDIO_DECODE_FAILED_MESSAGE in rec.transcription
        assert "/data/uploads" not in rec.transcription
        assert _jobs(rid) == [("convert", "failed")]
        # The upload is kept, so the recording can be reprocessed.
        assert os.path.exists(original_path)
        webhook.assert_called_once()
        assert webhook.call_args[0][:2] == ("convert", rid)
        email.assert_called_once()


def test_a_conversion_that_times_out_says_so(user, tmp_path):
    with app.app_context():
        rid, _, job, _, _ = _fail_conversion(user, tmp_path, FFmpegTimeoutError("ran past 600 s"))

        assert (job.status, job.retry_count) == ("failed", 1)
        assert translate("server.apiError.audio_conversion_timeout") in db.session.get(Recording, rid).transcription


def test_extracted_audio_over_the_limit_fails_the_recording(user, tmp_path):
    with app.app_context():
        rid = _upload(user, _webm(tmp_path), "meeting.webm").get_json()["id"]
        job = ProcessingJob.query.filter_by(recording_id=rid).one()
        params = json.loads(job.params)
        params["extracted_audio_limit_mb"] = 0.000001
        job.params = json.dumps(params)
        db.session.commit()

        assert _run_convert_job().status == "failed"
        rec = db.session.get(Recording, rid)
        assert rec.status == "FAILED"
        assert _jobs(rid) == [("convert", "failed")]


def test_a_recording_deleted_during_its_conversion_leaves_no_file(user, tmp_path):
    from src.utils import audio_conversion
    real_convert = audio_conversion.convert_if_needed
    outputs = []

    def convert_then_delete(*args, **kwargs):
        result = real_convert(*args, **kwargs)
        outputs.append(result.output_path)
        Recording.query.filter_by(user_id=user).delete(synchronize_session=False)
        ProcessingJob.query.filter_by(user_id=user).delete(synchronize_session=False)
        db.session.commit()
        return result

    with app.app_context():
        rid = _upload(user, _webm(tmp_path), "meeting.webm").get_json()["id"]
        original_path = _local_path(db.session.get(Recording, rid).audio_path)
        job = job_queue._claim_next_job(CONVERT_JOBS, 'convert')
        storage = get_storage_service()
        with patch("src.utils.audio_conversion.convert_if_needed", side_effect=convert_then_delete), \
             patch.object(storage, "delete", wraps=storage.delete) as delete:
            job_queue._process_job(job)

        assert outputs, db.session.get(ProcessingJob, job.id).error_message
        assert not os.path.exists(outputs[0])
        stored_copy = delete.call_args_list[-1][0][0]
        assert f"/{rid}/" in stored_copy and not stored_copy.endswith(".webm")
        assert not os.path.exists(_local_path(stored_copy))
        assert ProcessingJob.query.filter_by(recording_id=rid, job_type="transcribe").count() == 0
        os.remove(original_path)


def test_the_job_list_shows_the_conversion_in_its_own_queue(user, tmp_path):
    with app.app_context():
        rid = _upload(user, _webm(tmp_path), "meeting.webm").get_json()["id"]
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user)

        jobs = [j for j in client.get("/api/recordings/job-queue-status").get_json()["jobs"]
                if j["recording_id"] == rid]

        assert [(j["job_type"], j["queue_type"], j["position"]) for j in jobs] == [("convert", "convert", 1)]


def test_without_convert_workers_the_upload_converts_the_file_itself(user, tmp_path):
    with app.app_context(), patch("src.api.recordings.CONVERT_WORKERS", 0):
        res = _upload(user, _webm(tmp_path), "meeting.webm")

        rid = res.get_json()["id"]
        # The stored file keeps the uploaded name in this path; its type shows the conversion.
        assert db.session.get(Recording, rid).mime_type != "audio/webm"
        assert _jobs(rid) == [("transcribe", "queued")]
