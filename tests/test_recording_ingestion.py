"""Steps of upload ingestion (src/services/recording_ingestion.py).

lookup_duplicate hashes the staged upload and finds an earlier recording of
it; handle_duplicate_replay answers a retried upload with that recording.
resolve_size_policy, enforce_size_policy and recheck_size_after_probe set
and apply the upload size limits; probe_upload and has_video_after_failed_probe
read the upload's codecs.

SHARED-DB: the user, recordings and jobs are removed afterwards.
"""

import hashlib
import io
import os
import sys
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.exceptions import RequestEntityTooLarge

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User
from src.services import recording_ingestion
from src.services.recording_ingestion import (
    SizePolicy, SizeRecheck, UploadProbe, enforce_size_policy, handle_duplicate_replay,
    has_video_after_failed_probe, lookup_duplicate, probe_upload, recheck_size_after_probe,
    resolve_size_policy,
)
from src.utils.ffprobe import FFProbeError, FFProbeTimeoutError

PAYLOAD = b"duplicate-detection-bytes"
PAYLOAD_HASH = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def owner():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"ing1_{s}", email=f"ing1_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        ProcessingJob.query.filter_by(user_id=uid).delete()
        Recording.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


@pytest.fixture
def staged(tmp_path):
    path = tmp_path / "staged_upload.wav"
    path.write_bytes(PAYLOAD)
    return str(path)


def _recording(uid, **fields):
    values = dict(user_id=uid, title="Earlier upload", status="COMPLETED",
                  original_filename="a.wav", file_hash=PAYLOAD_HASH, processing_source="upload")
    values.update(fields)
    rec = Recording(**values)
    db.session.add(rec)
    db.session.commit()
    return rec


def _lookup(uid, staged, form=None, reuse_duplicate=False, **kwargs):
    args = dict(processing_source="upload", original_filename="a.wav")
    args.update(kwargs)
    return lookup_duplicate(owner=db.session.get(User, uid), filepath=staged, form=form or {},
                            reuse_duplicate=reuse_duplicate, **args)


# --- lookup_duplicate ---------------------------------------------------------

def test_lookup_leaves_everything_none_when_hashing_fails(owner, staged):
    with app.app_context():
        _recording(owner)
        with patch("src.utils.file_hash.compute_file_sha256", side_effect=OSError("unreadable")), \
                patch.object(app.logger, "warning") as warning:
            result = _lookup(owner, staged)
        assert result == (None, None, None)
        warning.assert_called_once_with("Could not compute file hash or check duplicates: unreadable")


def test_lookup_keeps_the_hash_when_the_query_fails(owner, staged):
    with app.app_context():
        with patch.object(Recording, "query") as query, patch.object(app.logger, "warning") as warning:
            query.filter_by.side_effect = RuntimeError("db gone")
            result = _lookup(owner, staged)
        assert result == (PAYLOAD_HASH, None, None)
        warning.assert_called_once_with("Could not compute file hash or check duplicates: db gone")


def test_lookup_without_a_match(owner, staged):
    with app.app_context():
        _recording(owner, file_hash="0" * 64)
        result = _lookup(owner, staged)
        assert result == (PAYLOAD_HASH, None, None)


def test_lookup_matches_on_hash_and_warns(owner, staged):
    with app.app_context():
        rec = _recording(owner, created_at=datetime(2026, 1, 2, 3, 4, 5))
        result = _lookup(owner, staged, processing_source="api_v1", original_filename="other.wav")
        assert result.file_hash == PAYLOAD_HASH
        assert result.existing.id == rec.id
        assert result.duplicate_warning == {
            "existing_recording_id": rec.id,
            "existing_title": "Earlier upload",
            "existing_created_at": "2026-01-02T03:04:05",
        }


def test_lookup_ignores_another_users_recording(owner, staged):
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        other = User(username=f"ing1o_{s}", email=f"ing1o_{s}@local.test", password="x")
        db.session.add(other)
        db.session.commit()
        try:
            _recording(other.id)
            assert _lookup(owner, staged) == (PAYLOAD_HASH, None, None)
        finally:
            Recording.query.filter_by(user_id=other.id).delete()
            db.session.delete(other)
            db.session.commit()


