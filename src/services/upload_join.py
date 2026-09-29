"""Joining several uploaded files into one recording.

The upload dialog sends each file with ``join_group`` (an id the browser
picks), ``join_index`` (its 0-based position) and ``join_count``. Each file
runs through the normal upload pipeline and is stored as an UploadJoinPart.
The request that completes the group claims every part in one conditional
UPDATE, so parallel or retried uploads can never join a group twice, creates
the recording from that request's upload options, and queues the existing
``merge`` job with the parts. The merge job joins the audio and queues one
transcription with the options the upload resolved.

Parts of a group that never completes (a closed tab, a failed part) are
removed by the hourly cleanup after PART_TTL_HOURS, or at once when the
dialog discards the group.
"""

import os
import re
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import OperationalError

from src.database import db
from src.models import Recording, RecordingTag
from src.models.upload_join import UploadJoinPart

MAX_JOIN_PARTS = 20
PART_TTL_HOURS = 24
_GROUP_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')


class JoinError(ValueError):
    """An invalid join request. The message is safe to show."""


def parse_join_fields(form):
    """(group, index, count) from an upload form, None for a normal upload."""
    group = (form.get('join_group') or '').strip()
    if not group:
        return None
    if not _GROUP_RE.match(group):
        raise JoinError('Invalid join group.')
    try:
        index = int(form.get('join_index', ''))
        count = int(form.get('join_count', ''))
    except (TypeError, ValueError):
        raise JoinError('join_index and join_count must be integers.')
    if not 2 <= count <= MAX_JOIN_PARTS:
        raise JoinError(f'A joined recording takes between 2 and {MAX_JOIN_PARTS} files.')
    if not 0 <= index < count:
        raise JoinError('join_index is out of range.')
    return group, index, count


def joined_recording(user_id, group):
    """The recording a group was already joined into, or None."""
    part = (UploadJoinPart.query
            .filter_by(user_id=user_id, group_id=group)
            .filter(UploadJoinPart.claimed_at.isnot(None))
            .first())
    if part is None or part.recording_id is None:
        return None
    return db.session.get(Recording, part.recording_id)


def received_count(user_id, group):
    return UploadJoinPart.query.filter_by(user_id=user_id, group_id=group).count()


def store_part(owner, join, filepath, *, original_filename, file_size, mime_type,
               duration, meeting_date, storage, now=None):
    """Store one uploaded part; a retried upload of the same part replaces it. Commits."""
    group, index, count = join
    now = now or datetime.utcnow()
    part = UploadJoinPart.query.filter_by(user_id=owner.id, group_id=group, part_index=index).first()
    previous_locator = None
    if part is None:
        part = UploadJoinPart(user_id=owner.id, group_id=group, part_index=index, part_count=count)
        db.session.add(part)
        db.session.flush()
    else:
        previous_locator = part.audio_path

    key = storage.build_recording_key(original_filename, f'join-{part.id}', now=now)
    stored = storage.upload_local_file(filepath, key, content_type=mime_type, delete_source=True)
    part.audio_path = stored.locator
    part.part_count = count
    part.original_filename = original_filename
    part.file_size = file_size
    part.mime_type = mime_type
    part.audio_duration_seconds = duration
    part.meeting_date = meeting_date
    part.created_at = now
    db.session.commit()

    if previous_locator and previous_locator != part.audio_path:
        try:
            storage.delete(previous_locator, missing_ok=True)
        except Exception as e:
            current_app.logger.warning(f"Could not delete replaced join part media {previous_locator}: {e}")
    return part


def claim_group(user_id, group, count):
    """Claim a complete group for joining; its parts in order, or None.

    None when parts are missing or another request claimed the group first.
    The claim is not committed: the caller creates the recording in the same
    transaction, so a failure there releases the claim.
    """
    parts = (UploadJoinPart.query
             .filter_by(user_id=user_id, group_id=group)
             .filter(UploadJoinPart.claimed_at.is_(None))
             .order_by(UploadJoinPart.part_index)
             .all())
    if [p.part_index for p in parts] != list(range(count)):
        return None
    if any(p.part_count != count or not p.audio_path for p in parts):
        return None
    try:
        claimed = (UploadJoinPart.query
                   .filter_by(user_id=user_id, group_id=group)
                   .filter(UploadJoinPart.claimed_at.is_(None))
                   .update({'claimed_at': datetime.utcnow()}, synchronize_session=False))
    except OperationalError as e:
        # SQLite refuses a write that races another writer; the other
        # request is completing this group.
        db.session.rollback()
        current_app.logger.info(f"Join group {group} is being claimed by another request: {e}")
        return None
    if claimed != count:
        db.session.rollback()
        return None
    return parts


