"""
Speaker cleanup service for managing orphaned speaker voice profiles.

This module provides automatic cleanup of speaker records when their associated
recordings are deleted through auto-deletion or manual deletion processes.

By default, speaker profiles (including voice embeddings) are preserved even
when all their recordings are deleted, since embeddings are aggregated and
represent hours of manual identification work. Set DELETE_ORPHANED_SPEAKERS=true
to enable automatic cleanup of speakers with no remaining recordings.
"""

import os
import logging
import json
from datetime import datetime
from sqlalchemy import exists
from src.database import db
from src.models import Speaker, SpeakerSnippet, Recording

logger = logging.getLogger(__name__)


def cleanup_orphaned_speakers(dry_run=False):
    """
    Clean up speaker records that no longer have any associated recordings.

    Only runs if DELETE_ORPHANED_SPEAKERS=true is set. By default, speaker
    profiles are preserved because voice embeddings are aggregated values
    that can't be reconstructed from recordings alone.

    A speaker is considered orphaned when:
    - It has no SpeakerSnippet records
    - Its embeddings_history contains no valid recording references

    Args:
        dry_run (bool): If True, only report what would be deleted without actually deleting

    Returns:
        dict: Statistics about cleanup operation
            {
                'speakers_deleted': int,
                'embeddings_removed': int,
                'speakers_evaluated': int,
                'orphaned_speakers': list of dict (if dry_run=True)
            }
    """
    delete_orphans = os.environ.get('DELETE_ORPHANED_SPEAKERS', 'false').lower() in ('true', '1', 'yes')

    if not delete_orphans:
        logger.debug("Speaker cleanup skipped (DELETE_ORPHANED_SPEAKERS is not enabled)")
        return {
            'speakers_deleted': 0,
            'embeddings_removed': 0,
            'speakers_evaluated': 0,
            'orphaned_speakers': []
        }

    logger.info("Starting speaker cleanup process (dry_run=%s)", dry_run)

    stats = {
        'speakers_deleted': 0,
        'embeddings_removed': 0,
        'speakers_evaluated': 0,
        'orphaned_speakers': []
    }

    try:
        # Clean embeddings_history references first
        embeddings_cleaned = clean_embeddings_history_references(dry_run=dry_run)
        stats['embeddings_removed'] = embeddings_cleaned

        # Find and process orphaned speakers
        orphaned_speaker_ids = get_orphaned_speakers()
        stats['speakers_evaluated'] = Speaker.query.count()

        if not orphaned_speaker_ids:
            logger.info("No orphaned speakers found")
            return stats

        logger.info("Found %d orphaned speaker(s)", len(orphaned_speaker_ids))

        if dry_run:
            # Report what would be deleted
            for speaker_id in orphaned_speaker_ids:
                speaker = Speaker.query.get(speaker_id)
                if speaker:
                    stats['orphaned_speakers'].append({
                        'id': speaker.id,
                        'name': speaker.name,
                        'user_id': speaker.user_id,
                        'embedding_count': speaker.embedding_count
                    })
            logger.info("Dry run: Would delete %d speakers", len(orphaned_speaker_ids))
        else:
            # Actually delete orphaned speakers
            for speaker_id in orphaned_speaker_ids:
                speaker = Speaker.query.get(speaker_id)
                if speaker:
                    logger.debug(
                        "Deleting orphaned speaker: id=%d, name='%s', user_id=%d, embedding_count=%d",
                        speaker.id, speaker.name, speaker.user_id, speaker.embedding_count or 0
                    )
                    db.session.delete(speaker)
                    stats['speakers_deleted'] += 1

            # Commit all deletions
            db.session.commit()
            logger.info("Speaker cleanup completed: %d speakers deleted", stats['speakers_deleted'])

            # Warning if large number deleted
            if stats['speakers_deleted'] >= 50:
                logger.warning(
                    "Large number of speakers deleted (%d). Review cleanup logic if unexpected.",
                    stats['speakers_deleted']
                )

        return stats

    except Exception as e:
        db.session.rollback()
        logger.error("Error during speaker cleanup: %s", str(e), exc_info=True)
        raise