def test_reuse_duplicate_matches_source_filename_notes_and_meeting_date(owner, staged):
    with app.app_context():
        rec = _recording(owner, processing_source="api_v1", notes="n1",
                         meeting_date=datetime(2026, 3, 4, 5, 6, 7),
                         created_at=datetime.utcnow() - timedelta(days=30))
        form = {"notes": "n1", "meeting_date": "2026-03-04T05:06:07Z"}
        found = _lookup(owner, staged, form, reuse_duplicate=True, processing_source="api_v1")
        assert found.existing.id == rec.id
        assert found.file_hash == PAYLOAD_HASH
        # A replay gets no duplicate warning.
        assert found.duplicate_warning is None

        misses = [
            ({**form, "meeting_date": "2026-03-04T05:06:08Z"}, {}),
            ({**form, "notes": "n2"}, {}),
            (form, {"original_filename": "b.wav"}),
            (form, {"processing_source": "upload"}),
        ]
        for miss_form, overrides in misses:
            args = {"processing_source": "api_v1", **overrides}
            assert _lookup(owner, staged, miss_form, reuse_duplicate=True, **args) == (PAYLOAD_HASH, None, None)


def test_reuse_duplicate_without_a_meeting_date_matches_only_the_last_day(owner, staged):
    with app.app_context():
        old = _recording(owner, created_at=datetime.utcnow() - timedelta(days=2))
        assert _lookup(owner, staged, {}, reuse_duplicate=True) == (PAYLOAD_HASH, None, None)
        # An unparsable meeting date counts as none.
        assert _lookup(owner, staged, {"meeting_date": "not a date"},
                       reuse_duplicate=True) == (PAYLOAD_HASH, None, None)
        old.created_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        assert _lookup(owner, staged, {"meeting_date": "not a date"}, reuse_duplicate=True).existing.id == old.id


# --- handle_duplicate_replay ----------------------------------------------------

def _replay(uid, rec, staged, success_status=200):
    return handle_duplicate_replay(rec, owner=db.session.get(User, uid), filepath=staged,
                                   file_hash=PAYLOAD_HASH, success_status=success_status)


def test_replay_answers_with_the_existing_recording(owner, staged):
    with app.app_context():
        rec = _recording(owner, status="COMPLETED")
        with patch.object(recording_ingestion.job_queue, "enqueue") as enqueue, \
                patch.object(app.logger, "info") as info:
            response, status = _replay(owner, rec, staged, success_status=201)
        assert status == 201
        body = response.get_json()
        assert body["id"] == rec.id and body["idempotent_replay"] is True
        assert not os.path.exists(staged)
        enqueue.assert_not_called()
        info.assert_any_call(f"Idempotent upload replay for user {owner}: "
                             f"hash={PAYLOAD_HASH[:12]}... matches recording {rec.id}")


@pytest.mark.parametrize("status", ["PENDING", "QUEUED"])
def test_replay_queues_a_lost_transcription(owner, staged, status):
    with app.app_context():
        rec = _recording(owner, status=status)
        with patch.object(recording_ingestion, "resolve_transcription_params",
                          return_value={"language": "en"}) as resolve, \
                patch.object(recording_ingestion.job_queue, "enqueue") as enqueue:
            response, code = _replay(owner, rec, staged)
        assert code == 200 and response.get_json()["idempotent_replay"] is True
        resolve.assert_called_once_with(rec)
        enqueue.assert_called_once_with(user_id=owner, recording_id=rec.id, job_type="transcribe",
                                        params={"language": "en"}, is_new_upload=True)


@pytest.mark.parametrize("job_type", ["transcribe", "convert"])
def test_replay_leaves_a_queued_job_alone(owner, staged, job_type):
    with app.app_context():
        rec = _recording(owner, status="QUEUED")
        db.session.add(ProcessingJob(user_id=owner, recording_id=rec.id, job_type=job_type))
        db.session.commit()
        with patch.object(recording_ingestion.job_queue, "enqueue") as enqueue:
            response, code = _replay(owner, rec, staged)
        assert code == 200 and response.get_json()["id"] == rec.id
        enqueue.assert_not_called()


def test_replay_removes_staging_before_enqueueing(owner, staged):
    with app.app_context():
        rec = _recording(owner, status="PENDING")
        seen = []
        with patch.object(recording_ingestion.job_queue, "enqueue",
                          side_effect=lambda **kw: seen.append(os.path.exists(staged))):
            _replay(owner, rec, staged)
        assert seen == [False]


