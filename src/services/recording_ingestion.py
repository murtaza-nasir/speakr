"""Steps of upload ingestion, moved out of ``ingest_uploaded_recording``.

``ingest_uploaded_recording`` (src/api/recordings.py) runs every upload
through one pipeline: POST /upload, the share target, API v1 uploads,
sliced upload finalization and the file monitor. Its steps move here one
at a time (discussion #556). The function still calls them in the same
order and keeps the outer error handling and the staging cleanup.

Duplicate detection:

* :func:`lookup_duplicate` hashes the staged upload and finds an earlier
  recording of the same file.
* :func:`handle_duplicate_replay` answers a retried upload
  (``reuse_duplicate``) with the recording it already created.

Size policy:

* :func:`resolve_size_policy` works out the per-type size limit, the
  request ceiling and whether chunking lifts the limit, before the upload
  is saved; :func:`enforce_size_policy` refuses an upload over them.
* :func:`recheck_size_after_probe` applies the regular limit once the probe
  shows that a video extension did not mean a video.

Probe:

* :func:`probe_upload` reads the upload's codecs with ffprobe, repairing a
  malformed .webm/.mkv header.
* :func:`has_video_after_failed_probe` takes a video extension as video
  when the probe did not finish, under VIDEO_RETENTION or
  VIDEO_PASSTHROUGH_ASR.
"""

import os
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Optional

from flask import current_app, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

from src.models import ProcessingJob, Recording
from src.services.job_queue import job_queue
from src.services.transcription_defaults import resolve_transcription_params
from src.utils.dates import to_utc_naive


class DuplicateLookup(NamedTuple):
    """What :func:`lookup_duplicate` found for one upload."""
    file_hash: Optional[str]
    existing: Optional[Recording]
    duplicate_warning: Optional[dict]


def lookup_duplicate(*, owner, filepath, form, processing_source, original_filename,
                     reuse_duplicate):
    """Hash the staged upload and look for an earlier recording of it.

    With reuse_duplicate the match must also have the same source, filename,
    notes and meeting date (or, without a meeting date, be under a day old).
    duplicate_warning is set only when reuse_duplicate is off. A failure to
    hash or query is logged and leaves the remaining fields None.
    """
    # Imported at call time so a patch of src.utils.file_hash reaches it.
    from src.utils.file_hash import compute_file_sha256

    file_hash = None
    duplicate_warning = None
    existing = None
    try:
        file_hash = compute_file_sha256(filepath)
        duplicate_query = Recording.query.filter_by(
            user_id=owner.id,
            file_hash=file_hash,
        )
        if reuse_duplicate:
            duplicate_query = duplicate_query.filter_by(
                processing_source=processing_source,
                original_filename=original_filename,
                notes=form.get('notes'),
            )
            replay_meeting_date = None
            raw_meeting_date = form.get('meeting_date')
            if raw_meeting_date:
                try:
                    replay_meeting_date = to_utc_naive(
                        datetime.fromisoformat(raw_meeting_date.replace('Z', '+00:00'))
                    )
                except (ValueError, TypeError):
                    pass
            if replay_meeting_date is not None:
                duplicate_query = duplicate_query.filter_by(meeting_date=replay_meeting_date)
            else:
                duplicate_query = duplicate_query.filter(
                    Recording.created_at >= datetime.utcnow() - timedelta(days=1)
                )
        existing = duplicate_query.first()
    except Exception as e:
        current_app.logger.warning(f"Could not compute file hash or check duplicates: {e}")

    # A replay (reuse_duplicate) is answered by handle_duplicate_replay instead.
    if existing and not reuse_duplicate:
        duplicate_warning = {
            'existing_recording_id': existing.id,
            'existing_title': existing.title,
            'existing_created_at': existing.created_at.isoformat() if existing.created_at else None
        }
        current_app.logger.info(
            f"Duplicate file detected for user {owner.id}: "
            f"hash={file_hash[:12]}... matches recording {existing.id}"
        )

    return DuplicateLookup(file_hash, existing, duplicate_warning)