def _joined_filename(first_name):
    stem = os.path.splitext(os.path.basename(first_name or 'recording'))[0] or 'recording'
    return f"{stem}.m4a"


def create_joined_recording(owner, parts, *, title=None, notes=None, folder=None, tags=(),
                            prompt_variables=None, transcribe_params=None):
    """Create the recording for a claimed group and queue the merge job. Commits."""
    from src.services.job_queue import job_queue
    from src.utils.titles import resolve_upload_title

    now = datetime.utcnow()
    original_filename = _joined_filename(parts[0].original_filename)
    meeting_dates = [p.meeting_date for p in parts if p.meeting_date]
    durations = [p.audio_duration_seconds for p in parts]
    recording = Recording(
        audio_path=None,
        original_filename=original_filename,
        title=resolve_upload_title(title, original_filename),
        file_size=sum(p.file_size or 0 for p in parts),
        status='QUEUED',
        meeting_date=min(meeting_dates) if meeting_dates else now,
        user_id=owner.id,
        mime_type='audio/mp4',
        audio_duration_seconds=sum(durations) if all(d for d in durations) else None,
        notes=notes,
        folder_id=folder.id if folder else None,
        processing_source='upload',
        prompt_variables=prompt_variables,
        keep_audio_only=True,  # the merge produces audio only
    )
    db.session.add(recording)
    db.session.flush()
    for order, tag in enumerate(tags, 1):
        db.session.add(RecordingTag(recording_id=recording.id, tag_id=tag.id, order=order, added_at=now))
    for p in parts:
        p.recording_id = recording.id
    db.session.commit()

    try:
        job_queue.enqueue(
            user_id=owner.id,
            recording_id=recording.id,
            job_type='merge',
            params={'part_ids': [p.id for p in parts], 'transcribe_params': transcribe_params},
            is_new_upload=True,
        )
    except Exception as e:
        current_app.logger.error(f"Join enqueue failed for recording {recording.id}: {e}")
        recording.status = 'FAILED'
        recording.error_message = f"Processing failed: {e}"
        db.session.commit()
    return recording


def _delete_media(storage, part):
    if part.audio_path:
        try:
            storage.delete(part.audio_path, missing_ok=True)
        except Exception as e:
            current_app.logger.warning(f"Could not delete join part media {part.audio_path}: {e}")
            return False
        part.audio_path = None
    return True


def release_parts_media(parts, storage):
    """After a successful join: drop the parts' media, keep the rows. Commits."""
    for p in parts:
        _delete_media(storage, p)
    db.session.commit()


def discard_group(user_id, group, storage=None):
    """Remove a group's unjoined parts, for a join the dialog gave up. Commits."""
    if storage is None:
        from src.services.storage import get_storage_service
        storage = get_storage_service()
    parts = (UploadJoinPart.query
             .filter_by(user_id=user_id, group_id=group)
             .filter(UploadJoinPart.claimed_at.is_(None))
             .all())
    for p in parts:
        _delete_media(storage, p)
        db.session.delete(p)
    db.session.commit()
    return len(parts)


def cleanup_stale_parts(max_age_hours=PART_TTL_HOURS, storage=None):
    """Remove parts older than the TTL; returns how many rows were removed.

    Unjoined parts lose media and row. A joined part keeps its media while
    its recording is still being merged, and is otherwise removed too.
    """
    if storage is None:
        from src.services.storage import get_storage_service
        storage = get_storage_service()
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    removed = 0
    for p in UploadJoinPart.query.filter(UploadJoinPart.created_at < cutoff).all():
        if p.claimed_at is not None and p.audio_path:
            rec = db.session.get(Recording, p.recording_id) if p.recording_id else None
            if rec is not None and rec.status == 'QUEUED' and not rec.audio_path:
                continue  # still waiting for its merge
        if not _delete_media(storage, p):
            continue
        db.session.delete(p)
        removed += 1
    db.session.commit()
    return removed