# --- size policy ----------------------------------------------------------------

MB = 1024 * 1024


def _settings(**values):
    return lambda key, default=None: values.get(key, default)


def _policy(filename="talk.wav", size=3 * MB, form=None, enforce_upload_limits=True,
            settings=None, chunking=None, retention=False, max_content_length=None):
    upload = io.BytesIO(b"x" * size)
    upload.seek(5)
    with patch("src.api.recordings.SystemSetting.get_setting",
               side_effect=_settings(**(settings or {}))), \
            patch("src.api.recordings.VIDEO_RETENTION", retention), \
            patch("src.api.recordings.USE_NEW_TRANSCRIPTION_ARCHITECTURE", False), \
            patch("src.api.recordings.chunking_service", MagicMock() if chunking else None), \
            patch("src.audio_chunking.get_effective_chunking_config", return_value=chunking), \
            patch.dict(app.config, {"MAX_CONTENT_LENGTH": max_content_length}):
        policy = resolve_size_policy(file=upload, form=form or {}, original_filename=filename,
                                     enforce_upload_limits=enforce_upload_limits)
    assert upload.tell() == 0
    return policy


def test_size_policy_for_an_audio_file():
    with app.app_context():
        policy = _policy(settings={"max_file_size_mb": 10}, max_content_length=500 * MB)
    assert policy == SizePolicy(
        keep_audio_only_flag=False, effective_audio_only=True, is_likely_video_by_ext=False,
        original_file_size=3 * MB, regular_limit_mb=10, audio_only_limit_mb=40,
        effective_limit_mb=10, effective_limit_bytes=10 * MB, max_content_length=500 * MB,
        connector_specs=None, should_enforce_size_limit=True,
    )


def test_size_policy_gives_a_video_extension_the_video_limit():
    with app.app_context():
        policy = _policy("talk.MOV", settings={"max_file_size_mb": 10, "max_audio_only_video_size_mb": 25})
    assert policy.is_likely_video_by_ext is True
    assert (policy.regular_limit_mb, policy.audio_only_limit_mb) == (10, 25)
    assert (policy.effective_limit_mb, policy.effective_limit_bytes) == (25, 25 * MB)


@pytest.mark.parametrize("form,retention,expected", [
    ({}, False, (False, True)),
    ({}, True, (False, False)),
    ({"keep_audio_only": "TRUE"}, True, (True, True)),
])
def test_size_policy_reads_keep_audio_only(form, retention, expected):
    with app.app_context():
        policy = _policy("talk.mp4", form=form, retention=retention)
    assert (policy.keep_audio_only_flag, policy.effective_audio_only) == expected


@pytest.mark.parametrize("config,message", [
    (SimpleNamespace(enabled=True, source="app", mode="size", limit_value=20),
     "Size-based chunking enabled (20MB, source=app) - skipping 3.0MB size limit check"),
    (SimpleNamespace(enabled=True, source="env", mode="duration", limit_value=600),
     "Duration-based chunking enabled (600s, source=env) - skipping 3.0MB size limit check"),
    (SimpleNamespace(enabled=False, source="connector_internal", mode="size", limit_value=0),
     "Connector handles chunking internally - skipping 3.0MB size limit check"),
])
def test_chunking_lifts_the_size_limit(config, message):
    with app.app_context(), patch.object(app.logger, "info") as info:
        policy = _policy(chunking=config)
    assert policy.should_enforce_size_limit is False
    info.assert_called_once_with(message)


def test_size_limit_stays_when_chunking_is_off():
    off = SimpleNamespace(enabled=False, source="app", mode="size", limit_value=20)
    with app.app_context():
        assert _policy(chunking=off).should_enforce_size_limit is True
        # Without enforce_upload_limits (watched files) nothing is enforced.
        assert _policy(enforce_upload_limits=False).should_enforce_size_limit is False


def test_size_policy_reads_the_connector_specs():
    specs = object()
    registry = MagicMock()
    registry.get_active_connector.return_value.specifications = specs
    with app.app_context(), patch("src.api.recordings.USE_NEW_TRANSCRIPTION_ARCHITECTURE", True), \
            patch("src.services.transcription.get_registry", return_value=registry), \
            patch("src.api.recordings.chunking_service", None):
        policy = resolve_size_policy(file=io.BytesIO(b"abc"), form={}, original_filename="a.wav",
                                     enforce_upload_limits=True)
    assert policy.connector_specs is specs


