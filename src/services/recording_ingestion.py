"""Upload ingestion: the pipeline behind ``ingest_uploaded_recording``.

``ingest_uploaded_recording`` (src/api/recordings.py) runs every upload
through one pipeline: POST /upload, the share target, API v1 uploads,
sliced upload finalization and the file monitor. It is a facade for
:func:`ingest`, which runs the steps below in order inside the outer error
handling and the staging cleanup (discussion #556).

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
  when the probe did not finish, under _self.VIDEO_RETENTION or
  _self.VIDEO_PASSTHROUGH_ASR.

Conversion:

* :func:`decide_conversion` keeps a retained video or a join part as it is,
  leaves a readable file to the convert job, or asks for conversion now.
* :func:`convert_upload_in_request` converts the file before the request is
  answered and turns an FFmpeg failure into the error answer.
* :func:`check_extracted_audio_size` refuses a video whose extracted audio
  is over the regular limit.

Storage and database writes:

* :func:`select_tags_and_folder` reads the upload's tags and folder from the
  form and keeps those the owner may use.
* :func:`store_recording` reserves the owner's quota, creates the recording,
  stores the file and commits the recording with its tags and folder shares.
* :func:`handle_unique_conflict` answers, from the caller's error handling,
  an upload whose rows hit a unique index (#476).
* :func:`build_ingestion_response` assembles the success answer.

Enqueue:

* :func:`enqueue_ingestion_jobs` queues the convert job of a deferred
  conversion or the transcribe job, then emits recording.created.

Assembly:

* :func:`ingest` runs the steps for one upload and answers it.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional

from flask import current_app, jsonify
from flask_login import current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from src.database import db
from src.models import (
    Folder, GroupMembership, ProcessingJob, Recording, RecordingTag, SystemSetting, Tag,
)
from src.services.job_queue import CONVERT_WORKERS, job_queue
from src.services.transcription_defaults import resolve_transcription_params
from src.utils.audio_conversion import convert_if_needed
from src.utils.dates import to_utc_naive
from src.utils.ffmpeg_utils import FFmpegError, FFmpegNotFoundError, FFmpegTimeoutError
from src.utils.ffprobe import (
    FFProbeError, get_codec_info, get_creation_date, get_duration, has_audio_stream,
    probe_rejected_input, try_repair_malformed_webm,
)
from src.utils.filenames import shorten_filename
from src.utils.localization import api_error, format_number, translate
from src.utils.mime import guess_media_type, served_media_mime
from src.utils.titles import resolve_upload_title, upload_title_source


# Extensions taken as video before (or without) a probe: the size limit
# before the upload is saved, and the fallback when the probe fails.
VIDEO_EXTENSIONS = frozenset({'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.mts'})


def _discard_staging(path):
    """Remove an upload's staging copy when the upload fails.

    A file handed to storage has already left the staging directory, so
    after that point this finds nothing to remove.
    """
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        current_app.logger.warning(f"Could not remove staging file {path}: {e}")


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
    # Read from src.api.recordings at call time: its routes use
    # USE_NEW_TRANSCRIPTION_ARCHITECTURE too, and init sets chunking_service.
    from src.api.recordings import USE_NEW_TRANSCRIPTION_ARCHITECTURE, chunking_service

    # Per-upload override for VIDEO_RETENTION. When True, the
    # processing pipeline extracts audio and discards the video stream
    # regardless of the server's VIDEO_RETENTION env var. Also signals
    # that this upload is allowed to exceed max_file_size_mb because
    # only the extracted audio needs to fit that limit. Sent by the
    # frontend either explicitly (toggle) or implicitly when
    # VIDEO_RETENTION is off and the queued file is video.
    keep_audio_only_flag = form.get('keep_audio_only', 'false').lower() == 'true'
    effective_audio_only = keep_audio_only_flag or not _self.VIDEO_RETENTION

    # Detect "this looks like a video" by extension up front so the
    # size-gate can pick the right limit before reading any bytes.
    # ffprobe runs later and is authoritative for the keep-video
    # decision; the extension list here is a pre-save heuristic.
    _ext_lower = os.path.splitext(original_filename)[1].lower()
    is_likely_video_by_ext = _ext_lower in VIDEO_EXTENSIONS

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
    # policy cap; the WSGI ceiling always applies (#533, enforce_size_policy).
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
    if codec_info is None and (_self.VIDEO_RETENTION or _self.VIDEO_PASSTHROUGH_ASR) and not has_video:
        file_ext = os.path.splitext(original_filename)[1].lower()
        if file_ext in VIDEO_EXTENSIONS:
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

    Only under _self.VIDEO_RETENTION or _self.VIDEO_PASSTHROUGH_ASR. A probe that read the
    file and rejected it is not overruled. Returns has_video.
    """
    # Fallback: if probe failed but VIDEO_RETENTION or VIDEO_PASSTHROUGH_ASR is on, check file extension
    # to avoid silently discarding video from files we couldn't probe
    if codec_info is None and (_self.VIDEO_RETENTION or _self.VIDEO_PASSTHROUGH_ASR) and not has_video:
        file_ext = os.path.splitext(original_filename)[1].lower()
        if file_ext in VIDEO_EXTENSIONS:
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
                    f"treating as video for {'VIDEO_PASSTHROUGH_ASR' if _self.VIDEO_PASSTHROUGH_ASR else 'VIDEO_RETENTION'}"
                )
    return has_video


