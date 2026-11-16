"""Deleting a recording completely, in one place.

The web route, API v1 and full-recording retention each had their own copy
of this, and they had drifted. API v1 built a filesystem path from the
storage locator by hand, which never matches a locator such as
local://recordings/... or an S3 object, so the media file was left behind.
Retention sent no recording.deleted webhook and left the export in place.
"""

from flask import current_app

from src.database import db
from src.utils.log_filters import loggable_error, loggable_locator


def delete_recording_completely(recording, storage=None, strict_media=False, reason='deleted',
                                audit_reason=None):
    """Delete the media, the rows that reference the recording, and the recording.

    Emits the recording.deleted webhook and marks the export as deleted.
    Orphaned speaker profiles are left to the caller, which may be deleting
    many recordings. Commits; raises on a database error so the caller can
    roll back.

    strict_media: when the media cannot be deleted, raise and keep the
    recording (retention wants that, so its next run retries). A user's
    delete leaves a stray file instead of an undeletable recording.
    reason: the tombstone reason for the changes feed, 'deleted' or
    'retention'.

    audit_reason: the reason code of the recording.deleted audit row (user,
    retention, account_deleted, ownerless, upload_canceled, merged). Default:
    retention for a retention deletion, else user.
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
            current_app.logger.info(f"Deleted media for recording {recording_id}: {loggable_locator(recording.audio_path)}")
        except Exception as e:
            if strict_media:
                raise
            current_app.logger.error(f"Error deleting media of recording {recording_id} "
                                     f"{loggable_locator(recording.audio_path)}: {loggable_error(e, recording.audio_path)}")

    from src.services.waveform_peaks import remove_peaks
    remove_peaks(current_app.config.get('UPLOAD_FOLDER') or '', recording_id)

    # Voice samples outlive the recording, as the averaged profile always
    # did; they only lose the reference (SQLite does not apply SET NULL).
    from src.models import SpeakerVoiceSample
    SpeakerVoiceSample.query.filter_by(recording_id=recording_id).update({'recording_id': None})

    forget_recording_sessions(recording_id)

    # A transcription still running on the WhisperX ASR service is cancelled
    # there, and a finished one has its stored transcript removed.
    try:
        from src.services.transcription.provider_tasks import cancel_remote_tasks
        cancel_remote_tasks(ProcessingJob.query.filter(
            ProcessingJob.recording_id == recording_id,
            ProcessingJob.provider_task.isnot(None)).all())
    except Exception as e:
        current_app.logger.warning(f"Cancelling ASR jobs of recording {recording_id} failed: {e}")

    snippets = SpeakerSnippet.query.filter_by(recording_id=recording_id).delete()
    jobs = ProcessingJob.query.filter_by(recording_id=recording_id).delete()
    if snippets or jobs:
        current_app.logger.info(f"Deleted {snippets} speaker snippets and {jobs} processing jobs for recording {recording_id}")

    # The one audit point of every deletion of a recording: web, bulk, API,
    # retention, account deletion. In the transaction of the deletion.
    from src.services import audit
    audit.record_in_tx('recording.deleted', recording_id=recording_id, recording_owner_id=recording.user_id,
                       details={'reason': audit_reason or ('retention' if reason == 'retention' else 'user')})
    db.session.info['tombstone_reason'] = reason
    try:
        db.session.delete(recording)  # cascades to chunks, shares, tags
        db.session.commit()
    finally:
        db.session.info.pop('tombstone_reason', None)
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


def forget_recording_sessions(recording_id):
    """Remove what the recording sessions of a deleted recording hold.

    The session of a recording made in the browser or uploaded in slices
    holds text of the user: the title and notes typed at the end of the
    recording, or the file name of the upload. A session that has ended is
    deleted with its recording. One still at work (its chunks are on disk and
    a stitch job may be reading them) keeps its row without that text and
    without the link; the hourly cleanup expires it and removes its folder.
    SQLite reuses the highest id, so a link left behind could point at a
    later recording of another user (#505). The caller commits.
    """
    from src.models import RecordingSession
    from src.models.recording_session import RECORDING_SESSION_ENDED
    sessions = RecordingSession.query.filter_by(finalized_recording_id=recording_id)
    sessions.filter(RecordingSession.status.in_(RECORDING_SESSION_ENDED)).delete(synchronize_session=False)
    sessions.update({'finalized_recording_id': None, 'finalize_metadata': None, 'upload_filename': None},
                    synchronize_session=False)


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