def test_size_policy_logs_a_connector_lookup_failure():
    with app.app_context(), patch("src.api.recordings.USE_NEW_TRANSCRIPTION_ARCHITECTURE", True), \
            patch("src.services.transcription.get_registry", side_effect=RuntimeError("no registry")), \
            patch("src.api.recordings.chunking_service", None), \
            patch.object(app.logger, "warning") as warning:
        policy = resolve_size_policy(file=io.BytesIO(b"abc"), form={}, original_filename="a.wav",
                                     enforce_upload_limits=True)
    assert policy.connector_specs is None
    warning.assert_called_once_with("Could not get connector specs for upload: no registry")


def _limits(size, limit_mb=10, enforce=True, ceiling=None):
    return SizePolicy(False, True, False, size, limit_mb, limit_mb * 4, limit_mb, limit_mb * MB,
                      ceiling, None, enforce)


def test_enforce_size_policy_applies_the_per_type_limit():
    enforce_size_policy(_limits(10 * MB), enforce_upload_limits=True)
    with pytest.raises(RequestEntityTooLarge):
        enforce_size_policy(_limits(10 * MB + 1), enforce_upload_limits=True)
    # Chunking lifts it.
    enforce_size_policy(_limits(10 * MB + 1, enforce=False), enforce_upload_limits=True)


def test_enforce_size_policy_applies_the_request_ceiling_whatever_the_chunking():
    with pytest.raises(RequestEntityTooLarge):
        enforce_size_policy(_limits(21 * MB, enforce=False, ceiling=20 * MB), enforce_upload_limits=True)
    enforce_size_policy(_limits(20 * MB, enforce=False, ceiling=20 * MB), enforce_upload_limits=True)
    # Watched files are not HTTP requests.
    enforce_size_policy(_limits(21 * MB, enforce=False, ceiling=20 * MB), enforce_upload_limits=False)


def _recheck(codec_info, has_video=False, filename="talk.mp4", size=11 * MB, enforce=True,
             retention=False, passthrough=False):
    with patch("src.api.recordings.VIDEO_RETENTION", retention), \
            patch("src.api.recordings.VIDEO_PASSTHROUGH_ASR", passthrough):
        return recheck_size_after_probe(
            codec_info=codec_info, has_video=has_video, original_filename=filename,
            is_likely_video_by_ext=filename.endswith(".mp4"), effective_limit_mb=40,
            regular_limit_mb=10, should_enforce_size_limit=enforce, original_file_size=size,
        )


def test_recheck_gives_an_audio_only_probe_the_regular_limit():
    audio = {"audio_codec": "aac", "has_video": False}
    assert _recheck(audio) == SizeRecheck(False, 10, True)
    assert _recheck(audio, size=10 * MB) == SizeRecheck(False, 40, False)
    assert _recheck(audio, enforce=False) == SizeRecheck(False, 40, False)
    # A probed video keeps the video limit.
    assert _recheck({"has_video": True}, has_video=True) == SizeRecheck(True, 40, False)


@pytest.mark.parametrize("retention,passthrough", [(True, False), (False, True)])
def test_recheck_refuses_a_large_unreadable_file_with_a_video_extension(retention, passthrough):
    assert _recheck(None, retention=retention, passthrough=passthrough) == SizeRecheck(False, 10, True)
    small = _recheck(None, size=10 * MB, retention=retention, passthrough=passthrough)
    assert small == SizeRecheck(True, 40, False)
    assert _recheck(None, enforce=False, retention=retention, passthrough=passthrough) == SizeRecheck(True, 40, False)


def test_recheck_leaves_an_unreadable_file_alone_without_video_settings():
    assert _recheck(None) == SizeRecheck(True, 40, False)
    assert _recheck(None, filename="talk.wav", retention=True) == SizeRecheck(False, 40, False)


# --- probe ----------------------------------------------------------------------

def _probe(staged, filename, codec=None, error=None, repaired=None, size=None):
    get_info = MagicMock(return_value=codec, side_effect=error)
    repair = MagicMock(return_value=repaired)
    with patch("src.api.recordings.get_codec_info", get_info), \
            patch("src.api.recordings.try_repair_malformed_webm", repair), \
            patch("os.path.getsize", return_value=size if size is not None else 3 * MB):
        result = probe_upload(filepath=staged, original_filename=filename)
    return result, get_info, repair


