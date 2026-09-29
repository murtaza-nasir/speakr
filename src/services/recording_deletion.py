"""Deleting a recording completely, in one place.

The web route, API v1 and full-recording retention each had their own copy
of this, and they had drifted. API v1 built a filesystem path from the
storage locator by hand, which never matches a locator such as
local://recordings/... or an S3 object, so the media file was left behind.
Retention sent no recording.deleted webhook and left the export in place.
"""

from flask import current_app

from src.database import db


def delete_recording_completely(recording, storage=None, strict_media=False):
    """Delete the media, the rows that reference the recording, and the recording.

    Emits the recording.deleted webhook and marks the export as deleted.
    Orphaned speaker profiles are left to the caller, which may be deleting
    many recordings. Commits; raises on a database error so the caller can
    roll back.

    strict_media: when the media cannot be deleted, raise and keep the
    recording (retention wants that, so its next run retries). A user's
    delete leaves a stray file instead of an undeletable recording.
    """
    from src.models.processing_job import ProcessingJob
    from src.models.speaker_snippet import SpeakerSnippet

    if storage is None:
        from src.services.storage import get_storage_service
        storage = get_storage_service()

    recording_id = recording.id
    title = recording.title
    user_id = recording.user_id

    if recording.audio_path:
        try:
            storage.delete(recording.audio_path, missing_ok=True)
            current_app.logger.info(f"Deleted media for recording {recording_id}: {recording.audio_path}")
        except Exception as e:
            if strict_media:
                raise
            current_app.logger.error(f"Error deleting media {recording.audio_path}: {e}")

    # Voice samples outlive the recording, as the averaged profile always
    # did; they only lose the reference (SQLite does not apply SET NULL).
    from src.models import SpeakerVoiceSample
    SpeakerVoiceSample.query.filter_by(recording_id=recording_id).update({'recording_id': None})

    snippets = SpeakerSnippet.query.filter_by(recording_id=recording_id).delete()
    jobs = ProcessingJob.query.filter_by(recording_id=recording_id).delete()
    if snippets or jobs:
        current_app.logger.info(f"Deleted {snippets} speaker snippets and {jobs} processing jobs for recording {recording_id}")

    db.session.delete(recording)  # cascades to chunks, shares, tags
    db.session.commit()
    current_app.logger.info(f"Deleted recording {recording_id}")

    try:
        from src.services.webhook_dispatch import emit_webhook_event
        emit_webhook_event(user_id=user_id, event_type='recording.deleted',
                           data={'recording_id': recording_id, 'title': title})
    except Exception as e:
        current_app.logger.warning(f"Webhook emit (recording.deleted) failed: {e}")

    try:
        from src.file_exporter import mark_export_as_deleted
        mark_export_as_deleted(recording_id)
    except Exception as e:
        current_app.logger.warning(f"Marking export as deleted failed for recording {recording_id}: {e}")


def cleanup_orphaned_speakers_quietly():
    """Best-effort removal of speaker profiles no recording uses any more."""
    try:
        from src.services.speaker_cleanup import cleanup_orphaned_speakers
        stats = cleanup_orphaned_speakers()
        if stats.get('speakers_deleted', 0) > 0:
            current_app.logger.info(f"Cleaned up {stats['speakers_deleted']} orphaned speakers")
        return stats
    except Exception as e:
        current_app.logger.warning(f"Speaker cleanup after recording deletion failed: {e}")
        return {}