class ConversionDecision(NamedTuple):
    """How :func:`decide_conversion` treats one upload's media."""
    keep_video_for_this_upload: bool
    skip_conversion_for_join: bool
    chunking_will_handle_large_audio: bool
    extracted_audio_limit_mb: Optional[int]
    can_defer_conversion: bool
    conversion_deferred: bool
    convert_in_request: bool


def decide_conversion(*, codec_info, has_video, keep_audio_only_flag, join, original_filename,
                      should_enforce_size_limit, enforce_upload_limits, effective_audio_only,
                      is_likely_video_by_ext, regular_limit_mb, original_file_size):
    """Decide whether the upload is kept as it is, converted later or converted now.

    A retained or passed-through video and an audio part of a join are kept;
    a readable file with audio is left to the convert job (#543). Anything
    else has convert_in_request set: :func:`convert_upload_in_request`.
    """
    # Read from src.api.recordings at call time: init sets it.
    from src.api.recordings import chunking_service

    # Per-upload `keep_audio_only` overrides VIDEO_RETENTION for this
    # request. VIDEO_PASSTHROUGH_ASR is an admin escape hatch that
    # sends video directly to the ASR endpoint — left untouched here.
    # A video without an audio track has nothing to transcribe: it goes
    # through conversion, which refuses it, as without these settings.
    keep_video_for_this_upload = (
        ((_self.VIDEO_RETENTION and not keep_audio_only_flag) or _self.VIDEO_PASSTHROUGH_ASR)
        and has_video
        and (codec_info is None or has_audio_stream(codec_info))
    )
    # An audio file uploaded to be joined is decoded and re-encoded by the
    # merge job anyway, so converting it here would only delay the upload
    # response (minutes for a long recording). Video parts still have their
    # audio extracted, which is a fast stream copy and keeps a large video
    # from being stored whole until the join.
    skip_conversion_for_join = bool(join) and codec_info is not None and not has_video
    chunking_will_handle_large_audio = (
        chunking_service is not None and not should_enforce_size_limit
    )
    # Only the extracted audio of a video has to fit the regular limit.
    extracted_audio_limit_mb = (
        regular_limit_mb
        if (enforce_upload_limits and effective_audio_only and is_likely_video_by_ext
            and not chunking_will_handle_large_audio)
        else None
    )
    # A file ffprobe could read, with an audio track, is converted after
    # the upload is answered, by the convert job (#543). The request still
    # refuses at once what it can: a file ffprobe could not read goes
    # through the conversion here, which refuses it with
    # audio_decode_failed, and so does a video without audio. So does a
    # video that only passes the size limit once its audio is extracted,
    # and parts of a join, which the merge job converts. Without convert
    # workers (CONVERT_QUEUE_WORKERS=0) every file is converted here.
    can_defer_conversion = (
        CONVERT_WORKERS > 0
        and not join
        and codec_info is not None
        and has_audio_stream(codec_info)
        and not (extracted_audio_limit_mb and original_file_size > extracted_audio_limit_mb * 1024 * 1024)
    )
    conversion_deferred = False
    convert_in_request = False
    if keep_video_for_this_upload:
        current_app.logger.info(f"Video {'passthrough' if _self.VIDEO_PASSTHROUGH_ASR else 'retention'}: keeping original video, skipping conversion")
    elif skip_conversion_for_join:
        current_app.logger.info(f"Join part {original_filename}: skipping conversion, the merge re-encodes it")
    elif can_defer_conversion:
        conversion_deferred = True
        current_app.logger.info(f"Upload {original_filename}: converting after the upload is answered")
    else:
        convert_in_request = True
    return ConversionDecision(
        keep_video_for_this_upload=keep_video_for_this_upload,
        skip_conversion_for_join=skip_conversion_for_join,
        chunking_will_handle_large_audio=chunking_will_handle_large_audio,
        extracted_audio_limit_mb=extracted_audio_limit_mb,
        can_defer_conversion=can_defer_conversion,
        conversion_deferred=conversion_deferred,
        convert_in_request=convert_in_request,
    )


class ConvertedUpload(NamedTuple):
    """The file :func:`convert_upload_in_request` left, or the error answer."""
    filepath: Optional[str]
    error_response: Optional[tuple]


