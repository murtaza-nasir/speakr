"""
Speaker Embedding Matcher Service.

This service handles voice embedding comparison and matching for speaker identification.
It provides functions to:
- Serialize/deserialize speaker embeddings for database storage
- Calculate cosine similarity between voice embeddings
- Find matching speakers based on voice similarity
- Update speaker profiles with new embeddings
- Calculate confidence scores for speaker profiles

Uses 256-dimensional embeddings from WhisperX diarization.
"""

import json
import logging
import numpy as np
from datetime import datetime
try:
    from sklearn.metrics.pairwise import cosine_similarity
except ImportError:
    cosine_similarity = None
from src.database import db
from src.models import Speaker

logger = logging.getLogger(__name__)


def serialize_embedding(embedding_array):
    """
    Convert numpy array or list to binary for database storage.

    Args:
        embedding_array: numpy array or list of floats (256 dimensions)

    Returns:
        bytes: Binary representation (1,024 bytes for 256 × float32)
    """
    return np.array(embedding_array, dtype=np.float32).tobytes()


def deserialize_embedding(binary_data):
    """
    Convert binary data back to numpy array.

    Args:
        binary_data: bytes from database (1,024 bytes)

    Returns:
        numpy.ndarray: 256-dimensional float32 array
    """
    return np.frombuffer(binary_data, dtype=np.float32)


def calculate_similarity(embedding1, embedding2):
    """
    Compute cosine similarity between two 256-dimensional voice embeddings.

    Args:
        embedding1: numpy array, list, or binary data
        embedding2: numpy array, list, or binary data

    Returns:
        float: Similarity score (0-1, where 1 is identical)
    """
    # Convert to numpy arrays if needed
    e1 = np.array(embedding1, dtype=np.float32).reshape(1, -1)
    e2 = np.array(embedding2, dtype=np.float32).reshape(1, -1)

    # Cosine similarity returns values from -1 to 1
    # For voice embeddings, we typically see 0.6-0.99 range
    return float(cosine_similarity(e1, e2)[0][0])


def find_matching_speakers(target_embedding, user_id, threshold=0.70, space_id=None):
    """
    Find speakers matching a target voice embedding for a specific user.

    Kept for callers of the original API. Matching itself lives in
    services/voice_profiles.py: each person is compared through their voice
    variants (the closest counts), only within one embedding space.

    Returns:
        list: [{'speaker_id', 'name', 'similarity' (percent), 'confidence',
                'embedding_count', 'variant_count'}], best first
    """
    from src.services.voice_profiles import find_matches
    return find_matches(target_embedding, user_id, space_id=space_id, threshold=threshold)


def update_speaker_embedding(speaker, new_embedding, recording_id):
    """
    Update a speaker's average embedding and history with a new sample.

    Uses weighted moving average to update the profile:
    - New embeddings get 30% weight
    - Existing average gets 70% weight

    Args:
        speaker: Speaker model instance
        new_embedding: New voice embedding (256-dim array/list)
        recording_id: ID of the recording this embedding came from

    Returns:
        float: Similarity between new embedding and previous average (None if first)
    """
    new_emb_array = np.array(new_embedding, dtype=np.float32)
    similarity_to_avg = None

    if speaker.average_embedding is None:
        # First embedding for this speaker
        speaker.average_embedding = serialize_embedding(new_emb_array)
        speaker.embedding_count = 1
        speaker.embeddings_history = [{
            'recording_id': recording_id,
            'timestamp': datetime.utcnow().isoformat(),
            'similarity': 100.0  # Perfect match to itself
        }]
    else:
        # Update existing average
        current_avg = deserialize_embedding(speaker.average_embedding)
        similarity_to_avg = calculate_similarity(new_emb_array, current_avg)

        # Weighted average: 30% new, 70% existing
        # This prevents sudden shifts while still adapting to voice changes
        weight = 0.3
        updated_avg = (1 - weight) * current_avg + weight * new_emb_array

        speaker.average_embedding = serialize_embedding(updated_avg)
        speaker.embedding_count += 1

        # Add to history (keep last 10 entries)
        history = speaker.embeddings_history or []
        history.append({
            'recording_id': recording_id,
            'timestamp': datetime.utcnow().isoformat(),
            'similarity': round(similarity_to_avg * 100, 1)
        })
        speaker.embeddings_history = history[-10:]  # Keep most recent 10

    # Recalculate confidence score
    speaker.confidence_score = calculate_confidence(speaker)

    # Commit changes
    db.session.commit()

    return similarity_to_avg


def calculate_confidence(speaker):
    """
    Calculate confidence score based on embedding consistency.

    Confidence is based on:
    - Number of samples (more is better)
    - Consistency of embeddings (high similarity scores = high confidence)

    Args:
        speaker: Speaker model instance with embeddings_history

    Returns:
        float: Confidence score (0-1)
    """
    if speaker.embedding_count is None or speaker.embedding_count < 1:
        return 0.0

    if speaker.embedding_count == 1:
        return 0.5  # Medium confidence with single sample

    # Get recent similarity scores from history
    history = speaker.embeddings_history or []
    if len(history) < 2:
        return 0.5

    # Use last 5 samples
    recent_history = history[-5:]
    similarities = [h.get('similarity', 0) / 100.0 for h in recent_history]

    # Average similarity to the profile
    avg_similarity = sum(similarities) / len(similarities)

    # Penalize if we have very few samples
    sample_factor = min(1.0, speaker.embedding_count / 5.0)

    # Confidence = average similarity × sample factor
    confidence = avg_similarity * sample_factor

    return min(1.0, max(0.0, confidence))


