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
"""

from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from flask import current_app, jsonify

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