def convert_upload_in_request(filepath, *, original_filename, codec_info,
                              needs_chunking_for_processing, connector_specs):
    """Convert the staged upload before the request is answered.

    Returns the path of the file left (the same path when nothing needed
    converting). When FFmpeg is missing, times out or
    cannot decode the file, the staging copy is removed and error_response
    holds the answer (500, 500 or 400) for the caller to return.
    """
    # Read from src.api.recordings at call time: its routes use it too.
    from src.api.recordings import USE_ASR_ENDPOINT

    # Use shared conversion utility - handles ALL conversion needs (codec conversion + compression)
    try:
        result = convert_if_needed(
            filepath,
            original_filename=original_filename,
            codec_info=codec_info,
            needs_chunking=needs_chunking_for_processing,
            is_asr_endpoint=USE_ASR_ENDPOINT,
            delete_original=True,
            connector_specs=connector_specs  # Pass connector specs for codec restrictions
        )
        filepath = result.output_path

        # Log what happened
        if result.was_converted:
            current_app.logger.info(f"File converted: {result.original_codec} -> {result.final_codec}")
        if result.was_compressed:
            current_app.logger.info(f"File compressed: {result.size_change_label}")

    except FFmpegNotFoundError as e:
        current_app.logger.error(f"FFmpeg not found: {e}")
        _discard_staging(filepath)
        return ConvertedUpload(None, (jsonify(api_error('audio_converter_missing')), 500))
    except FFmpegTimeoutError as e:
        # A timeout is the server's limit, not proof the file is bad.
        current_app.logger.error(f"FFmpeg conversion timed out for {filepath}: {e}")
        _discard_staging(filepath)
        return ConvertedUpload(None, (jsonify({'error': translate('server.apiError.audio_conversion_timeout'),
                                               'code': 'audio_conversion_timeout'}), 500))
    except FFmpegError as e:
        # The probe is allowed to fail, so a payload that is not audio
        # at all is first refused here, as the client's error.
        current_app.logger.error(f"FFmpeg conversion failed for {filepath}: {e}")
        _discard_staging(filepath)
        return ConvertedUpload(None, (jsonify(api_error('audio_decode_failed')), 400))

    return ConvertedUpload(filepath, None)


def check_extracted_audio_size(*, filepath, final_file_size, extracted_audio_limit_mb,
                               conversion_deferred, regular_limit_mb):
    """Refuse a video whose extracted audio is over the regular limit.

    Removes the staging copy and returns the 413 answer, or None when the
    file fits (or the convert job checks it later).
    """
    # Post-extraction size guard. When the upload exceeded the regular
    # max_file_size_mb under the "audio-only video" exception, the
    # stored file (after audio extraction) must still fit the regular
    # limit. If it does not, the user picked a video whose audio track
    # alone is enormous AND chunking is off; reject the upload,
    # clean up, and let the user know. When chunking is on the
    # large extracted audio is fine — the chunking pipeline will
    # split it for the ASR call. extracted_audio_limit_mb is None when
    # the upload limits are not enforced (watched files); a deferred
    # conversion is checked by the convert job, after the extraction.
    if (
        extracted_audio_limit_mb
        and not conversion_deferred
        and final_file_size > extracted_audio_limit_mb * 1024 * 1024
    ):
        _discard_staging(filepath)
        current_app.logger.warning(
            f"Audio-only extraction left {final_file_size/1024/1024:.1f}MB of audio, "
            f"which exceeds regular limit {regular_limit_mb}MB and chunking is off. "
            f"Rejecting upload."
        )
        return jsonify({
            **api_error('extracted_audio_too_large',
                        size=format_number(final_file_size / 1024 / 1024),
                        limit=format_number(regular_limit_mb)),
            'max_size_mb': float(regular_limit_mb),
            'extracted_audio_mb': round(final_file_size / 1024 / 1024, 1),
            'audio_only_mode': True,
            'chunking_enabled': False,
        }), 413
    return None


class TagFolderSelection(NamedTuple):
    """The tags and folder :func:`select_tags_and_folder` accepted for one upload."""
    selected_tags: list
    selected_folder: Optional[Folder]


def select_tags_and_folder(*, form, owner):
    """Read the upload's tags and folder from the form, keeping those owner may use.

    A tag or folder is accepted when it is owner's own or a group's of which
    owner is a member. tag_ids[0], tag_ids[1], ... are read in order up to
    the first missing index; tag_id is read only when none was accepted.
    """
    # Get selected tags if provided (multiple tags support)
    selected_tags = []
    tag_index = 0
    while True:
        tag_id_key = f'tag_ids[{tag_index}]'
        tag_id = form.get(tag_id_key)
        if not tag_id:
            break

        # Check if tag belongs to user OR is a group tag where user is a member
        tag = Tag.query.filter_by(id=tag_id).first()
        if tag:
            # Allow tag if it's user's own tag OR it's a group tag where user is a member
            if tag.user_id == owner.id or (tag.group_id and GroupMembership.query.filter_by(group_id=tag.group_id, user_id=owner.id).first()):
                selected_tags.append(tag)
        tag_index += 1

    # For backward compatibility with single tag uploads
    if not selected_tags:
        single_tag_id = form.get('tag_id')
        if single_tag_id:
            # Check if tag belongs to user OR is a group tag where user is a member
            tag = Tag.query.filter_by(id=single_tag_id).first()
            if tag and (tag.user_id == owner.id or (tag.group_id and GroupMembership.query.filter_by(group_id=tag.group_id, user_id=owner.id).first())):
                selected_tags.append(tag)

    # Get folder_id if provided
    selected_folder = None
    folder_id = form.get('folder_id')
    if folder_id:
        folder = Folder.query.filter_by(id=folder_id).first()
        if folder:
            # Allow folder if it's user's own folder OR it's a group folder where user is a member
            if folder.user_id == owner.id or (folder.group_id and GroupMembership.query.filter_by(group_id=folder.group_id, user_id=owner.id).first()):
                selected_folder = folder
    return TagFolderSelection(selected_tags, selected_folder)