def handle_duplicate_replay(existing, *, owner, filepath, file_hash, success_status):
    """Answer a retried upload with the recording it already created.

    Removes the retry's staging copy and queues the transcription again if
    the recording is still waiting without a job. Returns the response.
    """
    # Deferred: src.api.recordings imports this module.
    from src.api.recordings import _discard_staging

    # The existing recording already owns the durable media object, so
    # the retry's staging copy is never needed for job recovery. Remove
    # it before enqueueing to avoid leaking one full file each time a
    # temporarily unavailable queue makes ASR retry the delivery.
    _discard_staging(filepath)

    # A pending conversion queues the transcription when it is done.
    existing_job = ProcessingJob.query.filter(
        ProcessingJob.recording_id == existing.id,
        ProcessingJob.job_type.in_(('transcribe', 'convert')),
    ).first()
    if existing.status in ('PENDING', 'QUEUED') and existing_job is None:
        # Resolve from the existing recording (tags/folder aware), not
        # just the owner: equivalent for ASR-sourced recordings today,
        # but robust if another caller ever reuses this recovery path.
        recovery_params = resolve_transcription_params(existing)
        job_queue.enqueue(
            user_id=owner.id,
            recording_id=existing.id,
            job_type='transcribe',
            params=recovery_params,
            is_new_upload=True,
        )
    current_app.logger.info(
        f"Idempotent upload replay for user {owner.id}: "
        f"hash={file_hash[:12]}... matches recording {existing.id}"
    )
    response_data = existing.to_dict(viewer_user=owner)
    response_data['idempotent_replay'] = True
    return jsonify(response_data), success_status


class SizePolicy(NamedTuple):
    """The size limits :func:`resolve_size_policy` set for one upload."""
    keep_audio_only_flag: bool
    effective_audio_only: bool
    is_likely_video_by_ext: bool
    original_file_size: int
    regular_limit_mb: int
    audio_only_limit_mb: int
    effective_limit_mb: int
    effective_limit_bytes: int
    max_content_length: Optional[int]
    connector_specs: Optional[Any]
    should_enforce_size_limit: bool


def resolve_size_policy(*, file, form, original_filename, enforce_upload_limits):
    """Work out the size limits for an upload before it is saved.

    Reads the upload's size from file (leaving it at the start), the
    keep_audio_only form field, the per-type limits and the request ceiling,
    and whether chunking lifts the per-type limit. Raises nothing itself:
    :func:`enforce_size_policy` refuses an upload over the limits.
    """
    # Read from src.api.recordings at call time: tests patch these, and init sets chunking_service.
    from src.api.recordings import (
        SystemSetting, USE_NEW_TRANSCRIPTION_ARCHITECTURE, VIDEO_RETENTION, chunking_service,
    )

    # Per-upload override for VIDEO_RETENTION. When True, the
    # processing pipeline extracts audio and discards the video stream
    # regardless of the server's VIDEO_RETENTION env var. Also signals
    # that this upload is allowed to exceed max_file_size_mb because
    # only the extracted audio needs to fit that limit. Sent by the
    # frontend either explicitly (toggle) or implicitly when
    # VIDEO_RETENTION is off and the queued file is video.
    keep_audio_only_flag = form.get('keep_audio_only', 'false').lower() == 'true'
    effective_audio_only = keep_audio_only_flag or not VIDEO_RETENTION

    # Detect "this looks like a video" by extension up front so the
    # size-gate can pick the right limit before reading any bytes.
    # ffprobe runs later and is authoritative for the keep-video
    # decision; the extension list here is a pre-save heuristic.
    _VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.mts'}
    _ext_lower = os.path.splitext(original_filename)[1].lower()
    is_likely_video_by_ext = _ext_lower in _VIDEO_EXTS

    # Get original file size
    file.seek(0, os.SEEK_END)
    original_file_size = file.tell()
    file.seek(0)

    # Resolve effective size limit. Video files (by extension) get
    # the larger video cap regardless of whether the user opted
    # into "Keep audio only". Audio files use the regular cap.
    # This is the option-B model: admins set a single per-file-type
    # upload cap and don't have to think about how the keep-audio
    # toggle interacts with size validation. The post-extraction
    # guard further down still enforces that the *stored* artifact
    # fits the regular limit when only audio is kept.
    regular_limit_mb = int(SystemSetting.get_setting('max_file_size_mb', 250))
    audio_only_limit_mb = int(
        SystemSetting.get_setting('max_audio_only_video_size_mb', regular_limit_mb * 4)
    )
    if is_likely_video_by_ext:
        effective_limit_mb = audio_only_limit_mb
    else:
        effective_limit_mb = regular_limit_mb
    effective_limit_bytes = effective_limit_mb * 1024 * 1024

    # Check size limit before saving - only enforce if chunking is disabled or connector handles it
    max_content_length = current_app.config.get('MAX_CONTENT_LENGTH')

    # Get connector specifications for chunking decisions
    connector_specs = None
    if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
        try:
            from src.services.transcription import get_registry
            registry = get_registry()
            connector = registry.get_active_connector()
            if connector:
                connector_specs = connector.specifications
        except Exception as e:
            current_app.logger.warning(f"Could not get connector specs for upload: {e}")

    # Chunking (app-level, or inside the connector) lifts the per-type
    # policy cap; the WSGI ceiling below always applies (#533).
    should_enforce_size_limit = enforce_upload_limits
    if chunking_service:
        from src.audio_chunking import get_effective_chunking_config
        chunking_config = get_effective_chunking_config(connector_specs)
        if chunking_config.enabled or chunking_config.source == 'connector_internal':
            should_enforce_size_limit = False
            if chunking_config.source == 'connector_internal':
                current_app.logger.info(f"Connector handles chunking internally - skipping {original_file_size/1024/1024:.1f}MB size limit check")
            elif chunking_config.mode == 'size':
                current_app.logger.info(f"Size-based chunking enabled ({chunking_config.limit_value}MB, source={chunking_config.source}) - skipping {original_file_size/1024/1024:.1f}MB size limit check")
            else:
                current_app.logger.info(f"Duration-based chunking enabled ({chunking_config.limit_value}s, source={chunking_config.source}) - skipping {original_file_size/1024/1024:.1f}MB size limit check")

    return SizePolicy(
        keep_audio_only_flag=keep_audio_only_flag,
        effective_audio_only=effective_audio_only,
        is_likely_video_by_ext=is_likely_video_by_ext,
        original_file_size=original_file_size,
        regular_limit_mb=regular_limit_mb,
        audio_only_limit_mb=audio_only_limit_mb,
        effective_limit_mb=effective_limit_mb,
        effective_limit_bytes=effective_limit_bytes,
        max_content_length=max_content_length,
        connector_specs=connector_specs,
        should_enforce_size_limit=should_enforce_size_limit,
    )


