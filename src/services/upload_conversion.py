"""Converting an uploaded file after the upload has been answered (#543).

The upload request saves and probes the file, stores it as it arrived and
queues a 'convert' job. The job runs the conversion an upload used to run
before answering (codec conversion, compression, audio extraction), stores the
result in place of the original and then queues the transcription. A file that
ffprobe cannot read is still converted, and refused, in the request.
"""

import os
import shutil
import uuid
from datetime import datetime

from flask import current_app

from src.database import db
from src.models import Recording
from src.utils.log_filters import loggable_error
from src.utils.ffmpeg_utils import (AUDIO_DECODE_FAILED_MESSAGE, FFmpegError, FFmpegNotFoundError,
                                    FFmpegTimeoutError)


class ConversionFailed(Exception):
    """A conversion that a retry would not fix; the message is shown to the user."""

    permanent = True


def _stored_mime(path, codec_info=None):
    from src.utils.mime import guess_media_type, resolve_media_mime, served_media_mime
    mime_type = guess_media_type(path)
    if not (mime_type or '').startswith(('audio/', 'video/')):
        mime_type = served_media_mime(resolve_media_mime(path, codec_info=codec_info))
    return mime_type


def _mark_cut_applied():
    """Replace the range in the running job's params with 'trim_applied'.

    Called in the transaction that stores the kept span, so the job row and
    the recording change together. Outside a job (a direct call) there is no
    row to change.
    """
    import json
    from src.models import ProcessingJob
    from src.services import job_control

    job_id = getattr(job_control.current(), 'job_id', None)
    if job_id is None:
        return
    job = db.session.get(ProcessingJob, job_id)
    if job is None:
        return
    try:
        stored = json.loads(job.params) if job.params else {}
    except (TypeError, ValueError):
        stored = {}
    for key in ('trim_start', 'trim_end', 'retain_video'):
        stored.pop(key, None)
    stored['trim_applied'] = True
    job.params = json.dumps(stored)


