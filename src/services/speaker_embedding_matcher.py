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

import numpy as np
from datetime import datetime
from sklearn.metrics.pairwise import cosine_similarity
from src.database import db
from src.models import Speaker


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


def find_matching_speakers(target_embedding, user_id, threshold=0.70):
    """
    Find speakers matching a target voice embedding for a specific user.

    Args:
        target_embedding: The voice embedding to match against (256-dim array/list)
        user_id: User ID to search within
        threshold: Minimum similarity score (0-1, default 0.70 = 70%)

    Returns:
        list: Sorted list of matching speakers with scores
              [{'speaker_id': 5, 'name': 'John', 'similarity': 85.3, 'confidence': 0.92}, ...]
    """
    # Get all speakers with embeddings for this user
    speakers = Speaker.query.filter_by(user_id=user_id).filter(
        Speaker.average_embedding.isnot(None)
    ).all()

    if not speakers:
        return []

    matches = []
    for speaker in speakers:
        try:
            # Deserialize and compare
            speaker_emb = deserialize_embedding(speaker.average_embedding)
            similarity = calculate_similarity(target_embedding, speaker_emb)

            if similarity >= threshold:
                matches.append({
                    'speaker_id': speaker.id,
                    'name': speaker.name,
                    'similarity': round(similarity * 100, 1),  # Convert to percentage
                    'confidence': speaker.confidence_score or 0.5,
                    'embedding_count': speaker.embedding_count or 0
                })
        except Exception as e:
            # Skip speakers with corrupted embeddings
            continue

    # Sort by similarity (highest first)
    return sorted(matches, key=lambda x: x['similarity'], reverse=True)


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