class StoredRecording(NamedTuple):
    """The recording :func:`store_recording` committed, or the quota refusal."""
    recording: Optional[Recording]
    error_response: Optional[tuple]


def store_recording(*, owner, storage, filepath, now, recorded_in_app, original_filename,
                    user_title, final_file_size, meeting_date, mime_type, audio_duration_seconds,
                    notes, selected_folder, selected_tags, processing_source, file_hash,
                    prompt_variables, effective_audio_only, prepare):
    """Create the recording, store the staged file and commit both.

    Unless the upload was recorded in this browser, the owner's quota is
    reserved first; a refusal rolls back, removes the staging copy and is
    returned in error_response. The recording is flushed, then prepare runs,
    then the file moves to storage (the staging copy is gone after that) and
    the tags and folder shares are added before the one commit. An
    IntegrityError from the commit propagates to the caller's handler
    (:func:`handle_unique_conflict`).
    """
    from src.services.storage_quota import StorageQuotaExceeded

    # The early check in ingest ran before the conversion, which another
    # upload can overlap. Count again, with the quota held until the
    # commit below, the bytes that will be stored (#509).
    if not recorded_in_app:
        from src.services.storage_quota import reserve_room
        try:
            reserve_room(owner, final_file_size)
        except StorageQuotaExceeded as quota_error:
            db.session.rollback()
            current_app.logger.info(f"Upload refused for user {owner.id}: {quota_error}")
            _discard_staging(filepath)
            return StoredRecording(None, quota_error.response())

    recording = Recording(
        audio_path=None,
        original_filename=original_filename,
        title=resolve_upload_title(user_title, original_filename),
        title_source=upload_title_source(user_title),
        file_size=final_file_size,
        status='PENDING',
        meeting_date=meeting_date,
        user_id=owner.id,
        mime_type=mime_type,
        audio_duration_seconds=audio_duration_seconds,
        notes=notes,
        folder_id=selected_folder.id if selected_folder else None,
        processing_source=processing_source,
        file_hash=file_hash,
        prompt_variables=prompt_variables,
        # Per-upload override: record the effective audio-only flag so
        # the processing task knows whether to drop the video stream
        # regardless of the server's VIDEO_RETENTION setting. True when
        # the user explicitly toggled it or when VIDEO_RETENTION is off.
        keep_audio_only=effective_audio_only,
    )
    db.session.add(recording)
    db.session.flush()  # Assign recording.id without committing transaction yet
    if prepare is not None:
        prepare(recording)

    storage_key = storage.build_recording_key(original_filename, recording.id, now=now)
    stored_object = storage.upload_local_file(
        filepath,
        storage_key,
        content_type=mime_type,
        delete_source=True,
    )
    # Intentionally do not reuse `filepath` after upload: with delete_source=True
    # the staging file may already be gone (notably on S3 backend). Workers should
    # always read from recording.audio_path via the storage facade.
    recording.audio_path = stored_object.locator

    # Add tags to recording if selected (preserve order)
    for order, tag in enumerate(selected_tags, 1):
        new_association = RecordingTag(
            recording_id=recording.id,
            tag_id=tag.id,
            order=order,
            added_at=datetime.utcnow()
        )
        db.session.add(new_association)

    if selected_folder:
        from src.api.folders import apply_group_folder_shares
        apply_group_folder_shares(recording, selected_folder)

    db.session.commit()

    if selected_tags:
        tag_names = [tag.name for tag in selected_tags]
        current_app.logger.info(f"Added {len(selected_tags)} tags to recording {recording.id}: {', '.join(tag_names)}")

    current_app.logger.info(f"Initial recording record created with ID: {recording.id}")
    return StoredRecording(recording, None)


def handle_unique_conflict(error, *, filepath, on_conflict):
    """Answer an upload whose rows hit a unique index (#476).

    Called from the caller's ``except IntegrityError`` block: rolls back,
    removes the staging copy, then returns on_conflict()'s response, or the
    500 unexpected_error_upload answer when there is none.
    """
    # Another upload committed the same unique key first (#476): keep
    # nothing of this one and let the caller answer with that recording.
    db.session.rollback()
    _discard_staging(filepath)
    replay = on_conflict() if on_conflict is not None else None
    if replay is not None:
        current_app.logger.info("Upload lost a race on a unique key; answering with the earlier recording")
        return replay
    current_app.logger.error(f"Error during file upload: {error}", exc_info=True)
    return jsonify(api_error('unexpected_error_upload')), 500


