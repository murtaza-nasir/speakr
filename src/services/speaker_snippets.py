"""
Speaker Snippets Service.

This service handles the extraction and management of representative speech snippets
from recordings. Snippets provide context when viewing speaker profiles and help
users verify speaker identifications.

Key functions:
- Extract snippets when speakers are identified in recordings
- Retrieve snippets for display in speaker profiles
- Clean up old snippets to prevent database bloat
"""

import json
from src.database import db
from src.models import Speaker, SpeakerSnippet, Recording

MAX_SNIPPETS_PER_SPEAKER = 7
MAX_SNIPPETS_PER_RECORDING = 2


def create_speaker_snippets(recording_id, speaker_map):
    """
    Extract and store representative snippets for each identified speaker.

    This function is called after a user saves speaker identifications in a recording.
    It extracts up to MAX_SNIPPETS_PER_RECORDING quotes per speaker from this recording,
    and enforces a global cap of MAX_SNIPPETS_PER_SPEAKER by evicting the oldest.

    Args:
        recording_id: ID of the recording
        speaker_map: Dict mapping SPEAKER_XX to speaker info
                    {'SPEAKER_00': {'name': 'John Doe', 'isMe': False}, ...}

    Returns:
        int: Number of snippets created
    """
    recording = Recording.query.get(recording_id)
    if not recording or not recording.transcription:
        return 0

    try:
        transcript = json.loads(recording.transcription)
    except (json.JSONDecodeError, TypeError):
        return 0

    # Build a reverse map: assigned name -> speaker_info
    # After transcript is saved, segment['speaker'] contains the real name,
    # not the original SPEAKER_XX label. We need to match by name too.
    name_to_info = {}
    for label, info in speaker_map.items():
        name = info.get('name', '').strip()
        if name and not name.startswith('SPEAKER_'):
            name_to_info[name] = info

    # Collect candidates per speaker: (speaker_obj, segment_idx, text, timestamp)
    candidates = {}  # speaker_id -> list of (segment_idx, text, timestamp)

    for segment_idx, segment in enumerate(transcript):
        speaker_field = segment.get('speaker')

        if not speaker_field:
            continue

        # Try matching by original label first, then by assigned name
        if speaker_field in speaker_map:
            speaker_info = speaker_map[speaker_field]
            speaker_name = speaker_info.get('name')
        elif speaker_field in name_to_info:
            speaker_name = speaker_field
        else:
            continue

        if not speaker_name or speaker_name.startswith('SPEAKER_'):
            continue

        # Find the speaker in database
        speaker = Speaker.query.filter_by(
            user_id=recording.user_id,
            name=speaker_name
        ).first()

        if not speaker:
            continue

        text = segment.get('sentence', '').strip()
        if len(text) < 10:
            continue

        if speaker.id not in candidates:
            candidates[speaker.id] = []
        candidates[speaker.id].append((segment_idx, text[:200], segment.get('start_time')))

    # Delete existing snippets for this recording (re-save replaces them)
    SpeakerSnippet.query.filter_by(recording_id=recording_id).delete()

    snippets_created = 0

    for speaker_id, segs in candidates.items():
        # Pick up to MAX_SNIPPETS_PER_RECORDING spread across the transcript
        if len(segs) <= MAX_SNIPPETS_PER_RECORDING:
            chosen = segs
        else:
            # Evenly sample from the segments
            step = len(segs) / MAX_SNIPPETS_PER_RECORDING
            chosen = [segs[int(i * step)] for i in range(MAX_SNIPPETS_PER_RECORDING)]

        for segment_idx, text_snippet, timestamp in chosen:
            # Evict oldest if at global cap
            global_count = SpeakerSnippet.query.filter_by(speaker_id=speaker_id).count()
            if global_count >= MAX_SNIPPETS_PER_SPEAKER:
                oldest = SpeakerSnippet.query.filter_by(speaker_id=speaker_id)\
                    .order_by(SpeakerSnippet.created_at.asc()).first()
                if oldest:
                    db.session.delete(oldest)
                    db.session.flush()

            snippet = SpeakerSnippet(
                speaker_id=speaker_id,
                recording_id=recording_id,
                segment_index=segment_idx,
                text_snippet=text_snippet,
                timestamp=timestamp,
            )
            db.session.add(snippet)
            snippets_created += 1

        # Flush after each speaker batch to keep counts accurate
        db.session.flush()

    if snippets_created > 0:
        db.session.commit()

    return snippets_created