def clean_embeddings_history_references(dry_run=False):
    """
    Clean embeddings_history JSON fields to remove references to deleted recordings.

    Scans all speakers' embeddings_history and removes entries where the
    recording_id no longer exists in the database.

    Args:
        dry_run (bool): If True, only count what would be cleaned

    Returns:
        int: Number of embedding references removed
    """
    logger.debug("Cleaning embeddings_history references (dry_run=%s)", dry_run)

    references_removed = 0

    try:
        # Get all speakers with embeddings_history
        speakers = Speaker.query.filter(Speaker.embeddings_history.isnot(None)).all()

        for speaker in speakers:
            try:
                # Parse embeddings_history JSON
                if not speaker.embeddings_history:
                    continue

                history = speaker.embeddings_history if isinstance(speaker.embeddings_history, list) else json.loads(speaker.embeddings_history)

                if not history or not isinstance(history, list):
                    continue

                # Filter out entries with deleted recording_ids
                cleaned_history = []
                for entry in history:
                    if not isinstance(entry, dict) or 'recording_id' not in entry:
                        continue

                    recording_id = entry['recording_id']

                    # Check if recording still exists
                    recording_exists = db.session.query(
                        exists().where(Recording.id == recording_id)
                    ).scalar()

                    if recording_exists:
                        cleaned_history.append(entry)
                    else:
                        references_removed += 1
                        logger.debug(
                            "Removing deleted recording reference: speaker_id=%d, recording_id=%d",
                            speaker.id, recording_id
                        )

                # Update speaker if history changed
                if len(cleaned_history) < len(history):
                    if not dry_run:
                        speaker.embeddings_history = cleaned_history
                        logger.debug(
                            "Updated speaker %d embeddings_history: %d -> %d entries",
                            speaker.id, len(history), len(cleaned_history)
                        )

            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(
                    "Error processing embeddings_history for speaker %d: %s",
                    speaker.id, str(e)
                )
                continue

        if not dry_run and references_removed > 0:
            db.session.commit()
            logger.debug("Cleaned %d embedding references", references_removed)

        return references_removed

    except Exception as e:
        db.session.rollback()
        logger.error("Error cleaning embeddings_history: %s", str(e), exc_info=True)
        raise


def get_orphaned_speakers(user_id=None):
    """
    Get list of speaker IDs that are orphaned (no associated recordings).

    A speaker is orphaned when:
    - It has no SpeakerSnippet records
    - After cleaning embeddings_history, it has no valid recording references

    Args:
        user_id (int, optional): Filter to specific user's speakers

    Returns:
        list: List of speaker IDs that are orphaned
    """
    logger.debug("Finding orphaned speakers (user_id=%s)", user_id)

    # Query for speakers with no snippets
    query = Speaker.query.filter(
        ~exists().where(SpeakerSnippet.speaker_id == Speaker.id)
    )

    if user_id is not None:
        query = query.filter(Speaker.user_id == user_id)

    speakers_without_snippets = query.all()

    orphaned_ids = []

    for speaker in speakers_without_snippets:
        # Check if embeddings_history has any valid recording references
        has_valid_recordings = False

        if speaker.embeddings_history:
            try:
                history = speaker.embeddings_history if isinstance(speaker.embeddings_history, list) else json.loads(speaker.embeddings_history)

                if history and isinstance(history, list):
                    for entry in history:
                        if isinstance(entry, dict) and 'recording_id' in entry:
                            recording_id = entry['recording_id']

                            # Check if this recording exists
                            recording_exists = db.session.query(
                                exists().where(Recording.id == recording_id)
                            ).scalar()

                            if recording_exists:
                                has_valid_recordings = True
                                break
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

        # If no snippets AND no valid recording references, it's orphaned
        if not has_valid_recordings:
            orphaned_ids.append(speaker.id)
            logger.debug(
                "Speaker %d ('%s') is orphaned: no snippets, no valid recordings",
                speaker.id, speaker.name
            )

    return orphaned_ids


def get_speaker_cleanup_statistics():
    """
    Get statistics about speaker data for monitoring.

    Returns:
        dict: Statistics about speakers
            {
                'total_speakers': int,
                'speakers_with_snippets': int,
                'speakers_with_embeddings': int,
                'potential_orphans': int
            }
    """
    stats = {
        'total_speakers': Speaker.query.count(),
        'speakers_with_snippets': db.session.query(Speaker.id).join(
            SpeakerSnippet, Speaker.id == SpeakerSnippet.speaker_id
        ).distinct().count(),
        'speakers_with_embeddings': Speaker.query.filter(
            Speaker.average_embedding.isnot(None)
        ).count(),
        'potential_orphans': len(get_orphaned_speakers())
    }

    return stats