def enforce_size_policy(policy, *, enforce_upload_limits):
    """Refuse, before it is saved, an upload over the request ceiling or the per-type limit.

    Raises RequestEntityTooLarge. The caller binds the policy's limits
    first, since its handler reports them.
    """
    # Two ceilings to consider. The Werkzeug `max_content_length` is
    # the WSGI hard cap; Werkzeug enforces it as well, and it is checked
    # here on every upload whatever the chunking setting (#533). The
    # smaller `effective_limit_bytes` is the per-request policy cap
    # (which differs between regular and audio-only-video uploads), and
    # chunking lifts it.
    if (enforce_upload_limits and policy.max_content_length
            and policy.original_file_size > policy.max_content_length):
        raise RequestEntityTooLarge()
    if policy.should_enforce_size_limit and policy.original_file_size > policy.effective_limit_bytes:
        raise RequestEntityTooLarge()


class SizeRecheck(NamedTuple):
    """The limits after :func:`recheck_size_after_probe`, and whether the upload is over them."""
    is_likely_video_by_ext: bool
    effective_limit_mb: int
    too_large: bool


def recheck_size_after_probe(*, codec_info, has_video, original_filename, is_likely_video_by_ext,
                             effective_limit_mb, regular_limit_mb, should_enforce_size_limit,
                             original_file_size):
    """Apply the regular limit once the probe shows a video extension did not mean video.

    An upload the probe found to be audio-only, or could not read, gets the
    regular limit. too_large is True where the upload is over it; the caller
    then binds the returned limits and raises RequestEntityTooLarge.
    """
    # Read from src.api.recordings at call time: tests patch these there.
    from src.api.recordings import VIDEO_PASSTHROUGH_ASR, VIDEO_RETENTION

    # The extension is only a pre-probe size hint. Once ffprobe proves the
    # upload is audio-only, do not let a video-looking filename grant the
    # larger video upload ceiling.
    if codec_info is not None and is_likely_video_by_ext and not has_video:
        is_likely_video_by_ext = False
        if should_enforce_size_limit and original_file_size > regular_limit_mb * 1024 * 1024:
            effective_limit_mb = regular_limit_mb
            return SizeRecheck(is_likely_video_by_ext, effective_limit_mb, True)

    # A file the probe could not read, with a video extension, under
    # VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR (see has_video_after_failed_probe).
    if codec_info is None and (VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR) and not has_video:
        video_extensions = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.mts'}
        file_ext = os.path.splitext(original_filename)[1].lower()
        if file_ext in video_extensions:
            # A failed probe cannot justify the larger video ceiling: an
            # arbitrary blob or audio file can also carry a video-looking
            # extension. Require positive video detection for oversized
            # uploads; smaller files can continue through conversion.
            if should_enforce_size_limit and original_file_size > regular_limit_mb * 1024 * 1024:
                effective_limit_mb = regular_limit_mb
                is_likely_video_by_ext = False
                return SizeRecheck(is_likely_video_by_ext, effective_limit_mb, True)
    return SizeRecheck(is_likely_video_by_ext, effective_limit_mb, False)