def _generate_dynamic_snippets(speaker_id, limit=3):
    """
    Dynamically generate audio snippets from a speaker's recent recordings.

    This function finds short audio segments (3-4 seconds) from recent recordings
    where the speaker appears. These can be played back to verify speaker identity.

    Args:
        speaker_id: ID of the speaker
        limit: Maximum number of snippets to return (default 3)

    Returns:
        list: List of snippet dictionaries with audio segment information
              [{'recording_id': 123, 'start_time': 45.2, 'duration': 3.5, ...}, ...]
    """
    # Get the speaker
    speaker = Speaker.query.get(speaker_id)
    if not speaker:
        return []

    # Find recordings that have this speaker's name in transcription
    # We'll look at the last 10 recordings and extract snippets from them
    recordings = Recording.query.filter_by(user_id=speaker.user_id)\
        .filter(Recording.transcription.isnot(None))\
        .filter(Recording.transcription != '')\
        .filter(Recording.audio_deleted_at.is_(None))\
        .order_by(Recording.created_at.desc())\
        .limit(10).all()

    snippets = []

    for recording in recordings:
        if len(snippets) >= limit:
            break

        try:
            # Parse transcription JSON
            transcript = json.loads(recording.transcription)

            if not isinstance(transcript, list):
                continue

            # Find segments where this speaker appears
            speaker_segments = []
            for idx, segment in enumerate(transcript):
                # Check if segment has speaker identification matching our speaker's name
                speaker_label = segment.get('speaker')

                # In identified transcripts, the speaker field contains the actual name
                if speaker_label != speaker.name:
                    continue

                start_time = segment.get('start_time')
                end_time = segment.get('end_time')

                if start_time is None or end_time is None:
                    continue

                duration = end_time - start_time

                # Skip very short segments (less than 2 seconds)
                if duration < 2.0:
                    continue

                speaker_segments.append({
                    'index': idx,
                    'start_time': start_time,
                    'end_time': end_time,
                    'duration': duration,
                    'text': segment.get('sentence', '').strip()[:100]  # Preview text
                })

            if not speaker_segments:
                continue

            # Take snippets from middle portions (skip first and last 10%)
            total_segments = len(speaker_segments)
            if total_segments > 4:
                # Skip first and last 10%
                start_idx = max(1, int(total_segments * 0.1))
                end_idx = min(total_segments - 1, int(total_segments * 0.9))
                middle_segments = speaker_segments[start_idx:end_idx]
            else:
                middle_segments = speaker_segments

            # Take 1 snippet per recording from the middle
            if middle_segments:
                # Pick a segment from the middle
                middle_idx = len(middle_segments) // 2
                segment = middle_segments[middle_idx]

                # Limit audio snippet to 3-4 seconds
                snippet_duration = min(4.0, segment['duration'])

                snippets.append({
                    'id': None,  # Dynamic snippet, no database ID
                    'speaker_id': speaker_id,
                    'recording_id': recording.id,
                    'start_time': segment['start_time'],
                    'duration': snippet_duration,
                    'text': segment['text'],  # Preview text for context
                    'recording_title': recording.title or 'Untitled Recording',
                    'created_at': recording.created_at.isoformat() if recording.created_at else None
                })

        except (json.JSONDecodeError, TypeError, KeyError) as e:
            # Skip recordings with invalid transcription format
            continue

    return snippets


def get_speaker_snippets(speaker_id, limit=3):
    """
    Get recent audio snippets for a speaker.

    Returns short audio segments (3-4 seconds) from recent recordings where this
    speaker appears. These audio snippets can be played to verify speaker identity.

    Args:
        speaker_id: ID of the speaker
        limit: Maximum number of snippets to return (default 3)

    Returns:
        list: List of snippet dictionaries with audio segment information
              [{'recording_id': 123, 'start_time': 45.2, 'duration': 3.5, ...}, ...]
    """
    # Always dynamically generate audio snippets from recent recordings
    return _generate_dynamic_snippets(speaker_id, limit)


def get_snippets_by_recording(recording_id, speaker_id):
    """
    Get all snippets for a specific speaker in a specific recording.

    Args:
        recording_id: ID of the recording
        speaker_id: ID of the speaker

    Returns:
        list: List of snippet dictionaries
    """
    snippets = SpeakerSnippet.query.filter_by(
        recording_id=recording_id,
        speaker_id=speaker_id
    ).order_by(SpeakerSnippet.segment_index).all()

    return [snippet.to_dict() for snippet in snippets]


def cleanup_old_snippets(speaker_id, keep=10):
    """
    Clean up old snippets for a speaker, keeping only the most recent ones.

    Args:
        speaker_id: ID of the speaker
        keep: Number of snippets to keep (default 10)

    Returns:
        int: Number of snippets deleted
    """
    # Get all snippets for this speaker, ordered by creation date
    all_snippets = SpeakerSnippet.query.filter_by(speaker_id=speaker_id)\
        .order_by(SpeakerSnippet.created_at.desc()).all()

    if len(all_snippets) <= keep:
        return 0

    # Delete old snippets beyond the keep limit
    snippets_to_delete = all_snippets[keep:]
    deleted_count = 0

    for snippet in snippets_to_delete:
        db.session.delete(snippet)
        deleted_count += 1

    if deleted_count > 0:
        db.session.commit()

    return deleted_count


def delete_snippets_for_recording(recording_id):
    """
    Delete all snippets associated with a recording.

    This is typically called when a recording is deleted or reprocessed.

    Args:
        recording_id: ID of the recording

    Returns:
        int: Number of snippets deleted
    """
    deleted_count = SpeakerSnippet.query.filter_by(recording_id=recording_id).delete()
    db.session.commit()
    return deleted_count


def get_speaker_recordings_with_snippets(speaker_id):
    """
    Get a list of recordings that have snippets for this speaker.

    Args:
        speaker_id: ID of the speaker

    Returns:
        list: List of recording dictionaries with snippet counts
              [{'id': 123, 'title': '...', 'snippet_count': 3, 'date': '...'}, ...]
    """
    # Get distinct recordings with snippet counts
    from sqlalchemy import func

    recordings_with_counts = db.session.query(
        Recording.id,
        Recording.title,
        Recording.created_at,
        func.count(SpeakerSnippet.id).label('snippet_count')
    ).join(
        SpeakerSnippet,
        Recording.id == SpeakerSnippet.recording_id
    ).filter(
        SpeakerSnippet.speaker_id == speaker_id
    ).group_by(
        Recording.id
    ).order_by(
        Recording.created_at.desc()
    ).all()

    return [{
        'id': r.id,
        'title': r.title,
        'snippet_count': r.snippet_count,
        'created_at': r.created_at.isoformat() if r.created_at else None
    } for r in recordings_with_counts]
