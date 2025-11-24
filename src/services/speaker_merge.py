"""
Speaker Merge Service.

This service handles merging multiple speaker profiles into one.
Useful when users accidentally create duplicate speakers for the same person.

When speakers are merged:
- Voice embeddings are combined using weighted average
- All snippets are transferred to the target speaker
- Usage statistics are combined
- Source speakers are deleted
- Confidence score is recalculated
"""

import numpy as np
from src.database import db
from src.models import Speaker, SpeakerSnippet
from src.services.speaker_embedding_matcher import (
    serialize_embedding,
    deserialize_embedding,
    calculate_confidence
)


def merge_speakers(target_id, source_ids, user_id):
    """
    Merge multiple speaker profiles into one target speaker.

    All embeddings, snippets, and usage data from source speakers are
    combined into the target speaker. Source speakers are then deleted.

    Args:
        target_id: ID of the speaker to keep (receives all merged data)
        source_ids: List of speaker IDs to merge into target
        user_id: ID of the user (for security check)

    Returns:
        Speaker: The updated target speaker

    Raises:
        ValueError: If speakers don't exist or don't belong to user
    """
    # Validate target speaker
    target = Speaker.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        raise ValueError(f"Target speaker {target_id} not found or doesn't belong to user")

    # Validate source speakers
    sources = Speaker.query.filter(
        Speaker.id.in_(source_ids),
        Speaker.user_id == user_id
    ).all()

    if len(sources) == 0:
        raise ValueError("No valid source speakers found")

    if len(sources) != len(source_ids):
        raise ValueError("Some source speakers don't exist or don't belong to user")

    # Can't merge a speaker with itself
    if target_id in source_ids:
        raise ValueError("Cannot merge a speaker with itself")

    # Combine embeddings
    _combine_embeddings(target, sources)

    # Transfer snippets
    for source in sources:
        SpeakerSnippet.query.filter_by(speaker_id=source.id).update(
            {'speaker_id': target_id}
        )

    # Combine usage statistics
    for source in sources:
        target.use_count += source.use_count

        # Update last_used to most recent
        if source.last_used and (not target.last_used or source.last_used > target.last_used):
            target.last_used = source.last_used

        # Combine embedding histories
        if source.embeddings_history:
            target_history = target.embeddings_history or []
            source_history = source.embeddings_history or []
            combined_history = target_history + source_history

            # Sort by timestamp (most recent last) and keep last 10
            try:
                combined_history.sort(key=lambda x: x.get('timestamp', ''))
                target.embeddings_history = combined_history[-10:]
            except:
                # If sorting fails, just concatenate and truncate
                target.embeddings_history = (target_history + source_history)[-10:]

    # Recalculate confidence score
    target.confidence_score = calculate_confidence(target)

    # Delete source speakers
    for source in sources:
        db.session.delete(source)

    # Commit all changes
    db.session.commit()

    return target


def _combine_embeddings(target, sources):
    """
    Combine embeddings from multiple speakers using weighted average.

    Weight is based on embedding_count (more samples = more weight).

    Args:
        target: Target Speaker instance
        sources: List of source Speaker instances
    """
    all_embeddings = []
    all_counts = []

    # Add target's embedding if it exists
    if target.average_embedding:
        all_embeddings.append(deserialize_embedding(target.average_embedding))
        all_counts.append(target.embedding_count or 1)

    # Add all source embeddings
    for source in sources:
        if source.average_embedding:
            all_embeddings.append(deserialize_embedding(source.average_embedding))
            all_counts.append(source.embedding_count or 1)

    if not all_embeddings:
        # No embeddings to combine
        return

    # Calculate weighted average
    total_count = sum(all_counts)
    weights = [c / total_count for c in all_counts]

    combined_emb = np.average(all_embeddings, axis=0, weights=weights)

    # Update target
    target.average_embedding = serialize_embedding(combined_emb)
    target.embedding_count = total_count


def preview_merge(target_id, source_ids, user_id):
    """
    Preview what a merge would look like without executing it.

    Args:
        target_id: ID of the target speaker
        source_ids: List of source speaker IDs
        user_id: ID of the user

    Returns:
        dict: Preview of the merge results
              {
                  'target_name': '...',
                  'source_names': [...],
                  'combined_use_count': 123,
                  'combined_embedding_count': 45,
                  'total_snippets': 67
              }
    """
    # Validate speakers
    target = Speaker.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        raise ValueError("Target speaker not found")

    sources = Speaker.query.filter(
        Speaker.id.in_(source_ids),
        Speaker.user_id == user_id
    ).all()

    if len(sources) == 0:
        raise ValueError("No valid source speakers found")

    # Calculate combined statistics
    combined_use_count = target.use_count
    combined_embedding_count = target.embedding_count or 0
    total_snippets = SpeakerSnippet.query.filter_by(speaker_id=target_id).count()

    source_names = []
    for source in sources:
        combined_use_count += source.use_count
        combined_embedding_count += (source.embedding_count or 0)
        total_snippets += SpeakerSnippet.query.filter_by(speaker_id=source.id).count()
        source_names.append(source.name)

    return {
        'target_name': target.name,
        'source_names': source_names,
        'combined_use_count': combined_use_count,
        'combined_embedding_count': combined_embedding_count,
        'total_snippets': total_snippets,
        'has_embeddings': target.average_embedding is not None or any(s.average_embedding for s in sources)
    }


def can_merge_speakers(speaker_ids, user_id):
    """
    Check if speakers can be merged (all belong to same user, no duplicates).

    Args:
        speaker_ids: List of speaker IDs
        user_id: ID of the user

    Returns:
        tuple: (bool, str) - (can_merge, error_message)
    """
    if len(speaker_ids) < 2:
        return False, "Need at least 2 speakers to merge"

    if len(speaker_ids) != len(set(speaker_ids)):
        return False, "Duplicate speaker IDs provided"

    speakers = Speaker.query.filter(
        Speaker.id.in_(speaker_ids),
        Speaker.user_id == user_id
    ).all()

    if len(speakers) != len(speaker_ids):
        return False, "Some speakers don't exist or don't belong to user"

    return True, ""