class UploadProbe(NamedTuple):
    """What :func:`probe_upload` found in the staged upload."""
    codec_info: Optional[dict]
    probe_error: Optional[Exception]
    has_video: bool


def probe_upload(*, filepath, original_filename):
    """Probe the staged upload's codecs with ffprobe.

    A probe that fails leaves codec_info None and the FFProbeError in
    probe_error; a .webm, .mkv or .mka whose header is not at the front is
    trimmed to it and probed again (#340).
    """
    # Read from src.api.recordings at call time: tests patch these there.
    from src.api.recordings import FFProbeError, get_codec_info, try_repair_malformed_webm

    # Probe once and use shared conversion utility
    # Scale timeout based on file size — large files (especially MP4 with moov at end) need more time
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    probe_timeout = max(10, min(60, int(file_size_mb / 10)))  # 10s min, scales ~1s per 10MB, 60s max
    codec_info = None
    probe_error = None
    try:
        codec_info = get_codec_info(filepath, timeout=probe_timeout)
        current_app.logger.info(
            f"Detected codec for {original_filename}: "
            f"audio_codec={codec_info.get('audio_codec')}, "
            f"has_video={codec_info.get('has_video', False)}"
        )
    except FFProbeError as e:
        current_app.logger.warning(f"Failed to probe {original_filename} (timeout={probe_timeout}s): {e}. Will attempt conversion.")
        codec_info = None
        probe_error = e
        # #340: a browser MediaRecorder can produce a .webm whose EBML header
        # isn't at the front (chunk-order artifact, seen on crash-recovered
        # recordings). If so, trim to the real header and re-probe before we
        # fall through to a conversion that would just fail again.
        if os.path.splitext(original_filename)[1].lower() in ('.webm', '.mkv', '.mka'):
            repaired_info = try_repair_malformed_webm(filepath, timeout=probe_timeout)
            if repaired_info is not None:
                codec_info = repaired_info
                probe_error = None
                current_app.logger.info(f"Recovered malformed WebM header for {original_filename}; probe succeeded after trim")

    # Video retention/passthrough: skip conversion for videos, processing pipeline handles extraction
    has_video = codec_info.get('has_video', False) if codec_info else False

    return UploadProbe(codec_info, probe_error, has_video)


def has_video_after_failed_probe(*, codec_info, probe_error, has_video, original_filename):
    """Take a video extension as video when the probe did not finish.

    Only under VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR. A probe that read the
    file and rejected it is not overruled. Returns has_video.
    """
    # Read from src.api.recordings at call time: tests patch these there.
    from src.api.recordings import VIDEO_PASSTHROUGH_ASR, VIDEO_RETENTION, probe_rejected_input

    # Fallback: if probe failed but VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR is on, check file extension
    # to avoid silently discarding video from files we couldn't probe
    if codec_info is None and (VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR) and not has_video:
        video_extensions = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.mts'}
        file_ext = os.path.splitext(original_filename)[1].lower()
        if file_ext in video_extensions:
            if probe_error is not None and probe_rejected_input(probe_error):
                # ffprobe read the file and could not make sense of it.
                # Only a probe that did not finish is trusted to the
                # extension: this file goes through conversion, which
                # refuses it with audio_decode_failed, as without these
                # settings (#482).
                current_app.logger.info(
                    f"ffprobe could not read '{file_ext}' upload; converting it as audio"
                )
            else:
                has_video = True
                current_app.logger.info(
                    f"Probe failed but file extension '{file_ext}' indicates video — "
                    f"treating as video for {'VIDEO_PASSTHROUGH_ASR' if VIDEO_PASSTHROUGH_ASR else 'VIDEO_RETENTION'}"
                )
    return has_video