def test_probe_upload_reads_the_codecs(staged):
    codec = {"audio_codec": "aac", "has_video": True}
    with app.app_context(), patch.object(app.logger, "info") as info:
        result, get_info, repair = _probe(staged, "talk.mp4", codec=codec)
    assert result == UploadProbe(codec, None, True)
    get_info.assert_called_once_with(staged, timeout=10)
    repair.assert_not_called()
    info.assert_called_once_with("Detected codec for talk.mp4: audio_codec=aac, has_video=True")


@pytest.mark.parametrize("size_mb,timeout", [(50, 10), (250, 25), (900, 60)])
def test_probe_timeout_scales_with_the_file_size(staged, size_mb, timeout):
    with app.app_context():
        _, get_info, _ = _probe(staged, "a.wav", codec={"audio_codec": "pcm_s16le"}, size=size_mb * MB)
    get_info.assert_called_once_with(staged, timeout=timeout)


def test_probe_upload_keeps_a_failed_probe(staged):
    error = FFProbeError("unreadable")
    with app.app_context(), patch.object(app.logger, "warning") as warning:
        result, _, repair = _probe(staged, "talk.mp3", error=error)
    assert result == UploadProbe(None, error, False)
    repair.assert_not_called()
    warning.assert_called_once_with(
        "Failed to probe talk.mp3 (timeout=10s): unreadable. Will attempt conversion.")


@pytest.mark.parametrize("filename", ["rec.webm", "rec.MKV", "rec.mka"])
def test_probe_upload_repairs_webm(staged, filename):
    repaired = {"audio_codec": "opus", "has_video": False}
    with app.app_context(), patch.object(app.logger, "info") as info:
        result, _, repair = _probe(staged, filename, error=FFProbeError("EBML header not found"),
                                   repaired=repaired)
    assert result == UploadProbe(repaired, None, False)
    repair.assert_called_once_with(staged, timeout=10)
    info.assert_called_once_with(
        f"Recovered malformed WebM header for {filename}; probe succeeded after trim")


def test_probe_upload_keeps_the_error_when_the_repair_fails(staged):
    error = FFProbeError("EBML header not found")
    with app.app_context():
        result, _, repair = _probe(staged, "rec.webm", error=error, repaired=None)
    assert result == UploadProbe(None, error, False)
    repair.assert_called_once_with(staged, timeout=10)


def _fallback(filename="talk.mp4", codec_info=None, error=None, has_video=False,
              retention=False, passthrough=False):
    with patch("src.api.recordings.VIDEO_RETENTION", retention), \
            patch("src.api.recordings.VIDEO_PASSTHROUGH_ASR", passthrough):
        return has_video_after_failed_probe(codec_info=codec_info, probe_error=error,
                                            has_video=has_video, original_filename=filename)


@pytest.mark.parametrize("retention,passthrough,label", [
    (True, False, "VIDEO_RETENTION"), (False, True, "VIDEO_PASSTHROUGH_ASR"), (True, True, "VIDEO_PASSTHROUGH_ASR"),
])
def test_unfinished_probe_takes_a_video_extension_as_video(retention, passthrough, label):
    with app.app_context(), patch.object(app.logger, "info") as info:
        assert _fallback(error=FFProbeTimeoutError("slow"), retention=retention, passthrough=passthrough) is True
    info.assert_called_once_with(
        f"Probe failed but file extension '.mp4' indicates video — treating as video for {label}")


def test_rejecting_probe_is_not_overruled_by_the_extension():
    with app.app_context(), patch.object(app.logger, "info") as info:
        assert _fallback("talk.MOV", error=FFProbeError("invalid data"), retention=True) is False
    info.assert_called_once_with("ffprobe could not read '.mov' upload; converting it as audio")


def test_extension_fallback_needs_a_failed_probe_a_video_extension_and_a_video_setting():
    with app.app_context(), patch.object(app.logger, "info") as info:
        assert _fallback(error=FFProbeTimeoutError("slow")) is False
        assert _fallback("talk.wav", error=FFProbeTimeoutError("slow"), retention=True) is False
        assert _fallback(codec_info={"has_video": False}, retention=True) is False
        assert _fallback(codec_info={"has_video": True}, has_video=True, retention=True) is True
    info.assert_not_called()