def convert_uploaded_recording(recording_id, params):
    """Convert a recording's stored upload, then queue its transcription.

    params holds the transcription parameters resolved at upload time
    ('transcribe') and, when only the extracted audio of a video has to fit
    the upload limit, that limit in MB ('extracted_audio_limit_mb'). A real
    cut also carries 'trim_start' and/or 'trim_end', whole seconds. The
    encode keeps that span; a range that is the whole file carries neither.
    'retain_video' stores that span as a video, so playback and the
    transcription share the shorter file.

    The cut is applied once. The commit that stores the kept span also
    replaces the range in the job's own params with 'trim_applied', so a job
    that runs again after that commit (recovered after a crash, or retried
    because the transcription could not be queued) converts nothing and only
    queues the transcription.
    """
    from src.config.app_config import USE_NEW_TRANSCRIPTION_ARCHITECTURE, chunking_service
    from src.services.job_queue import job_queue
    from src.services.recording_trim import TrimError
    from src.services.storage import get_storage_service
    from src.utils.audio_conversion import convert_if_needed
    from src.utils.ffprobe import get_duration
    from src.utils.localization import format_number, translate

    recording = db.session.get(Recording, recording_id)
    if recording is None:
        return
    storage = get_storage_service()
    old_locator = recording.audio_path
    original_filename = recording.original_filename or os.path.basename(old_locator or 'recording.bin')
    new_locator = None
    language = getattr(recording.owner, 'ui_language', None)
    stored_mime = recording.mime_type
    trim_start = params.get('trim_start')
    trim_end = params.get('trim_end')
    already_cut = bool(params.get('trim_applied'))
    real_cut = not already_cut and (bool(trim_start) or trim_end is not None)
    if already_cut:
        current_app.logger.info(f"Recording {recording_id} already has its kept span; the conversion is not run again")

    with storage.materialize(old_locator) as materialized:
        local_path = materialized.local_path
        before = os.stat(local_path)
        use_asr_endpoint = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
        connector_specs = None
        if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
            try:
                from src.services.transcription import get_registry
                connector = get_registry().get_active_connector()
                if connector:
                    connector_specs = connector.specifications
            except Exception as e:
                current_app.logger.warning(f"Could not get connector specs for conversion: {e}")
        needs_chunking = bool(
            chunking_service and chunking_service.needs_chunking(local_path, use_asr_endpoint, connector_specs)
        )
        try:
            if already_cut:
                output_path = local_path
                result = None
            elif params.get('retain_video'):
                from src.services.recording_trim import cut_upload_span
                output_path = cut_upload_span(local_path, trim_start, trim_end)
                result = None
            else:
                result = convert_if_needed(
                    local_path,
                    original_filename=original_filename,
                    needs_chunking=needs_chunking,
                    is_asr_endpoint=use_asr_endpoint,
                    delete_original=False,
                    connector_specs=connector_specs,
                    keep_start=trim_start,
                    keep_end=trim_end,
                )
                output_path = result.output_path
        except FFmpegNotFoundError as e:
            current_app.logger.error(f"FFmpeg not found while converting recording {recording_id}: {loggable_error(e, local_path, recording_id=recording_id)}")
            raise ConversionFailed(translate('server.apiError.audio_converter_missing', language)) from None
        except FFmpegTimeoutError as e:
            current_app.logger.error(f"Conversion of recording {recording_id} timed out: {loggable_error(e, local_path, recording_id=recording_id)}")
            raise ConversionFailed(translate('server.apiError.audio_conversion_timeout', language)) from None
        except FFmpegError as e:
            # FFmpeg's output names its build and the file's path: it stays in the log.
            current_app.logger.error(f"Conversion of recording {recording_id} failed: {loggable_error(e, local_path, recording_id=recording_id)}")
            raise ConversionFailed(AUDIO_DECODE_FAILED_MESSAGE) from None
        except TrimError:
            # The cut of a kept video wrote nothing.
            current_app.logger.error(f"The kept span of recording {recording_id} could not be written")
            raise ConversionFailed(AUDIO_DECODE_FAILED_MESSAGE) from None

        converted = output_path != local_path
        try:
            final_size = os.path.getsize(output_path)
            limit_mb = params.get('extracted_audio_limit_mb')
            if limit_mb and final_size > limit_mb * 1024 * 1024:
                raise ConversionFailed(translate(
                    'server.apiError.extracted_audio_too_large', language,
                    size=format_number(final_size / 1024 / 1024), limit=format_number(limit_mb),
                ))
            # A remux in place (an MP3 without a VBR header) changes the
            # file itself: a local file is already stored, a downloaded copy
            # is stored again.
            after = os.stat(output_path)
            changed_copy = (not converted and materialized.cleanup_required
                            and (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns))
            mime_type = _stored_mime(output_path) if converted else stored_mime
            duration = None
            try:
                duration = get_duration(output_path, timeout=30)
            except Exception as e:
                current_app.logger.warning(f"Could not determine duration of converted recording {recording_id}: {loggable_error(e, output_path, local_path, recording_id=recording_id)}")

            if converted or changed_copy:
                stem = os.path.splitext(original_filename)[0] or 'recording'
                key = storage.build_recording_key(stem + os.path.splitext(output_path)[1], recording_id,
                                                  now=datetime.utcnow())
                if converted:
                    # FFmpeg writes beside a local upload, which can be the
                    # very path of the new key: move it out of the way first.
                    staged = os.path.join(storage.get_staging_dir(),
                                          f"{uuid.uuid4().hex}_{os.path.basename(output_path)}")
                    shutil.move(output_path, staged)
                    output_path = staged
                new_locator = storage.upload_local_file(output_path, key, content_type=mime_type,
                                                        delete_source=converted).locator
        finally:
            if converted and os.path.exists(output_path):
                os.remove(output_path)

    recording = db.session.get(Recording, recording_id)
    if recording is None:
        # Deleted while the file was converted: drop the new copy as well.
        if new_locator:
            storage.delete(new_locator)
        return
    if new_locator:
        recording.audio_path = new_locator
    recording.file_size = final_size
    recording.mime_type = mime_type
    if duration:
        recording.audio_duration_seconds = float(duration)
    if real_cut:
        _mark_cut_applied()
    db.session.commit()
    if new_locator and old_locator != new_locator:
        try:
            storage.delete(old_locator)
        except Exception as e:
            current_app.logger.warning(f"Could not delete the original upload of recording {recording_id}: {loggable_error(e, old_locator, recording_id=recording_id)}")
    if result is not None and (result.was_converted or result.was_compressed):
        current_app.logger.info(
            f"Converted recording {recording_id}: {result.original_codec} -> {result.final_codec}, "
            f"{result.size_change_label}"
        )
    elif params.get('retain_video') and converted:
        current_app.logger.info(f"Stored the kept span of the video of recording {recording_id}")

    # A safe point: the converted audio is stored, and a canceled conversion
    # queues no transcription.
    from src.services import job_control
    job_control.current().check_cancel()

    job_queue.enqueue(
        user_id=recording.user_id,
        recording_id=recording.id,
        job_type='transcribe',
        params=params.get('transcribe') or {},
        is_new_upload=True,
    )
