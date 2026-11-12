"""Daily removal of recordings that have no owner (#572).

Before #572 an admin's user delete removed the user's media but kept the
recording rows with user_id NULL: transcript, summary, notes, embeddings and
shares stayed in the database with nobody able to open or delete them. Every
path that creates a recording sets an owner, so a recording without one is
always such a leftover.

The cleanup runs in two passes, one day apart. The first run that finds a
recording without an owner only marks it (``ownerless_since``); a later run
deletes it if it still has no owner and the mark is at least
OWNERLESS_GRACE old. A recording that gains an owner loses its mark, and so
does one with work in flight, which therefore starts its wait again.

Only database rows are candidates. Media files on disk that no row refers to
are counted for the log and never deleted here: a file cannot tell which
upload it belongs to, and a guess that is wrong destroys a user's recording.
An admin can remove them with scripts/clean_unreferenced_files.py, which has
a minimum age and a confirmation prompt.
"""

from datetime import datetime, timedelta

from flask import current_app
from src.database import db

# Time between the first run that marks a recording and the run that may
# delete it. A little under a day, so the next 2 AM run qualifies.
OWNERLESS_GRACE = timedelta(hours=20)

# Upper bound on files the unreferenced-file count looks at.
UNREFERENCED_SCAN_LIMIT = 200_000

ACTIVE_JOB_STATUSES = ('queued', 'processing')
ACTIVE_SESSION_STATUSES = ('recording', 'finalizing')


def _in_flight_ids(recording_ids):
    """The ids among recording_ids with work that may still touch them.

    A processing job queued or running, a recording session still receiving
    or stitching chunks, and an upload-join part whose file has not been
    merged yet.
    """
    from src.models import RecordingSession
    from src.models.processing_job import ProcessingJob
    from src.models.upload_join import UploadJoinPart

    ids = list(recording_ids)
    busy = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        busy.update(rid for (rid,) in db.session.query(ProcessingJob.recording_id).filter(
            ProcessingJob.recording_id.in_(chunk),
            ProcessingJob.status.in_(ACTIVE_JOB_STATUSES)).distinct())
        busy.update(rid for (rid,) in db.session.query(RecordingSession.finalized_recording_id).filter(
            RecordingSession.finalized_recording_id.in_(chunk),
            RecordingSession.status.in_(ACTIVE_SESSION_STATUSES)).distinct())
        busy.update(rid for (rid,) in db.session.query(UploadJoinPart.recording_id).filter(
            UploadJoinPart.recording_id.in_(chunk),
            UploadJoinPart.audio_path.isnot(None)).distinct())
    return busy


def _clear_marks(condition):
    """Clear ownerless_since where condition holds. Returns the row count.

    A bulk UPDATE, so the change feed and webhooks see nothing: the mark is
    bookkeeping of this cleanup, not a change to the recording.
    """
    from src.models import Recording
    count = (Recording.query
             .filter(Recording.ownerless_since.isnot(None), condition)
             .update({Recording.ownerless_since: None}, synchronize_session=False))
    db.session.commit()
    return count


