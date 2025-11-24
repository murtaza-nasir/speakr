"""
Recording retention and auto-deletion services.
"""

import os
from datetime import datetime, timedelta
from flask import current_app

from src.database import db
from src.models import Recording, RecordingTag, Tag

ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
GLOBAL_RETENTION_DAYS = int(os.environ.get('GLOBAL_RETENTION_DAYS', '0'))
DELETION_MODE = os.environ.get('DELETION_MODE', 'full_recording')



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
    for tag_assoc in recording.tag_associations:
        if tag_assoc.tag.protect_from_deletion:
            return True
        if tag_assoc.tag.retention_days == -1:
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
    for tag_assoc in recording.tag_associations:
        if tag_assoc.tag.retention_days and tag_assoc.tag.retention_days > 0:
            tag_retention_periods.append(tag_assoc.tag.retention_days)

    # If any tags have retention periods, use the shortest one (most conservative)
    if tag_retention_periods:
        return min(tag_retention_periods)

    # Fall back to global retention
    if GLOBAL_RETENTION_DAYS > 0:
        return GLOBAL_RETENTION_DAYS

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
    if not ENABLE_AUTO_DELETION:
        return {'error': 'Auto-deletion is not enabled'}

    # Check if any retention policy exists (global or tag-level)
    has_global_retention = GLOBAL_RETENTION_DAYS > 0
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
        # Get completed recordings to check
        # In audio_only mode: Skip recordings where audio was already deleted
        # In full_recording mode: Include all (to catch audio-only deletions for full cleanup)
        if DELETION_MODE == 'audio_only':
            all_recordings = Recording.query.filter(
                Recording.status == 'COMPLETED',
                Recording.audio_deleted_at.is_(None)  # Skip already-deleted audio
            ).all()
        else:  # full_recording mode
            all_recordings = Recording.query.filter(
                Recording.status == 'COMPLETED'
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
                if DELETION_MODE == 'audio_only':
                    # Delete only the audio file, keep transcription
                    if recording.audio_path and os.path.exists(recording.audio_path):
                        current_app.logger.info(f"Recording {recording.id} is past retention ({retention_days} days), deleting audio")
                        os.remove(recording.audio_path)
                        current_app.logger.info(f"Auto-deleted audio file: {recording.audio_path}")
                        recording.audio_deleted_at = datetime.utcnow()
                        db.session.commit()
                        stats['deleted_audio_only'] += 1
                    else:
                        # Audio already deleted or doesn't exist - just mark timestamp
                        if not recording.audio_deleted_at:
                            recording.audio_deleted_at = datetime.utcnow()
                            db.session.commit()
                            current_app.logger.debug(f"Recording {recording.id} audio file not found, marked as deleted")

                else:  # full_recording mode
                    # Check if this is completing a previous audio_only deletion
                    if recording.audio_deleted_at:
                        current_app.logger.info(f"Recording {recording.id} has deleted audio (mode changed), completing full deletion")
                    else:
                        current_app.logger.info(f"Recording {recording.id} is past retention ({retention_days} days), deleting fully")

                    # Delete audio file if it exists
                    if recording.audio_path and os.path.exists(recording.audio_path):
                        os.remove(recording.audio_path)

                    # Delete the database record (cascades to chunks, shares, etc.)
                    db.session.delete(recording)
                    db.session.commit()
                    stats['deleted_full'] += 1
                    current_app.logger.info(f"Auto-deleted full recording ID: {recording.id}")

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