def build_ingestion_response(recording, *, owner, duplicate_warning, reuse_duplicate,
                             success_status):
    """The success answer for a stored recording: its dict, the duplicate warning, the replay flag."""
    response_data = recording.to_dict(viewer_user=owner)
    if duplicate_warning:
        response_data['duplicate_warning'] = duplicate_warning
    if reuse_duplicate:
        response_data['idempotent_replay'] = False
    return jsonify(response_data), success_status


def enqueue_ingestion_jobs(recording, *, owner, resolved_params, conversion_deferred,
                           extracted_audio_limit_mb):
    """Queue the committed recording's first job and emit its recording.created event.

    A deferred conversion queues the convert job, which carries the
    transcribe params and extracted_audio_limit_mb and queues the
    transcription itself; otherwise the transcribe job is queued. Returns
    nothing.
    """
    # Read from src.api.recordings at call time: its routes use it too, and
    # tests replace it there.
    from src.api.recordings import job_queue

    # --- Queue transcription job ---
    # Params were already resolved (incl. admin model validation) in ingest.
    job_params = resolved_params

    if conversion_deferred:
        # The convert job queues the transcription with these params.
        job_queue.enqueue(
            user_id=owner.id,
            recording_id=recording.id,
            job_type='convert',
            params={'transcribe': job_params, 'extracted_audio_limit_mb': extracted_audio_limit_mb},
            is_new_upload=True
        )
        current_app.logger.info(f"Conversion job queued for recording ID: {recording.id}")
    else:
        current_app.logger.info(f"Queueing transcription for recording {recording.id} with params: {job_params}")
        job_queue.enqueue(
            user_id=owner.id,
            recording_id=recording.id,
            job_type='transcribe',
            params=job_params,
            is_new_upload=True
        )
        current_app.logger.info(f"Transcription job queued for recording ID: {recording.id}")

    # Webhook event (#275). Fan-out happens off-request via the
    # dispatcher; this call only enqueues a delivery row per matching
    # subscription, so it is cheap and safe inside the request path.
    from src.services.webhook_dispatch import emit_recording_created
    emit_recording_created(recording)


