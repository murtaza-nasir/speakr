"""
Embedding generation and semantic search services.
"""

import os
import numpy as np
from flask import current_app
from sqlalchemy.orm import joinedload

try:
    from sentence_transformers import SentenceTransformer
    from sklearn.metrics.pairwise import cosine_similarity
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    cosine_similarity = None

from src.database import db
from src.models import Recording, TranscriptChunk, InternalShare, RecordingTag

ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'

# Initialize embedding model (lazy loading)
_embedding_model = None



def get_embedding_model():
    """Get or initialize the sentence transformer model."""
    global _embedding_model
    
    if not EMBEDDINGS_AVAILABLE:
        return None
        
    if _embedding_model is None:
        try:
            _embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            current_app.logger.info("Embedding model loaded successfully")
        except Exception as e:
            current_app.logger.error(f"Failed to load embedding model: {e}")
            return None
    return _embedding_model



def chunk_transcription(transcription, max_chunk_length=500, overlap=50):
    """
    Split transcription into overlapping chunks for better context retrieval.
    
    Args:
        transcription (str): The full transcription text
        max_chunk_length (int): Maximum characters per chunk
        overlap (int): Character overlap between chunks
    
    Returns:
        list: List of text chunks
    """
    if not transcription or len(transcription) <= max_chunk_length:
        return [transcription] if transcription else []
    
    chunks = []
    start = 0
    
    while start < len(transcription):
        end = start + max_chunk_length
        
        # Try to break at sentence boundaries
        if end < len(transcription):
            # Look for sentence endings within the last 100 characters
            sentence_end = -1
            for i in range(max(0, end - 100), end):
                if transcription[i] in '.!?':
                    # Check if it's not an abbreviation
                    if i + 1 < len(transcription) and transcription[i + 1].isspace():
                        sentence_end = i + 1
            
            if sentence_end > start:
                end = sentence_end
        
        chunk = transcription[start:end].strip()
        if chunk:
            chunks.append(chunk)
        
        # Move start position with overlap
        start = max(start + 1, end - overlap)
        
        # Prevent infinite loop
        if start >= len(transcription):
            break
    
    return chunks



def generate_embeddings(texts):
    """
    Generate embeddings for a list of texts.
    
    Args:
        texts (list): List of text strings
    
    Returns:
        list: List of embedding vectors as numpy arrays, or empty list if embeddings unavailable
    """
    if not EMBEDDINGS_AVAILABLE:
        current_app.logger.warning("Embeddings not available - skipping embedding generation")
        return []
        
    model = get_embedding_model()
    if not model or not texts:
        return []
    
    try:
        embeddings = model.encode(texts)
        return [embedding.astype(np.float32) for embedding in embeddings]
    except Exception as e:
        current_app.logger.error(f"Error generating embeddings: {e}")
        return []



def serialize_embedding(embedding):
    """Convert numpy array to binary for database storage."""
    if embedding is None or not EMBEDDINGS_AVAILABLE:
        return None
    return embedding.tobytes()



def deserialize_embedding(binary_data):
    """Convert binary data back to numpy array."""
    if binary_data is None or not EMBEDDINGS_AVAILABLE:
        return None
    return np.frombuffer(binary_data, dtype=np.float32)



def get_accessible_recording_ids(user_id):
    """
    Get all recording IDs that a user has access to.

    Includes:
    - Recordings owned by the user
    - Recordings shared with the user via InternalShare
    - Recordings shared via group tags (if team membership exists)

    Args:
        user_id (int): User ID to check access for

    Returns:
        list: List of recording IDs the user can access
    """
    accessible_ids = set()

    # 1. User's own recordings
    own_recordings = db.session.query(Recording.id).filter_by(user_id=user_id).all()
    accessible_ids.update([r.id for r in own_recordings])

    # 2. Internally shared recordings
    if ENABLE_INTERNAL_SHARING:
        shared_recordings = db.session.query(InternalShare.recording_id).filter_by(
            shared_with_user_id=user_id
        ).all()
        accessible_ids.update([r.recording_id for r in shared_recordings])

    return list(accessible_ids)



