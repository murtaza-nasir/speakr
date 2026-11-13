"""
Recording retention and auto-deletion services.
"""

import os
from datetime import datetime, timedelta
from flask import current_app

from src.database import db
from src.models import Recording, RecordingTag, Tag
from src.services.storage import get_storage_service
from src.utils.localization import api_error




def tag_binds_retention(recording, tag):
    """Whether a tag on the recording takes part in its retention.

    A group tag always does. A personal tag is private to the user who made
    it, and anyone who can view a recording may attach their own (#589), so
    only the owner's personal tags count: a tag the owner cannot see must
    not shorten the recording's life or keep it forever.
    """
    if tag is None:
        return False
    if tag.group_id is not None:
        return True
    return recording.user_id is not None and tag.user_id == recording.user_id


def retention_tags(recording):
    """The recording's tags that take part in its retention."""
    return [assoc.tag for assoc in recording.tag_associations if tag_binds_retention(recording, assoc.tag)]


def is_recording_exempt_from_deletion(recording):
    """
    Check if a recording is exempt from auto-deletion.

    Args:
        recording: Recording object to check

    Returns:
        Boolean indicating if the recording should be kept
    """
    # Manual exemption flag
    if recording.deletion_exempt:
        return True

    # Check if any of the recording's tags protect it from deletion
    # Protection can be indicated by either protect_from_deletion flag OR retention_days == -1
    for tag in retention_tags(recording):
        if tag.protect_from_deletion:
            return True
        if tag.retention_days == -1:
            return True

    return False



def get_retention_days_for_recording(recording):
    """
    Get the effective retention period for a recording.
    Multi-tier system: tag retention (shortest) → global retention

    Tags with retention_days set override the global retention policy.
    If multiple tags have retention_days, the SHORTEST period is used (most conservative).
    Note: retention_days == -1 indicates infinite retention (protected), which is handled separately.

    Args:
        recording: Recording object

    Returns:
        Integer days for retention period, or None if no retention applies
    """
    # Collect all tag-level retention periods
    # Skip -1 (infinite retention/protected) as that's handled in is_recording_exempt_from_deletion
    tag_retention_periods = []
    for tag in retention_tags(recording):
        if tag.retention_days and tag.retention_days > 0:
            tag_retention_periods.append(tag.retention_days)

    # If any tags have retention periods, use the shortest one (most conservative)
    if tag_retention_periods:
        return min(tag_retention_periods)

    # Fall back to global retention
    if _self.GLOBAL_RETENTION_DAYS > 0:
        return _self.GLOBAL_RETENTION_DAYS

    return None



