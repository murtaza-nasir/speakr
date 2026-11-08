"""Steps of upload ingestion (src/services/recording_ingestion.py).

lookup_duplicate hashes the staged upload and finds an earlier recording of
it; handle_duplicate_replay answers a retried upload with that recording.
resolve_size_policy, enforce_size_policy and recheck_size_after_probe set
and apply the upload size limits; probe_upload and has_video_after_failed_probe
read the upload's codecs. decide_conversion, convert_upload_in_request and
check_extracted_audio_size decide on and run the conversion.
select_tags_and_folder, store_recording, handle_unique_conflict and
build_ingestion_response write the recording and assemble the answer;
enqueue_ingestion_jobs queues its first job. ingest runs them all inside the
outer error handling; ingest_uploaded_recording is its facade.

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
from src.models import (
    Folder, Group, GroupMembership, ProcessingJob, Recording, RecordingTag, Tag, User,
)
from src.services import recording_ingestion
from src.services.recording_ingestion import (
    ConversionDecision, ConvertedUpload, SizePolicy, SizeRecheck, StoredRecording,
    TagFolderSelection, UploadProbe, build_ingestion_response, check_extracted_audio_size,
    convert_upload_in_request, decide_conversion, enforce_size_policy, enqueue_ingestion_jobs,
    handle_duplicate_replay,
    handle_unique_conflict, has_video_after_failed_probe, ingest, lookup_duplicate, probe_upload,
    recheck_size_after_probe, resolve_size_policy, select_tags_and_folder, store_recording,
)
from src.utils.ffmpeg_utils import FFmpegError, FFmpegNotFoundError, FFmpegTimeoutError
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


# --- conversion decision ----------------------------------------------------------

AUDIO = {"audio_codec": "aac", "has_video": False}
VIDEO = {"audio_codec": "aac", "has_video": True}
SILENT_VIDEO = {"audio_codec": None, "has_video": True}


def _decide(convert_workers=1, retention=False, passthrough=False, chunking=False, **overrides):
    args = dict(codec_info=AUDIO, has_video=False, keep_audio_only_flag=False, join=None,
                original_filename="talk.m4a", should_enforce_size_limit=True, enforce_upload_limits=True,
                effective_audio_only=True, is_likely_video_by_ext=False, regular_limit_mb=10,
                original_file_size=3 * MB)
    args.update(overrides)
    with patch("src.api.recordings.CONVERT_WORKERS", convert_workers), \
            patch("src.api.recordings.VIDEO_RETENTION", retention), \
            patch("src.api.recordings.VIDEO_PASSTHROUGH_ASR", passthrough), \
            patch("src.api.recordings.chunking_service", MagicMock() if chunking else None):
        return decide_conversion(**args)


def test_decide_conversion_defers_when_probe_succeeds():
    with app.app_context(), patch.object(app.logger, "info") as info:
        decision = _decide()
    assert decision == ConversionDecision(
        keep_video_for_this_upload=False, skip_conversion_for_join=False,
        chunking_will_handle_large_audio=False, extracted_audio_limit_mb=None,
        can_defer_conversion=True, conversion_deferred=True, convert_in_request=False,
    )
    info.assert_called_once_with("Upload talk.m4a: converting after the upload is answered")


@pytest.mark.parametrize("overrides", [
    {"convert_workers": 0},
    {"codec_info": None},
    {"codec_info": SILENT_VIDEO, "has_video": True},
    # The extracted audio of a large video is checked here, not by the convert job.
    {"is_likely_video_by_ext": True, "original_file_size": 11 * MB},
])
def test_decide_conversion_converts_in_the_request(overrides):
    with app.app_context(), patch.object(app.logger, "info") as info:
        decision = _decide(**overrides)
    assert (decision.can_defer_conversion, decision.conversion_deferred, decision.convert_in_request) == \
        (False, False, True)
    info.assert_not_called()


@pytest.mark.parametrize("retention,passthrough,keep_audio_only,label", [
    (True, False, False, "retention"),
    (False, True, False, "passthrough"),
    (True, True, True, "passthrough"),
])
def test_decide_conversion_keeps_a_video(retention, passthrough, keep_audio_only, label):
    with app.app_context(), patch.object(app.logger, "info") as info:
        decision = _decide(retention=retention, passthrough=passthrough, codec_info=VIDEO, has_video=True,
                           keep_audio_only_flag=keep_audio_only, original_filename="talk.mp4")
    assert decision.keep_video_for_this_upload is True
    assert (decision.conversion_deferred, decision.convert_in_request) == (False, False)
    info.assert_called_once_with(f"Video {label}: keeping original video, skipping conversion")


def test_decide_conversion_keeps_no_video_without_audio_or_with_keep_audio_only():
    with app.app_context():
        assert _decide(retention=True, codec_info=SILENT_VIDEO, has_video=True).keep_video_for_this_upload is False
        assert _decide(retention=True, codec_info=VIDEO, has_video=True,
                       keep_audio_only_flag=True).keep_video_for_this_upload is False
        # A video the probe could not read but its extension showed (has_video) is kept.
        assert _decide(retention=True, codec_info=None, has_video=True).keep_video_for_this_upload is True


def test_decide_conversion_skips_an_audio_join_part():
    with app.app_context(), patch.object(app.logger, "info") as info:
        decision = _decide(join=("group", 0, 2))
    assert decision.skip_conversion_for_join is True
    assert (decision.can_defer_conversion, decision.conversion_deferred, decision.convert_in_request) == \
        (False, False, False)
    info.assert_called_once_with("Join part talk.m4a: skipping conversion, the merge re-encodes it")
    with app.app_context():
        video_part = _decide(join=("group", 0, 2), codec_info=VIDEO, has_video=True)
    assert (video_part.skip_conversion_for_join, video_part.convert_in_request) == (False, True)


def test_extracted_audio_limit_applies_to_an_audio_only_video_without_chunking():
    video = dict(is_likely_video_by_ext=True, original_filename="talk.mp4")
    with app.app_context():
        assert _decide(**video).extracted_audio_limit_mb == 10
        assert _decide(**video, effective_audio_only=False).extracted_audio_limit_mb is None
        assert _decide(**video, enforce_upload_limits=False).extracted_audio_limit_mb is None
        assert _decide().extracted_audio_limit_mb is None
        chunked = _decide(**video, chunking=True, should_enforce_size_limit=False)
    assert (chunked.chunking_will_handle_large_audio, chunked.extracted_audio_limit_mb) == (True, None)


def _convert(staged, outcome):
    convert = MagicMock(side_effect=outcome) if isinstance(outcome, Exception) else MagicMock(return_value=outcome)
    specs = object()
    with patch("src.api.recordings.convert_if_needed", convert), \
            patch("src.api.recordings.USE_ASR_ENDPOINT", True):
        converted = convert_upload_in_request(staged, original_filename="talk.m4a", codec_info=AUDIO,
                                              needs_chunking_for_processing=False, connector_specs=specs)
    convert.assert_called_once_with(staged, original_filename="talk.m4a", codec_info=AUDIO,
                                    needs_chunking=False, is_asr_endpoint=True, delete_original=True,
                                    connector_specs=specs)
    return converted


def test_convert_upload_in_request_returns_the_converted_path(staged):
    result = SimpleNamespace(output_path="/staging/talk.mp3", was_converted=True, was_compressed=False,
                             original_codec="alac", final_codec="mp3")
    with app.app_context(), patch.object(app.logger, "info") as info:
        converted = _convert(staged, result)
    assert converted == ConvertedUpload("/staging/talk.mp3", None)
    info.assert_called_once_with("File converted: alac -> mp3")


@pytest.mark.parametrize("error,status,code", [
    (FFmpegNotFoundError("no ffmpeg"), 500, "audio_converter_missing"),
    (FFmpegTimeoutError("too slow"), 500, "audio_conversion_timeout"),
    (FFmpegError("bad data"), 400, "audio_decode_failed"),
])
def test_immediate_conversion_errors_remove_staging_and_answer(staged, error, status, code):
    with app.app_context(), patch.object(app.logger, "error") as log_error:
        converted = _convert(staged, error)
        response, got_status = converted.error_response
        assert converted.filepath is None
        assert got_status == status
        assert response.get_json()["code"] == code
    assert not os.path.exists(staged)
    log_error.assert_called_once()


def _guard(staged, final_mb, limit=10, deferred=False):
    return check_extracted_audio_size(filepath=staged, final_file_size=int(final_mb * MB),
                                      extracted_audio_limit_mb=limit, conversion_deferred=deferred,
                                      regular_limit_mb=10)


def test_post_extraction_size_guard_returns_413(staged):
    with app.app_context(), patch.object(app.logger, "warning") as warning:
        response, status = _guard(staged, 12.5)
        body = response.get_json()
    assert status == 413
    assert body["code"] == "extracted_audio_too_large"
    assert {k: body[k] for k in ("max_size_mb", "extracted_audio_mb", "audio_only_mode", "chunking_enabled")} == \
        {"max_size_mb": 10.0, "extracted_audio_mb": 12.5, "audio_only_mode": True, "chunking_enabled": False}
    assert not os.path.exists(staged)
    warning.assert_called_once_with("Audio-only extraction left 12.5MB of audio, which exceeds regular "
                                    "limit 10MB and chunking is off. Rejecting upload.")


def test_post_extraction_size_guard_lets_the_rest_through(staged):
    with app.app_context():
        assert _guard(staged, 10) is None
        assert _guard(staged, 12.5, limit=None) is None
        # The convert job checks a deferred conversion.
        assert _guard(staged, 12.5, deferred=True) is None
    assert os.path.exists(staged)


# --- select_tags_and_folder ---------------------------------------------------------

@pytest.fixture
def tagworld(owner):
    """The owner, another user, a group the owner is in and one the owner is not in."""
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        other = User(username=f"ing3o_{s}", email=f"ing3o_{s}@local.test", password="x")
        member_of = Group(name=f"ing3g_{s}")
        not_in = Group(name=f"ing3n_{s}")
        db.session.add_all([other, member_of, not_in])
        db.session.flush()
        db.session.add(GroupMembership(group_id=member_of.id, user_id=owner))
        db.session.add(GroupMembership(group_id=not_in.id, user_id=other.id))
        ids = SimpleNamespace(owner=owner, other=other.id, group=member_of.id, foreign_group=not_in.id)

        def tag(name, user_id, group_id=None):
            t = Tag(name=name, user_id=user_id, group_id=group_id)
            db.session.add(t)
            db.session.flush()
            return t.id

        def folder(name, user_id, group_id=None):
            f = Folder(name=name, user_id=user_id, group_id=group_id)
            db.session.add(f)
            db.session.flush()
            return f.id

        ids.own_tag = tag("own", owner)
        ids.own_tag2 = tag("own2", owner)
        ids.others_tag = tag("others", other.id)
        ids.group_tag = tag("group", other.id, member_of.id)
        ids.foreign_group_tag = tag("foreign", other.id, not_in.id)
        ids.own_folder = folder("own", owner)
        ids.others_folder = folder("others", other.id)
        ids.group_folder = folder("group", other.id, member_of.id)
        ids.foreign_group_folder = folder("foreign", other.id, not_in.id)
        db.session.commit()
    yield ids
    with app.app_context():
        rec_ids = [r.id for r in Recording.query.filter_by(user_id=owner)]
        if rec_ids:
            RecordingTag.query.filter(RecordingTag.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        Recording.query.filter_by(user_id=owner).update({"folder_id": None})
        Tag.query.filter(Tag.user_id.in_([owner, ids.other])).delete(synchronize_session=False)
        Folder.query.filter(Folder.user_id.in_([owner, ids.other])).delete(synchronize_session=False)
        GroupMembership.query.filter(GroupMembership.group_id.in_([ids.group, ids.foreign_group])).delete(
            synchronize_session=False)
        Group.query.filter(Group.id.in_([ids.group, ids.foreign_group])).delete(synchronize_session=False)
        db.session.delete(db.session.get(User, ids.other))
        db.session.commit()


def _select(uid, form):
    selection = select_tags_and_folder(form=form, owner=db.session.get(User, uid))
    assert isinstance(selection, TagFolderSelection)
    return [t.id for t in selection.selected_tags], (
        selection.selected_folder.id if selection.selected_folder else None)


def test_select_keeps_own_and_member_group_tags_in_order(tagworld):
    w = tagworld
    with app.app_context():
        form = {"tag_ids[0]": str(w.group_tag), "tag_ids[1]": str(w.others_tag),
                "tag_ids[2]": str(w.own_tag), "tag_ids[3]": str(w.foreign_group_tag),
                "tag_ids[4]": "999999999", "tag_ids[5]": str(w.own_tag2)}
        assert _select(w.owner, form) == ([w.group_tag, w.own_tag, w.own_tag2], None)


def test_select_stops_at_the_first_missing_index(tagworld):
    w = tagworld
    with app.app_context():
        form = {"tag_ids[0]": str(w.own_tag), "tag_ids[2]": str(w.own_tag2)}
        assert _select(w.owner, form) == ([w.own_tag], None)
        assert _select(w.owner, {"tag_ids[0]": "", "tag_ids[1]": str(w.own_tag)}) == ([], None)


def test_select_reads_tag_id_only_when_no_tag_was_accepted(tagworld):
    w = tagworld
    with app.app_context():
        assert _select(w.owner, {"tag_id": str(w.own_tag2)}) == ([w.own_tag2], None)
        assert _select(w.owner, {"tag_id": str(w.group_tag)}) == ([w.group_tag], None)
        assert _select(w.owner, {"tag_id": str(w.others_tag)}) == ([], None)
        # A refused tag_ids entry still lets tag_id through; an accepted one does not.
        assert _select(w.owner, {"tag_ids[0]": str(w.others_tag), "tag_id": str(w.own_tag2)}) == \
            ([w.own_tag2], None)
        assert _select(w.owner, {"tag_ids[0]": str(w.own_tag), "tag_id": str(w.own_tag2)}) == \
            ([w.own_tag], None)


def test_select_keeps_own_and_member_group_folders(tagworld):
    w = tagworld
    with app.app_context():
        assert _select(w.owner, {"folder_id": str(w.own_folder)}) == ([], w.own_folder)
        assert _select(w.owner, {"folder_id": str(w.group_folder)}) == ([], w.group_folder)
        assert _select(w.owner, {"folder_id": str(w.others_folder)}) == ([], None)
        assert _select(w.owner, {"folder_id": str(w.foreign_group_folder)}) == ([], None)
        assert _select(w.owner, {"folder_id": "999999999"}) == ([], None)
        assert _select(w.owner, {}) == ([], None)


# --- store_recording ------------------------------------------------------------------

class _Storage:
    """Moves the staged file away like delete_source=True, and records each call."""

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.calls = []

    def build_recording_key(self, original_filename, recording_id, *, now):
        self.calls.append(("key", original_filename, recording_id, now))
        return f"recordings/{recording_id}/{original_filename}"

    def upload_local_file(self, filepath, key, *, content_type, delete_source):
        self.events.append("upload")
        self.calls.append(("upload", filepath, key, content_type, delete_source))
        os.remove(filepath)
        return SimpleNamespace(locator=f"local://{key}")


def _store(uid, staged, storage, **overrides):
    args = dict(
        owner=db.session.get(User, uid), storage=storage, filepath=staged,
        now=datetime(2026, 5, 6, 7, 8, 9), recorded_in_app=False, original_filename="talk.wav",
        user_title=None, final_file_size=len(PAYLOAD), meeting_date=datetime(2026, 1, 2, 3, 4, 5),
        mime_type="audio/wav", audio_duration_seconds=12.5, notes="the notes", selected_folder=None,
        selected_tags=[], processing_source="api_v1", file_hash=PAYLOAD_HASH,
        prompt_variables={"client": "Acme"}, effective_audio_only=True, prepare=None,
    )
    args.update(overrides)
    return store_recording(**args)


def test_store_recording_commits_and_returns_response(owner, staged):
    now = datetime(2026, 5, 6, 7, 8, 9)
    with app.app_context():
        storage = _Storage()
        with patch("src.services.storage_quota.reserve_room") as reserve, \
                patch.object(app.logger, "info") as info:
            stored = _store(owner, staged, storage, now=now)
        assert isinstance(stored, StoredRecording) and stored.error_response is None
        rec_id = stored.recording.id
        reserve.assert_called_once()
        assert reserve.call_args.args[0].id == owner and reserve.call_args.args[1] == len(PAYLOAD)
        assert storage.calls == [
            ("key", "talk.wav", rec_id, now),
            ("upload", staged, f"recordings/{rec_id}/talk.wav", "audio/wav", True),
        ]
        info.assert_called_once_with(f"Initial recording record created with ID: {rec_id}")
        db.session.expire_all()
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert (rec.audio_path, rec.status, rec.original_filename, rec.file_size, rec.user_id) == \
            (f"local://recordings/{rec_id}/talk.wav", "PENDING", "talk.wav", len(PAYLOAD), owner)
        assert (rec.meeting_date, rec.mime_type, rec.audio_duration_seconds, rec.notes) == \
            (datetime(2026, 1, 2, 3, 4, 5), "audio/wav", 12.5, "the notes")
        assert (rec.processing_source, rec.file_hash, rec.prompt_variables, rec.keep_audio_only) == \
            ("api_v1", PAYLOAD_HASH, {"client": "Acme"}, True)
        from src.utils.titles import placeholder_title
        assert rec.folder_id is None and rec.title == placeholder_title("talk.wav")
        assert rec.title_source is None
        body, status = build_ingestion_response(rec, owner=db.session.get(User, owner),
                                                duplicate_warning=None, reuse_duplicate=False,
                                                success_status=202)
        assert status == 202 and body.get_json()["id"] == rec_id


def test_store_recording_runs_prepare_after_the_flush_and_before_storage(owner, staged):
    with app.app_context():
        events = []
        storage = _Storage(events)

        def prepare(recording):
            # The row has an id but the file is not stored yet.
            events.append(("prepare", recording.id is not None, recording.audio_path,
                           recording in db.session.new, os.path.exists(staged)))
            recording.notes = "set by prepare"

        with patch("src.services.storage_quota.reserve_room"):
            stored = _store(owner, staged, storage, prepare=prepare)
        assert events == [("prepare", True, None, False, True), "upload"]
        rec_id = stored.recording.id
    with app.app_context():
        assert db.session.get(Recording, rec_id).notes == "set by prepare"


def test_store_recording_adds_tags_in_order_and_the_folder(tagworld, staged):
    w = tagworld
    with app.app_context():
        tags = [db.session.get(Tag, w.own_tag2), db.session.get(Tag, w.group_tag)]
        folder = db.session.get(Folder, w.own_folder)
        with patch("src.services.storage_quota.reserve_room"), \
                patch("src.api.folders.apply_group_folder_shares") as shares, \
                patch.object(app.logger, "info") as info:
            stored = _store(w.owner, staged, _Storage(), selected_tags=tags, selected_folder=folder,
                            user_title="Board meeting")
        rec_id = stored.recording.id
        shares.assert_called_once_with(stored.recording, folder)
        assert info.call_args_list[0].args == (f"Added 2 tags to recording {rec_id}: own2, group",)
    with app.app_context():
        rec = db.session.get(Recording, rec_id)
        assert rec.folder_id == w.own_folder
        assert (rec.title, rec.title_source) == ("Board meeting", "user")
        rows = RecordingTag.query.filter_by(recording_id=rec_id).order_by(RecordingTag.order).all()
        assert [(r.tag_id, r.order) for r in rows] == [(w.own_tag2, 1), (w.group_tag, 2)]


def test_store_recording_without_a_folder_shares_nothing(owner, staged):
    with app.app_context():
        with patch("src.services.storage_quota.reserve_room"), \
                patch("src.api.folders.apply_group_folder_shares") as shares:
            _store(owner, staged, _Storage())
        shares.assert_not_called()


def test_store_recording_recorded_in_app_skips_the_quota(owner, staged):
    with app.app_context():
        with patch("src.services.storage_quota.reserve_room") as reserve:
            stored = _store(owner, staged, _Storage(), recorded_in_app=True)
        reserve.assert_not_called()
        assert stored.recording is not None


def test_store_recording_quota_refusal_rolls_back_and_removes_staging(owner, staged):
    from src.services.storage_quota import StorageQuotaExceeded
    with app.app_context():
        storage = _Storage()
        before = Recording.query.filter_by(user_id=owner).count()
        with patch("src.services.storage_quota.reserve_room",
                   side_effect=StorageQuotaExceeded(900, 1000, 200)), \
                patch.object(db.session, "rollback", wraps=db.session.rollback) as rollback, \
                patch.object(app.logger, "info") as info:
            stored = _store(owner, staged, storage)
        assert stored.recording is None
        response, status = stored.error_response
        assert status == 507 and response.get_json()["code"] == "storage_quota_exceeded"
        rollback.assert_called_once_with()
        info.assert_called_once_with(f"Upload refused for user {owner}: "
                                     "Storage quota exceeded: 900 used + 200 new > 1000")
        assert not os.path.exists(staged)
        assert storage.calls == []
        assert Recording.query.filter_by(user_id=owner).count() == before


def test_store_recording_lets_a_unique_index_error_through(owner, staged):
    from sqlalchemy.exc import IntegrityError
    with app.app_context():
        error = IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        with patch("src.services.storage_quota.reserve_room"), \
                patch.object(db.session, "commit", side_effect=error):
            with pytest.raises(IntegrityError):
                _store(owner, staged, _Storage())
        db.session.rollback()


# --- handle_unique_conflict -------------------------------------------------------------

def _conflict(staged, on_conflict):
    from sqlalchemy.exc import IntegrityError
    try:
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
    except IntegrityError as e:
        return handle_unique_conflict(e, filepath=staged, on_conflict=on_conflict)


def test_unique_conflict_answers_with_on_conflict(staged):
    with app.app_context():
        seen = []

        def on_conflict():
            seen.append(("on_conflict", os.path.exists(staged), rollback.call_count))
            return ("earlier recording", 200)

        with patch.object(db.session, "rollback") as rollback, \
                patch.object(app.logger, "info") as info, patch.object(app.logger, "error") as error:
            assert _conflict(staged, on_conflict) == ("earlier recording", 200)
        # Rolled back and the staging copy removed before on_conflict runs.
        assert seen == [("on_conflict", False, 1)]
        info.assert_called_once_with("Upload lost a race on a unique key; answering with the earlier recording")
        error.assert_not_called()


@pytest.mark.parametrize("on_conflict", [None, lambda: None])
def test_unique_conflict_without_a_replay_is_the_usual_error(staged, on_conflict):
    with app.app_context():
        with patch.object(db.session, "rollback") as rollback, \
                patch.object(app.logger, "error") as error:
            response, status = _conflict(staged, on_conflict)
        assert status == 500 and response.get_json()["code"] == "unexpected_error_upload"
        rollback.assert_called_once_with()
        assert not os.path.exists(staged)
        error.assert_called_once()
        assert error.call_args.args[0].startswith("Error during file upload: (builtins.Exception) UNIQUE")
        assert error.call_args.kwargs == {"exc_info": True}


# --- build_ingestion_response -----------------------------------------------------------

@pytest.mark.parametrize("warning,reuse,expected", [
    (None, False, {}),
    ({"existing_recording_id": 1}, False, {"duplicate_warning": {"existing_recording_id": 1}}),
    (None, True, {"idempotent_replay": False}),
    ({}, True, {"idempotent_replay": False}),
])
def test_build_ingestion_response(owner, warning, reuse, expected):
    with app.app_context():
        rec = _recording(owner)
        user = db.session.get(User, owner)
        response, status = build_ingestion_response(rec, owner=user, duplicate_warning=warning,
                                                    reuse_duplicate=reuse, success_status=201)
        assert status == 201
        assert response.get_json() == {**rec.to_dict(viewer_user=user), **expected}


# --- enqueue_ingestion_jobs -------------------------------------------------------------

PARAMS = {"language": "de", "min_speakers": 2, "transcription_model": "whisper-1"}


def _enqueue(uid, rec, deferred, limit=None):
    events = []
    with patch("src.api.recordings.job_queue") as queue, \
            patch("src.services.webhook_dispatch.emit_recording_created",
                  side_effect=lambda r: events.append(("emit", r.id))) as emit, \
            patch.object(app.logger, "info") as info:
        queue.enqueue.side_effect = lambda **kw: events.append(("enqueue", kw["job_type"]))
        result = enqueue_ingestion_jobs(rec, owner=db.session.get(User, uid), resolved_params=PARAMS,
                                        conversion_deferred=deferred, extracted_audio_limit_mb=limit)
    return result, queue, emit, info, events


def test_deferred_conversion_enqueues_convert_job(owner):
    with app.app_context():
        rec = _recording(owner, status="PENDING")
        result, queue, emit, info, events = _enqueue(owner, rec, deferred=True, limit=40)
        assert result is None
        queue.enqueue.assert_called_once_with(
            user_id=owner, recording_id=rec.id, job_type="convert",
            params={"transcribe": PARAMS, "extracted_audio_limit_mb": 40}, is_new_upload=True)
        assert events == [("enqueue", "convert"), ("emit", rec.id)]
        emit.assert_called_once_with(rec)
        assert [c.args for c in info.call_args_list] == [
            (f"Conversion job queued for recording ID: {rec.id}",)]


def test_convert_job_carries_no_limit_when_none_applies(owner):
    with app.app_context():
        rec = _recording(owner, status="PENDING")
        _, queue, _, _, _ = _enqueue(owner, rec, deferred=True, limit=None)
        assert queue.enqueue.call_args.kwargs["params"] == {"transcribe": PARAMS,
                                                            "extracted_audio_limit_mb": None}


def test_without_deferral_enqueues_transcribe_job(owner):
    with app.app_context():
        rec = _recording(owner, status="PENDING")
        result, queue, emit, info, events = _enqueue(owner, rec, deferred=False, limit=40)
        assert result is None
        queue.enqueue.assert_called_once_with(user_id=owner, recording_id=rec.id, job_type="transcribe",
                                              params=PARAMS, is_new_upload=True)
        assert events == [("enqueue", "transcribe"), ("emit", rec.id)]
        assert [c.args for c in info.call_args_list] == [
            (f"Queueing transcription for recording {rec.id} with params: {PARAMS}",),
            (f"Transcription job queued for recording ID: {rec.id}",),
        ]


def test_enqueue_failure_emits_nothing(owner):
    with app.app_context():
        rec = _recording(owner, status="PENDING")
        with patch("src.api.recordings.job_queue") as queue, \
                patch("src.services.webhook_dispatch.emit_recording_created") as emit:
            queue.enqueue.side_effect = RuntimeError("queue down")
            with pytest.raises(RuntimeError):
                enqueue_ingestion_jobs(rec, owner=db.session.get(User, owner), resolved_params=PARAMS,
                                       conversion_deferred=False, extracted_audio_limit_mb=None)
        emit.assert_not_called()


# --- ingest and the ingest_uploaded_recording facade ------------------------------------

def test_facade_hands_every_argument_to_ingest():
    from src.api import recordings
    args = dict(owner=object(), uploaded_file=object(), form={"a": "b"}, original_filename="x.wav",
                processing_source="api_v1", success_status=201, reuse_duplicate=True,
                prepare=object(), on_conflict=object(), source=object(), enforce_upload_limits=False)
    with patch("src.api.recordings.ingest", return_value=("answer", 299)) as run:
        assert recordings.ingest_uploaded_recording(**args) == ("answer", 299)
        run.assert_called_once_with(**args)
        run.reset_mock()
        recordings.ingest_uploaded_recording(owner=args["owner"], form={})
        run.assert_called_once_with(owner=args["owner"], uploaded_file=None, form={},
                                    original_filename=None, processing_source="upload",
                                    success_status=202, reuse_duplicate=False, prepare=None,
                                    on_conflict=None, source=None, enforce_upload_limits=True)


def _ingest_upload(tmp_path, filename="talk.wav", **kwargs):
    from werkzeug.datastructures import FileStorage
    storage = MagicMock()
    storage.get_staging_dir.return_value = str(tmp_path)
    discarded = []
    with patch("src.api.recordings.get_storage_service", return_value=storage), \
            patch("src.api.recordings._discard_staging", side_effect=discarded.append):
        result = ingest(owner=SimpleNamespace(id=1), form={},
                        uploaded_file=FileStorage(io.BytesIO(b"x" * 64), filename=filename), **kwargs)
    return result, discarded


def test_ingest_without_a_file():
    with app.app_context():
        response, status = ingest(owner=SimpleNamespace(id=1), form={})
        assert (status, response.get_json()["code"]) == (400, "no_file_provided")


def test_ingest_too_large_before_the_limit_is_known(tmp_path):
    with app.test_request_context(), \
            patch.dict(app.config, {"MAX_CONTENT_LENGTH": 8 * MB}), \
            patch.object(recording_ingestion, "resolve_size_policy", side_effect=RequestEntityTooLarge()):
        (response, status), discarded = _ingest_upload(tmp_path)
        body = response.get_json()
    assert status == 413
    assert {k: body[k] for k in ("max_size_mb", "effective_limit_mb", "audio_only_mode")} == \
        {"max_size_mb": 8.0, "effective_limit_mb": 8.0, "audio_only_mode": False}
    assert len(discarded) == 1 and discarded[0].startswith(str(tmp_path)) and discarded[0].endswith("_talk.wav")


def test_ingest_too_large_reports_the_limit_that_fired(tmp_path):
    policy = SizePolicy(keep_audio_only_flag=True, effective_audio_only=True, is_likely_video_by_ext=True,
                        original_file_size=64, regular_limit_mb=10, audio_only_limit_mb=40,
                        effective_limit_mb=40, effective_limit_bytes=40 * MB, max_content_length=None,
                        connector_specs=None, should_enforce_size_limit=True)
    with app.test_request_context(), \
            patch.object(recording_ingestion, "resolve_size_policy", return_value=policy), \
            patch.object(recording_ingestion, "enforce_size_policy", side_effect=RequestEntityTooLarge()):
        (response, status), discarded = _ingest_upload(tmp_path, filename="talk.mp4")
        body = response.get_json()
    assert status == 413
    assert {k: body[k] for k in ("max_size_mb", "effective_limit_mb", "audio_only_mode")} == \
        {"max_size_mb": 40.0, "effective_limit_mb": 40.0, "audio_only_mode": True}
    assert len(discarded) == 1


def test_ingest_answers_a_unique_conflict_through_handle_unique_conflict(tmp_path):
    from sqlalchemy.exc import IntegrityError
    error = IntegrityError("INSERT", {}, Exception("UNIQUE"))
    on_conflict = MagicMock()
    with app.test_request_context(), \
            patch.object(recording_ingestion, "enforce_size_policy", side_effect=error), \
            patch.object(recording_ingestion, "handle_unique_conflict",
                         return_value=("replayed", 200)) as handle:
        result, _ = _ingest_upload(tmp_path, on_conflict=on_conflict)
    assert result == ("replayed", 200)
    handle.assert_called_once()
    assert handle.call_args.args == (error,)
    assert handle.call_args.kwargs["on_conflict"] is on_conflict
    assert handle.call_args.kwargs["filepath"].startswith(str(tmp_path))


def test_ingest_any_other_error_rolls_back_and_removes_staging(tmp_path):
    with app.test_request_context(), \
            patch.object(recording_ingestion, "enforce_size_policy", side_effect=RuntimeError("boom")), \
            patch.object(db.session, "rollback") as rollback, \
            patch.object(app.logger, "error") as log_error:
        (response, status), discarded = _ingest_upload(tmp_path)
    assert (status, response.get_json()["code"]) == (500, "unexpected_error_upload")
    rollback.assert_called_once_with()
    assert len(discarded) == 1 and discarded[0].startswith(str(tmp_path))
    log_error.assert_called_once_with("Error during file upload: boom", exc_info=True)