def get_speaker_voice_profile_summary(speaker):
    """
    Get a human-readable summary of a speaker's voice profile.

    Args:
        speaker: Speaker model instance

    Returns:
        dict: Profile summary with statistics and status
    """
    if not speaker.average_embedding:
        return {
            'has_profile': False,
            'message': 'No voice profile yet'
        }

    return {
        'has_profile': True,
        'embedding_count': speaker.embedding_count or 0,
        'confidence_score': speaker.confidence_score or 0.0,
        'confidence_level': _get_confidence_level(speaker.confidence_score),
        'last_updated': speaker.embeddings_history[-1]['timestamp'] if speaker.embeddings_history else None,
        'recordings': len(speaker.embeddings_history or [])
    }


def _get_confidence_level(score):
    """
    Convert numeric confidence score to human-readable level.

    Args:
        score: float (0-1)

    Returns:
        str: 'low', 'medium', or 'high'
    """
    if score is None or score < 0.6:
        return 'low'
    elif score < 0.8:
        return 'medium'
    else:
        return 'high'


# Threshold mapping for auto-labelling
AUTO_LABEL_THRESHOLDS = {
    'low': 0.3,      # Aggressive, may have more false positives
    'medium': 0.6,   # Default, balanced approach
    'high': 0.8      # Only auto-label well-established speakers
}

# Base similarity threshold for finding matches (70%)
BASE_SIMILARITY_THRESHOLD = 0.70

# Ambiguity threshold: if top 2 matches are within 5% similarity, skip
AMBIGUITY_MARGIN = 0.05


def apply_auto_speaker_labels(recording, user):
    """
    Match a new recording's speakers against the user's voice profiles.

    Returns {SPEAKER_XX: name}. One person per label and one label per
    person; labels with too little speech, and labels whose two best
    candidates are nearly tied, are left alone. The threshold is the user's
    low / medium / high setting, calibrated to the embedding space once it
    has enough samples (services/voice_profiles.py).
    """
    if not user.auto_speaker_labelling or not recording.speaker_embeddings:
        return {}
    from src.services.voice_profiles import auto_label_map, auto_label_threshold
    threshold = auto_label_threshold(user.auto_speaker_labelling_threshold or 'medium',
                                     recording.speaker_embeddings_space_id)
    return auto_label_map(recording, user, threshold)


def apply_speaker_names_to_transcription(recording, speaker_map):
    """
    Apply speaker name mappings to a recording's transcription.

    This function updates the transcription JSON by replacing generic speaker
    labels (SPEAKER_00, SPEAKER_01, etc.) with actual speaker names, and
    updates the recording's participants list.

    Args:
        recording: Recording model instance with transcription
        speaker_map: Dict mapping {SPEAKER_XX: speaker_name}

    Returns:
        bool: True if changes were made, False otherwise
    """
    import logging
    logger = logging.getLogger(__name__)

    if not speaker_map or not recording.transcription:
        logger.warning(f"Auto-label: No speaker_map or transcription (map={bool(speaker_map)}, trans={bool(recording.transcription)})")
        return False

    try:
        # Parse transcription as JSON array: [{speaker, sentence, start_time, end_time}, ...]
        segments = json.loads(recording.transcription)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Auto-label: Failed to parse transcription as JSON: {e}")
        return False

    if not isinstance(segments, list) or not segments:
        logger.warning(f"Auto-label: Transcription not in expected array format")
        return False

    # Track which speakers were renamed
    renamed_speakers = set()

    # Update speaker labels in segments
    for segment in segments:
        if 'speaker' in segment and segment['speaker'] in speaker_map:
            segment['speaker'] = speaker_map[segment['speaker']]
            renamed_speakers.add(segment['speaker'])

    if not renamed_speakers:
        logger.warning(f"Auto-label: No speakers matched in segments")
        return False

    logger.info(f"Auto-label: Applied names to {len(renamed_speakers)} speakers: {renamed_speakers}")

    # Participants: real names only, never leftover SPEAKER_XX labels
    from src.services.speaker import participants_from_segments
    recording.participants = participants_from_segments(segments)

    # Save updated transcription
    recording.transcription = json.dumps(segments)
    db.session.commit()

    return True


def update_speaker_profiles_from_recording(recording, speaker_map, user):
    """
    Train voice profiles from auto-applied labels.

    Samples from auto-labelling carry half the weight of names a person
    confirmed, and confirming or correcting the name later in the speaker
    dialog replaces them.

    Returns:
        int: Number of speaker profiles updated
    """
    if not speaker_map or not recording.speaker_embeddings:
        return 0
    from src.services.voice_profiles import apply_names_to_profiles
    stats = apply_names_to_profiles(recording, dict(speaker_map), None, user, source='auto')
    for name in set(speaker_map.values()):
        speaker = Speaker.query.filter_by(user_id=user.id, name=name).first()
        if speaker:
            speaker.use_count = (speaker.use_count or 0) + 1
            speaker.last_used = datetime.utcnow()
    db.session.commit()
    return stats['stored']