def process_recording_chunks(recording_id):
    """
    Process a recording by creating chunks and generating embeddings.
    This should be called after a recording is transcribed.
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not recording.transcription:
            return False
        
        # Delete existing chunks for this recording
        TranscriptChunk.query.filter_by(recording_id=recording_id).delete()
        
        # Create chunks
        chunks = chunk_transcription(recording.transcription)
        
        if not chunks:
            return True
        
        # Generate embeddings
        embeddings = generate_embeddings(chunks)
        
        # Store chunks in database
        for i, (chunk_text, embedding) in enumerate(zip(chunks, embeddings)):
            chunk = TranscriptChunk(
                recording_id=recording_id,
                user_id=recording.user_id,
                chunk_index=i,
                content=chunk_text,
                embedding=serialize_embedding(embedding) if embedding is not None else None
            )
            db.session.add(chunk)
        
        db.session.commit()
        current_app.logger.info(f"Created {len(chunks)} chunks for recording {recording_id}")
        return True
        
    except Exception as e:
        current_app.logger.error(f"Error processing chunks for recording {recording_id}: {e}")
        db.session.rollback()
        return False



def basic_text_search_chunks(user_id, query, filters=None, top_k=5):
    """
    Basic text search fallback when embeddings are not available.
    Uses simple text matching instead of semantic search.
    Searches across user's own recordings and recordings shared with them.
    """
    try:
        # Get all accessible recording IDs (own + shared)
        accessible_recording_ids = get_accessible_recording_ids(user_id)

        if not accessible_recording_ids:
            return []

        # Build base query for chunks from accessible recordings
        chunks_query = TranscriptChunk.query.filter(
            TranscriptChunk.recording_id.in_(accessible_recording_ids)
        )
        
        # Apply filters if provided
        if filters:
            if filters.get('tag_ids'):
                chunks_query = chunks_query.join(Recording).join(
                    RecordingTag, Recording.id == RecordingTag.recording_id
                ).filter(RecordingTag.tag_id.in_(filters['tag_ids']))
            
            if filters.get('speaker_names'):
                # Filter by participants field in recordings instead of chunk speaker_name
                if not any(hasattr(desc, 'name') and desc.name == 'recording' for desc in chunks_query.column_descriptions):
                    chunks_query = chunks_query.join(Recording)
                
                # Build OR conditions for each speaker name in participants
                speaker_conditions = []
                for speaker_name in filters['speaker_names']:
                    speaker_conditions.append(
                        Recording.participants.ilike(f'%{speaker_name}%')
                    )
                
                chunks_query = chunks_query.filter(db.or_(*speaker_conditions))
                current_app.logger.info(f"Applied speaker filter for: {filters['speaker_names']}")
            
            if filters.get('recording_ids'):
                chunks_query = chunks_query.filter(
                    TranscriptChunk.recording_id.in_(filters['recording_ids'])
                )
            
            if filters.get('date_from') or filters.get('date_to'):
                chunks_query = chunks_query.join(Recording)
                if filters.get('date_from'):
                    chunks_query = chunks_query.filter(Recording.meeting_date >= filters['date_from'])
                if filters.get('date_to'):
                    chunks_query = chunks_query.filter(Recording.meeting_date <= filters['date_to'])
        
        # Simple text search - split query into words and search for them
        query_words = query.lower().split()
        if query_words:
            # Create a filter that matches any of the query words in the content
            text_conditions = []
            for word in query_words:
                text_conditions.append(TranscriptChunk.content.ilike(f'%{word}%'))
            
            # Combine conditions with OR
            from sqlalchemy import or_
            chunks_query = chunks_query.filter(or_(*text_conditions))
        
        # Get chunks and return with dummy similarity scores
        chunks = chunks_query.limit(top_k).all()
        
        # Return chunks with dummy similarity scores (1.0 for found chunks)
        return [(chunk, 1.0) for chunk in chunks]
        
    except Exception as e:
        current_app.logger.error(f"Error in basic text search: {e}")
        return []



def semantic_search_chunks(user_id, query, filters=None, top_k=5):
    """
    Perform semantic search on transcript chunks with filtering.
    Searches across user's own recordings and recordings shared with them.

    Args:
        user_id (int): User ID for permission filtering
        query (str): Search query
        filters (dict): Optional filters for tags, speakers, dates, recording_ids
        top_k (int): Number of top chunks to return

    Returns:
        list: List of relevant chunks with similarity scores
    """
    try:
        # If embeddings are not available, fall back to basic text search
        if not EMBEDDINGS_AVAILABLE:
            current_app.logger.info("Embeddings not available - using basic text search as fallback")
            return basic_text_search_chunks(user_id, query, filters, top_k)

        # Generate embedding for the query
        model = get_embedding_model()
        if not model:
            return basic_text_search_chunks(user_id, query, filters, top_k)

        query_embedding = model.encode([query])[0]

        # Get all accessible recording IDs (own + shared)
        accessible_recording_ids = get_accessible_recording_ids(user_id)

        if not accessible_recording_ids:
            return []

        # Build base query for chunks from accessible recordings with eager loading
        chunks_query = TranscriptChunk.query.options(joinedload(TranscriptChunk.recording)).filter(
            TranscriptChunk.recording_id.in_(accessible_recording_ids)
        )
        
        # Apply filters if provided
        if filters:
            if filters.get('tag_ids'):
                # Join with recordings that have specified tags
                chunks_query = chunks_query.join(Recording).join(
                    RecordingTag, Recording.id == RecordingTag.recording_id
                ).filter(RecordingTag.tag_id.in_(filters['tag_ids']))
            
            if filters.get('speaker_names'):
                # Filter by participants field in recordings instead of chunk speaker_name
                if not any(hasattr(desc, 'name') and desc.name == 'recording' for desc in chunks_query.column_descriptions):
                    chunks_query = chunks_query.join(Recording)
                
                # Build OR conditions for each speaker name in participants
                speaker_conditions = []
                for speaker_name in filters['speaker_names']:
                    speaker_conditions.append(
                        Recording.participants.ilike(f'%{speaker_name}%')
                    )
                
                chunks_query = chunks_query.filter(db.or_(*speaker_conditions))
                current_app.logger.info(f"Applied speaker filter for: {filters['speaker_names']}")
            
            if filters.get('recording_ids'):
                chunks_query = chunks_query.filter(
                    TranscriptChunk.recording_id.in_(filters['recording_ids'])
                )
            
            if filters.get('date_from') or filters.get('date_to'):
                chunks_query = chunks_query.join(Recording)
                if filters.get('date_from'):
                    chunks_query = chunks_query.filter(Recording.meeting_date >= filters['date_from'])
                if filters.get('date_to'):
                    chunks_query = chunks_query.filter(Recording.meeting_date <= filters['date_to'])
        
        # Get chunks that have embeddings
        chunks = chunks_query.filter(TranscriptChunk.embedding.isnot(None)).all()
        
        if not chunks:
            return []
        
        # Calculate similarities
        chunk_similarities = []
        for chunk in chunks:
            try:
                chunk_embedding = deserialize_embedding(chunk.embedding)
                if chunk_embedding is not None:
                    similarity = cosine_similarity(
                        query_embedding.reshape(1, -1),
                        chunk_embedding.reshape(1, -1)
                    )[0][0]
                    chunk_similarities.append((chunk, float(similarity)))
            except Exception as e:
                current_app.logger.warning(f"Error calculating similarity for chunk {chunk.id}: {e}")
                continue
        
        # Sort by similarity and return top k
        chunk_similarities.sort(key=lambda x: x[1], reverse=True)
        return chunk_similarities[:top_k]
        
    except Exception as e:
        current_app.logger.error(f"Error in semantic search: {e}")
        return []

# --- Helper Functions for Document Processing ---