def process_auto_deletion():
    """
    Process auto-deletion of recordings based on retention policies.
    This can be called by a scheduled job or admin endpoint.

    Supports per-recording retention via tag-level retention_days overrides.
    Tags with retention_days set take precedence over global retention.

    Returns:
        Dictionary with deletion statistics
    """
    if not _self.ENABLE_AUTO_DELETION:
        return api_error('auto_deletion_disabled')

    # Check if any retention policy exists (global or tag-level)
    has_global_retention = _self.GLOBAL_RETENTION_DAYS > 0
    # We'll check for tag-level retention on a per-recording basis

    if not has_global_retention:
        # Still check recordings in case they have tag-level retention
        current_app.logger.info("No global retention configured, checking for tag-level retention policies")

    stats = {
        'checked': 0,
        'deleted_audio_only': 0,
        'deleted_full': 0,
        'exempted': 0,
        'skipped_no_retention': 0,
        'errors': 0
    }

    try:
        # Get settled recordings to check (COMPLETED and FAILED — never QUEUED
        # or PROCESSING, so an in-flight backlog can't be swept mid-work)
        # In audio_only mode: Skip recordings where audio was already deleted
        # In full_recording mode: Include all (to catch audio-only deletions for full cleanup)
        settled_statuses = ['COMPLETED', 'FAILED']
        if _self.DELETION_MODE == 'audio_only':
            all_recordings = Recording.query.filter(
                Recording.status.in_(settled_statuses),
                Recording.audio_deleted_at.is_(None)  # Skip already-deleted audio
            ).all()
        else:  # full_recording mode
            all_recordings = Recording.query.filter(
                Recording.status.in_(settled_statuses)
            ).all()

        stats['checked'] = len(all_recordings)
        current_time = datetime.utcnow()

        for recording in all_recordings:
            try:
                # Check if exempt from deletion entirely
                if is_recording_exempt_from_deletion(recording):
                    stats['exempted'] += 1
                    continue

                # Get the effective retention period for this specific recording
                retention_days = get_retention_days_for_recording(recording)

                if not retention_days:
                    # No retention policy applies to this recording
                    stats['skipped_no_retention'] += 1
                    continue

                # Calculate the cutoff date for this specific recording
                cutoff_date = current_time - timedelta(days=retention_days)

                # Check if recording is past its retention period
                if recording.created_at >= cutoff_date:
                    # Recording is still within retention period
                    continue

                # Recording is past retention period - process deletion

                # Determine deletion mode
                if _self.DELETION_MODE == 'audio_only':
                    # Delete only the audio file, keep transcription
                    current_app.logger.info(f"Recording {recording.id} is past retention ({retention_days} days), removing audio")
                    if remove_recording_audio(recording):
                        stats['deleted_audio_only'] += 1

                else:  # full_recording mode
                    # Check if this is completing a previous audio_only deletion
                    if recording.audio_deleted_at:
                        current_app.logger.info(f"Recording {recording.id} has deleted audio (mode changed), completing full deletion")
                    else:
                        current_app.logger.info(f"Recording {recording.id} is past retention ({retention_days} days), deleting fully")

                    from src.services.recording_deletion import delete_recording_completely
                    deleted_id = recording.id
                    delete_recording_completely(recording, storage=get_storage_service(), strict_media=True,
                                                reason='retention')
                    stats['deleted_full'] += 1
                    current_app.logger.info(f"Auto-deleted full recording ID: {deleted_id}")

            except Exception as e:
                stats['errors'] += 1
                current_app.logger.error(f"Error auto-deleting recording {recording.id}: {e}")
                db.session.rollback()

        # After processing recording deletions, clean up orphaned speaker profiles
        try:
            from src.services.speaker_cleanup import cleanup_orphaned_speakers
            speaker_stats = cleanup_orphaned_speakers()
            stats['speakers_deleted'] = speaker_stats['speakers_deleted']
            stats['embeddings_cleaned'] = speaker_stats['embeddings_removed']
            stats['speakers_evaluated'] = speaker_stats['speakers_evaluated']
            current_app.logger.info(
                f"Speaker cleanup completed: {speaker_stats['speakers_deleted']} speakers deleted, "
                f"{speaker_stats['embeddings_removed']} embedding references removed"
            )
        except Exception as e:
            current_app.logger.error(f"Error during speaker cleanup: {e}", exc_info=True)
            stats['speaker_cleanup_error'] = str(e)

        current_app.logger.info(f"Auto-deletion completed: {stats}")
        return stats

    except Exception as e:
        current_app.logger.error(f"Error during auto-deletion process: {e}", exc_info=True)
        return {'error': str(e)}

# --- API client setup for OpenRouter ---
# Use environment variables from .env


def remove_recording_audio(recording):
    """Delete a recording's media file and keep everything else.

    The transcript, summary, notes, tags and shares stay; the recording shows
    as "Audio removed" and can no longer be played or reprocessed. Used by
    audio-only retention and by the manual "Delete audio" action.

    Returns True when a stored file was deleted, False when there was none
    (the recording is marked either way). Commits.
    """
    storage = get_storage_service()
    deleted = False
    if recording.audio_path and storage.exists(recording.audio_path):
        storage.delete(recording.audio_path, missing_ok=True)
        current_app.logger.info(f"Removed audio file for recording {recording.id}: {recording.audio_path}")
        deleted = True
    if not recording.audio_deleted_at:
        recording.audio_deleted_at = datetime.utcnow()
    db.session.commit()
    return deleted


# Hot-applied System Settings (high priority) with environment fallback.
from src.utils.system_settings import apply_hot_settings
_SYSTEM_SETTING_MAP = {
    "ENABLE_AUTO_DELETION": ('enable_auto_deletion', 'ENABLE_AUTO_DELETION', False),
    "GLOBAL_RETENTION_DAYS": ('global_retention_days', 'GLOBAL_RETENTION_DAYS', 0),
    "DELETION_MODE": ('deletion_mode', 'DELETION_MODE', 'full_recording'),
}
_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)