def sweep_ownerless_recordings(now=None, storage=None):
    """Mark, and after the grace period delete, recordings without an owner.

    Returns a dict of counts: candidates, marked, waiting, deleted,
    skipped_in_flight, unmarked (marks cleared because an owner returned or
    work was in flight), errors and unreferenced_files (None when the count
    could not be taken).
    """
    from src.models import Recording
    from src.services.recording_deletion import (cleanup_orphaned_speakers_quietly,
                                                 delete_recording_completely)

    now = now or datetime.utcnow()
    cutoff = now - OWNERLESS_GRACE
    stats = {'candidates': 0, 'marked': 0, 'waiting': 0, 'deleted': 0, 'skipped_in_flight': 0,
             'unmarked': 0, 'errors': 0, 'unreferenced_files': None}

    # A recording that has an owner again keeps nothing of this cleanup.
    stats['unmarked'] += _clear_marks(Recording.user_id.isnot(None))

    candidates = (db.session.query(Recording.id, Recording.ownerless_since)
                  .filter(Recording.user_id.is_(None))
                  .order_by(Recording.id).all())
    stats['candidates'] = len(candidates)

    busy = _in_flight_ids(rid for rid, _ in candidates) if candidates else set()
    if busy:
        stats['skipped_in_flight'] = len(busy)
        stats['unmarked'] += _clear_marks(Recording.id.in_(busy))

    to_mark = [rid for rid, since in candidates if rid not in busy and since is None]
    for start in range(0, len(to_mark), 500):
        stats['marked'] += (Recording.query
                            .filter(Recording.id.in_(to_mark[start:start + 500]),
                                    Recording.user_id.is_(None),
                                    Recording.ownerless_since.is_(None))
                            .update({Recording.ownerless_since: now}, synchronize_session=False))
    db.session.commit()

    marked_at = dict(candidates)
    due = []
    for rid, since in candidates:
        if rid in busy or since is None:
            continue
        if since <= cutoff:
            due.append(rid)
        else:
            stats['waiting'] += 1

    if due and storage is None:
        from src.services.storage import get_storage_service
        storage = get_storage_service()

    for rid in due:
        try:
            # Read the row again and check every condition once more right
            # before deleting: an owner or a job may have appeared since the
            # candidates were listed.
            db.session.expire_all()
            recording = db.session.get(Recording, rid)
            if (recording is None or recording.user_id is not None
                    or recording.ownerless_since is None or recording.ownerless_since > cutoff):
                continue
            if _in_flight_ids([rid]):
                stats['skipped_in_flight'] += 1
                continue
            # strict_media: a file that cannot be deleted keeps the row, so
            # the next run retries instead of leaving an untracked file.
            delete_recording_completely(recording, storage=storage, strict_media=True)
            stats['deleted'] += 1
            current_app.logger.info(
                f"Ownerless cleanup: deleted recording {rid}, without an owner since {_iso(marked_at[rid])}")
        except Exception as e:
            stats['errors'] += 1
            db.session.rollback()
            current_app.logger.error(f"Ownerless cleanup: could not delete recording {rid}: {e}")

    if stats['deleted']:
        cleanup_orphaned_speakers_quietly()

    try:
        stats['unreferenced_files'] = count_unreferenced_media_files(storage)
    except Exception as e:
        current_app.logger.warning(f"Ownerless cleanup: could not count unreferenced media files: {e}")

    return stats


def _iso(value):
    return value.isoformat(timespec='seconds') + 'Z' if value else 'unknown'


def count_unreferenced_media_files(storage=None):
    """Count local media files that no row refers to. Only for the log.

    The scan is the one scripts/clean_unreferenced_files.py uses (see
    src/services/unreferenced_files.py), without its minimum age. Returns None
    when the storage has no local folder to look at.
    """
    from src.services import unreferenced_files

    if storage is None:
        from src.services.storage import get_storage_service
        storage = get_storage_service()
    if unreferenced_files.local_root(storage) is None:
        return None
    result = unreferenced_files.scan(storage, min_age=None, limit=UNREFERENCED_SCAN_LIMIT,
                                     include_s3=False)
    if result.truncated:
        current_app.logger.info(
            f"Ownerless cleanup: stopped counting unreferenced files after {UNREFERENCED_SCAN_LIMIT} entries")
    return len(result.candidates) + len(result.kept_name_matches)


def run_ownerless_cleanup():
    """The daily step: sweep, log the counts, never raise."""
    try:
        stats = sweep_ownerless_recordings()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Ownerless recording cleanup failed: {e}", exc_info=True)
        return {'error': str(e)}
    files = stats['unreferenced_files']
    current_app.logger.info(
        "Ownerless recording cleanup: "
        f"{stats['candidates']} without an owner, {stats['marked']} marked, "
        f"{stats['waiting']} waiting, {stats['deleted']} deleted, "
        f"{stats['skipped_in_flight']} skipped (work in flight), {stats['unmarked']} marks cleared, "
        f"{stats['errors']} errors; "
        + (f"{files} media file(s) on disk without a recording (not deleted)" if files is not None
           else "media files on disk not counted"))
    return stats