def ingest(
    *,
    owner,
    uploaded_file=None,
    form,
    original_filename=None,
    processing_source='upload',
    success_status=202,
    reuse_duplicate=False,
    prepare=None,
    on_conflict=None,
    source=None,
    enforce_upload_limits=True,
):
    """Run an upload through every step above; ``ingest_uploaded_recording`` is its facade.

    Takes the facade's arguments and returns its answer (see its docstring).
    The outer error handling stays here, around every step: a
    RequestEntityTooLarge is answered with the limit that fired, an
    IntegrityError by :func:`handle_unique_conflict`, and any other error
    with the 500 unexpected_error_upload answer, each after removing the
    staging copy in ``filepath``.
    """
    # Read from src.api.recordings at call time: its routes use USE_ASR_ENDPOINT
    # and get_storage_service too, init sets chunking_service, and the join
    # path lives there.
    from src.api.recordings import (
        USE_ASR_ENDPOINT, _ingest_join_part, chunking_service, get_storage_service,
    )

    filepath = None
    try:
        from src.services.recording_source import coerce_source
        file = coerce_source(uploaded_file=uploaded_file, source=source)
        if file is None:
            return jsonify(api_error('no_file_provided')), 400
        if not original_filename:
            original_filename = file.filename
        if not original_filename:
            return jsonify(api_error('no_file_selected')), 400

        # Joining several files into one recording (upload dialog). A retry
        # of a part whose group was already joined gets that recording.
        from src.services.upload_join import parse_join_fields, joined_recording, JoinError
        try:
            join = parse_join_fields(form)
        except JoinError as e:
            return jsonify({'error': str(e)}), 400
        if join:
            already = joined_recording(owner.id, join[0])
            if already is not None:
                response_data = already.to_dict(viewer_user=owner)
                response_data['idempotent_replay'] = True
                return jsonify(response_data), success_status

        safe_filename = shorten_filename(secure_filename(original_filename))
        storage = get_storage_service()
        staging_dir = storage.get_staging_dir()
        filepath = os.path.join(
            staging_dir,
            f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}_{safe_filename}",
        )

        size_policy = resolve_size_policy(
            file=file,
            form=form,
            original_filename=original_filename,
            enforce_upload_limits=enforce_upload_limits,
        )
        keep_audio_only_flag = size_policy.keep_audio_only_flag
        effective_audio_only = size_policy.effective_audio_only
        is_likely_video_by_ext = size_policy.is_likely_video_by_ext
        original_file_size = size_policy.original_file_size
        regular_limit_mb = size_policy.regular_limit_mb
        effective_limit_mb = size_policy.effective_limit_mb
        connector_specs = size_policy.connector_specs
        should_enforce_size_limit = size_policy.should_enforce_size_limit
        enforce_size_policy(size_policy, enforce_upload_limits=enforce_upload_limits)

        # Storage quota (#413). A recording made in this browser was checked
        # when it started and is not refused at the end, so it is never lost;
        # only a signed-in browser can say so, never an API token.
        from src.services.storage_quota import check_room, StorageQuotaExceeded
        from src.utils.token_auth import current_api_token
        recorded_in_app = (str(form.get('from_recorder', '')).lower() == 'true'
                           and current_api_token() is None)
        if not recorded_in_app:
            try:
                check_room(owner, original_file_size)
            except StorageQuotaExceeded as quota_error:
                current_app.logger.info(f"Upload refused for user {owner.id}: {quota_error}")
                return quota_error.response()

        file.save(filepath)
        current_app.logger.info(f"File saved to {filepath}")

        # An empty part has nothing to decode: refuse it before the probe and
        # the conversion (#482).
        if os.path.getsize(filepath) == 0:
            current_app.logger.info(f"Upload refused for user {owner.id}: the file is empty")
            _discard_staging(filepath)
            return jsonify(api_error('empty_file')), 400

        # Compute file hash on the ORIGINAL upload before any conversion/compression.
        # Lossy re-encoding (e.g. FLAC→MP3) produces different bytes each run,
        # so hashing after conversion would miss duplicates.
        duplicate = lookup_duplicate(
            owner=owner,
            filepath=filepath,
            form=form,
            processing_source=processing_source,
            original_filename=original_filename,
            reuse_duplicate=reuse_duplicate,
        )
        file_hash = duplicate.file_hash
        duplicate_warning = duplicate.duplicate_warning

        if duplicate.existing and reuse_duplicate:
            return handle_duplicate_replay(
                duplicate.existing,
                owner=owner,
                filepath=filepath,
                file_hash=file_hash,
                success_status=success_status,
            )

        # --- Convert files only when chunking is needed ---

        # Check if chunking will be needed for this file (uses connector-aware logic)
        needs_chunking_for_processing = bool(
            chunking_service and
            chunking_service.needs_chunking(filepath, USE_ASR_ENDPOINT, connector_specs)
        )

        upload_probe = probe_upload(filepath=filepath, original_filename=original_filename)
        codec_info = upload_probe.codec_info
        probe_error = upload_probe.probe_error
        has_video = upload_probe.has_video

        size_recheck = recheck_size_after_probe(
            codec_info=codec_info,
            has_video=has_video,
            original_filename=original_filename,
            is_likely_video_by_ext=is_likely_video_by_ext,
            effective_limit_mb=effective_limit_mb,
            regular_limit_mb=regular_limit_mb,
            should_enforce_size_limit=should_enforce_size_limit,
            original_file_size=original_file_size,
        )
        is_likely_video_by_ext = size_recheck.is_likely_video_by_ext
        effective_limit_mb = size_recheck.effective_limit_mb
        if size_recheck.too_large:
            raise RequestEntityTooLarge()

        has_video = has_video_after_failed_probe(
            codec_info=codec_info,
            probe_error=probe_error,
            has_video=has_video,
            original_filename=original_filename,
        )
        conversion = decide_conversion(
            codec_info=codec_info,
            has_video=has_video,
            keep_audio_only_flag=keep_audio_only_flag,
            join=join,
            original_filename=original_filename,
            should_enforce_size_limit=should_enforce_size_limit,
            enforce_upload_limits=enforce_upload_limits,
            effective_audio_only=effective_audio_only,
            is_likely_video_by_ext=is_likely_video_by_ext,
            regular_limit_mb=regular_limit_mb,
            original_file_size=original_file_size,
        )
        keep_video_for_this_upload = conversion.keep_video_for_this_upload
        extracted_audio_limit_mb = conversion.extracted_audio_limit_mb
        conversion_deferred = conversion.conversion_deferred
        if conversion.convert_in_request:
            converted = convert_upload_in_request(
                filepath,
                original_filename=original_filename,
                codec_info=codec_info,
                needs_chunking_for_processing=needs_chunking_for_processing,
                connector_specs=connector_specs,
            )
            if converted.error_response is not None:
                return converted.error_response
            filepath = converted.filepath

        # Get final file size (of original or converted file)
        final_file_size = os.path.getsize(filepath)

        extracted_audio_response = check_extracted_audio_size(
            filepath=filepath,
            final_file_size=final_file_size,
            extracted_audio_limit_mb=extracted_audio_limit_mb,
            conversion_deferred=conversion_deferred,
            regular_limit_mb=regular_limit_mb,
        )
        if extracted_audio_response is not None:
            return extracted_audio_response

        # (file_hash and duplicate_warning already computed above, before conversion)

        # Determine MIME type of the final file
        mime_type = guess_media_type(filepath)

        # For a retained-video file, derive the MIME from the file's ACTUAL
        # container via the shared probe-driven resolver (the same one the
        # processing task uses) rather than the extension guess, which
        # mislabels ambiguous containers — notably '.webm', registered as
        # audio/webm in app.py — and would hide the video player in the UI.
        # We already have codec_info from the probe above, so no second probe.
        if keep_video_for_this_upload:
            from src.utils.mime import resolve_media_mime
            corrected = resolve_media_mime(filepath, codec_info=codec_info, has_video=True)
            if corrected != mime_type:
                current_app.logger.info(f"Resolved video mime {mime_type!r} -> {corrected!r} from container for {filepath}")
            mime_type = corrected

        # The extension is the uploader's choice: an audio file named .html
        # was stored as text/html (#438). A guess that is not audio or video
        # is replaced by the type of the contents.
        if not (mime_type or '').startswith(('audio/', 'video/')):
            from src.utils.mime import resolve_media_mime
            mime_type = served_media_mime(resolve_media_mime(filepath, codec_info=codec_info))

        current_app.logger.info(f"Final MIME type: {mime_type} for file {filepath}")

        # Cache media duration while the upload is still available locally.
        # This avoids later S3 materialization just to serialize API responses.
        audio_duration_seconds = None
        try:
            detected_duration = get_duration(filepath, timeout=30)
            if detected_duration is not None and detected_duration > 0:
                audio_duration_seconds = float(detected_duration)
        except Exception as e:
            current_app.logger.warning(f"Could not determine duration for upload {original_filename}: {e}")

        # Get notes from the form
        notes = form.get('notes')

        # Get optional user-provided title and meeting_date
        user_title = form.get('title')
        user_meeting_date = form.get('meeting_date')

        # Get file's lastModified timestamp from client (milliseconds since epoch)
        file_last_modified = form.get('file_last_modified')
        # Client timezone offset (JS getTimezoneOffset(): minutes to add to
        # local time to reach UTC). Used to convert filename wall-clock dates
        # to the naive-UTC storage convention.
        client_tz_offset = form.get('client_tz_offset')
        # The browser's IANA zone (#412): saved for an uploader in 'auto' mode so
        # the title of this very recording already uses it. Only for the
        # uploader's own recordings, never for an owner uploaded on behalf of.
        client_timezone = form.get('client_timezone')
        if client_timezone and current_user.is_authenticated and owner.id == current_user.id:
            from src.utils.timezones import record_browser_timezone
            if record_browser_timezone(owner, client_timezone):
                db.session.commit()

        selection = select_tags_and_folder(form=form, owner=owner)
        selected_tags = selection.selected_tags
        selected_folder = selection.selected_folder

        # Get ASR advanced options if provided
        language = form.get('language', '')
        min_speakers = form.get('min_speakers') or None
        max_speakers = form.get('max_speakers') or None
        hotwords = form.get('hotwords', '').strip() or None
        initial_prompt = form.get('initial_prompt', '').strip() or None
        transcription_model = form.get('transcription_model', '').strip() or None

        # Per-recording prompt-template variables. Sent as a JSON string from
        # the upload form so multiple values fit in a single form field. The
        # sanitiser enforces identifier-shaped keys and per-value / total
        # size caps so an arbitrary JSON blob cannot bloat the column.
        from src.utils.prompt_variables import sanitize_variable_values
        prompt_variables = None
        raw_prompt_variables = form.get('prompt_variables')
        if raw_prompt_variables:
            try:
                parsed_vars = json.loads(raw_prompt_variables)
                prompt_variables = sanitize_variable_values(parsed_vars)
            except (TypeError, ValueError):
                current_app.logger.warning(
                    f"Could not parse prompt_variables form field as JSON: {raw_prompt_variables[:200]}"
                )

        # Convert to int if provided
        if min_speakers:
            try:
                min_speakers = int(min_speakers)
            except (ValueError, TypeError):
                min_speakers = None
        if max_speakers:
            try:
                max_speakers = int(max_speakers)
            except (ValueError, TypeError):
                max_speakers = None

        # Resolve the full transcribe param set through the shared chain used by
        # every ingestion path: per-request form value > tag > folder > env >
        # account default (see resolve_transcription_params). Folder defaults
        # fill any gaps a tag left open (rather than being ignored when a tag is
        # present), and an empty language falls through the chain rather than
        # forcing auto-detect. The resolver also applies the admin-curated model
        # validation/default, so no separate _resolve_transcription_model call is
        # needed below.
        resolved_params = resolve_transcription_params(
            overrides={
                'language': language,
                'min_speakers': min_speakers,
                'max_speakers': max_speakers,
                'hotwords': hotwords,
                'initial_prompt': initial_prompt,
                'transcription_model': transcription_model,
            },
            tags=selected_tags,
            folder=selected_folder,
            owner=owner,
        )

        # Create initial database entry
        now = datetime.utcnow()

        # Determine meeting_date: prefer user-provided, then client lastModified, then file metadata, then current time
        meeting_date = None

        # First try user-provided meeting_date (ISO 8601 format)
        if user_meeting_date:
            try:
                parsed = datetime.fromisoformat(user_meeting_date.replace('Z', '+00:00'))
                # meeting_date is stored as naive UTC: convert zone-aware input
                # to UTC before stripping the timezone
                meeting_date = to_utc_naive(parsed)
                current_app.logger.info(f"Using user-provided meeting_date: {meeting_date}")
            except (ValueError, TypeError) as e:
                current_app.logger.warning(f"Could not parse user meeting_date '{user_meeting_date}': {e}")

        # Then try parsing a date from the filename when the user opted in
        # (#342). The pattern is explicit user intent, so it outranks the
        # automatic sources (lastModified / embedded metadata) below.
        if not meeting_date and owner.parse_filename_dates and original_filename:
            from src.utils.filename_dates import parse_filename_date
            tz_offset = None
            if client_tz_offset is not None:
                try:
                    tz_offset = int(client_tz_offset)
                except (TypeError, ValueError):
                    tz_offset = None
            meeting_date = parse_filename_date(
                original_filename,
                pattern_key=owner.filename_date_pattern or 'auto',
                custom_regex=owner.filename_date_regex,
                tz_offset_minutes=tz_offset,
                tz_name=form.get('client_timezone'),
            )
            if meeting_date:
                current_app.logger.info(f"Using filename-parsed meeting_date: {meeting_date}")

        # Then try client-provided file lastModified (most reliable for uploads)
        if not meeting_date and file_last_modified:
            try:
                # JavaScript lastModified is in milliseconds since epoch; convert
                # to naive UTC (the storage convention) rather than server-local
                # so the viewer's browser renders the file's original local time
                # regardless of the server's timezone
                timestamp_ms = int(file_last_modified)
                meeting_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
                current_app.logger.info(f"Using client file lastModified: {meeting_date}")
            except (ValueError, TypeError, OSError) as e:
                current_app.logger.warning(f"Could not parse file_last_modified '{file_last_modified}': {e}")

        # Fall back to file metadata (creation_time, date tags, etc.)
        if not meeting_date:
            meeting_date = to_utc_naive(get_creation_date(filepath, use_file_mtime=False))
            if meeting_date:
                current_app.logger.info(f"Using file metadata creation date: {meeting_date}")

        # Final fallback to current time
        if not meeting_date:
            meeting_date = now
            current_app.logger.debug("No file date available, using current time")

        if join:
            return _ingest_join_part(
                owner, join, filepath, storage, now,
                original_filename=original_filename, file_size=final_file_size,
                mime_type=mime_type, duration=audio_duration_seconds, meeting_date=meeting_date,
                title=user_title, notes=notes, folder=selected_folder, tags=selected_tags,
                prompt_variables=prompt_variables, transcribe_params=resolved_params,
                duplicate_warning=duplicate_warning, success_status=success_status,
            )

        stored = store_recording(
            owner=owner,
            storage=storage,
            filepath=filepath,
            now=now,
            recorded_in_app=recorded_in_app,
            original_filename=original_filename,
            user_title=user_title,
            final_file_size=final_file_size,
            meeting_date=meeting_date,
            mime_type=mime_type,
            audio_duration_seconds=audio_duration_seconds,
            notes=notes,
            selected_folder=selected_folder,
            selected_tags=selected_tags,
            processing_source=processing_source,
            file_hash=file_hash,
            prompt_variables=prompt_variables,
            effective_audio_only=effective_audio_only,
            prepare=prepare,
        )
        if stored.error_response is not None:
            return stored.error_response
        recording = stored.recording

        enqueue_ingestion_jobs(
            recording,
            owner=owner,
            resolved_params=resolved_params,
            conversion_deferred=conversion_deferred,
            extracted_audio_limit_mb=extracted_audio_limit_mb,
        )

        return build_ingestion_response(
            recording,
            owner=owner,
            duplicate_warning=duplicate_warning,
            reuse_duplicate=reuse_duplicate,
            success_status=success_status,
        )

    except RequestEntityTooLarge:
        _discard_staging(filepath)
        # Report the effective limit that fired (regular vs audio-only)
        # so the frontend can show the right message. effective_limit_mb
        # is set at the top of the function before the size check.
        try:
            limit_mb = float(effective_limit_mb)
        except (NameError, TypeError, ValueError):
            limit_mb = float(current_app.config['MAX_CONTENT_LENGTH']) / (1024 * 1024)
        audio_only_mode = False
        try:
            audio_only_mode = bool(effective_audio_only) and bool(is_likely_video_by_ext)
        except NameError:
            pass
        current_app.logger.warning(
            f"Upload failed: File too large (>{limit_mb}MB, audio_only={audio_only_mode})"
        )
        return jsonify({
            **api_error('file_too_large_max', maxSize=format_number(limit_mb)),
            'max_size_mb': limit_mb,
            'effective_limit_mb': limit_mb,
            'audio_only_mode': audio_only_mode,
        }), 413
    except IntegrityError as e:
        return handle_unique_conflict(e, filepath=filepath, on_conflict=on_conflict)
    except Exception as e:
        db.session.rollback()
        _discard_staging(filepath)
        current_app.logger.error(f"Error during file upload: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error_upload')), 500


# Hot-applied System Settings (high priority) with environment fallback.
import sys
_self = sys.modules[__name__]
from src.utils.system_settings import make_module_getattr as _make_getattr
_SYSTEM_SETTING_MAP = {
    "VIDEO_RETENTION": ('video_retention', 'VIDEO_RETENTION', False),
    "VIDEO_PASSTHROUGH_ASR": ('video_passthrough_asr', 'VIDEO_PASSTHROUGH_ASR', False),
}
__getattr__ = _make_getattr(_SYSTEM_SETTING_MAP)
