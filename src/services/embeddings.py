"""
Embedding generation and semantic search services.
"""

import os
import re
import json
import time
import random
import numpy as np
from flask import current_app
from sqlalchemy.orm import joinedload

try:
    from sentence_transformers import SentenceTransformer
    from sklearn.metrics.pairwise import cosine_similarity
    LOCAL_EMBEDDINGS_AVAILABLE = True
except ImportError:
    LOCAL_EMBEDDINGS_AVAILABLE = False
    SentenceTransformer = None  # type: ignore
    cosine_similarity = None

# sklearn's cosine_similarity may be available even when sentence-transformers
# is not (e.g., the lite image with API-mode embeddings). Try to import it
# independently so semantic search can still run on API-generated vectors.
if cosine_similarity is None:
    try:
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore
    except ImportError:
        cosine_similarity = None

from src.database import db
from src.models import Recording, TranscriptChunk, InternalShare, RecordingTag

ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'

# Embedding model used to generate semantic-search vectors. By default Speakr
# loads sentence-transformers locally; setting EMBEDDING_BASE_URL switches to
# an OpenAI-compatible HTTP endpoint (vLLM, OpenRouter, OpenAI directly, etc.)
# and the same EMBEDDING_MODEL value is sent as the model name in requests.
# Changing the model or the endpoint after chunks are already embedded will
# produce a dimensionality / semantic-space mismatch; see the startup warning
# in src/init_db.py and the docs for guidance.
EMBEDDING_MODEL = os.environ.get('EMBEDDING_MODEL', 'all-MiniLM-L6-v2').strip() or 'all-MiniLM-L6-v2'
EMBEDDING_BASE_URL = os.environ.get('EMBEDDING_BASE_URL', '').strip()
EMBEDDING_API_KEY = os.environ.get('EMBEDDING_API_KEY', '').strip()
_dim_raw = os.environ.get('EMBEDDING_DIMENSIONS', '').strip()
try:
    EMBEDDING_DIMENSIONS = int(_dim_raw) if _dim_raw else None
except (TypeError, ValueError):
    EMBEDDING_DIMENSIONS = None

# When EMBEDDING_BASE_URL is set, embeddings are produced via an OpenAI-
# compatible /v1/embeddings call rather than locally.
USE_API_EMBEDDINGS = bool(EMBEDDING_BASE_URL)

# Identifier persisted to system_setting so the dimensionality compatibility
# check covers both "model changed" and "provider changed" scenarios.
EMBEDDING_IDENTIFIER = f"{EMBEDDING_BASE_URL or 'local'}::{EMBEDDING_MODEL}"

# True when at least one embedding path can produce vectors. The legacy name
# EMBEDDINGS_AVAILABLE is preserved for callers that import it elsewhere.
EMBEDDINGS_AVAILABLE = LOCAL_EMBEDDINGS_AVAILABLE or (USE_API_EMBEDDINGS and cosine_similarity is not None)

# Initialize embedding model (lazy loading)
_embedding_model = None
_embedding_api_client = None



def get_embedding_model():
    """Get or initialize the local sentence transformer model.

    Returns None when running in API mode or when sentence-transformers is
    not installed.
    """
    global _embedding_model

    if USE_API_EMBEDDINGS or not LOCAL_EMBEDDINGS_AVAILABLE:
        return None

    if _embedding_model is None:
        try:
            _embedding_model = SentenceTransformer(EMBEDDING_MODEL)
            current_app.logger.info(f"Embedding model loaded successfully: {EMBEDDING_MODEL}")
        except Exception as e:
            current_app.logger.error(f"Failed to load embedding model {EMBEDDING_MODEL!r}: {e}")
            return None
    return _embedding_model


def get_embedding_api_client():
    """Get or initialize the OpenAI-compatible embeddings API client.

    Returns None when API mode is disabled or the openai package is missing.
    """
    global _embedding_api_client

    if not USE_API_EMBEDDINGS:
        return None

    if _embedding_api_client is None:
        try:
            from openai import OpenAI
            from src.services.llm import llm_timeout, LLM_MAX_RETRIES, http_client_no_proxy
            _embedding_api_client = OpenAI(
                api_key=EMBEDDING_API_KEY or "not-needed",
                base_url=EMBEDDING_BASE_URL,
                http_client=http_client_no_proxy,
                timeout=llm_timeout,
                max_retries=LLM_MAX_RETRIES,
            )
            current_app.logger.info(
                f"Embedding API client initialized: base_url={EMBEDDING_BASE_URL}, model={EMBEDDING_MODEL}"
            )
        except Exception as e:
            current_app.logger.error(f"Failed to initialize embedding API client: {e}")
            return None
    return _embedding_api_client


_API_EMBED_MAX_ATTEMPTS = int(os.environ.get('EMBEDDING_API_MAX_RETRIES', '3'))
# After a full retry cycle fails, skip the embedding API for this long so
# every search doesn't pay the retry latency while the provider is down.
_API_EMBED_COOLDOWN_SECONDS = int(os.environ.get('EMBEDDING_API_COOLDOWN_SECONDS', '120'))
_api_embed_down_until = 0.0
_API_EMBED_BASE_BACKOFF_SECONDS = float(os.environ.get('EMBEDDING_API_BACKOFF_SECONDS', '1.5'))

# Substrings of error messages that suggest the failure is transient and
# worth retrying. Auth and model-not-found errors do not match and fail fast.
_TRANSIENT_ERROR_HINTS = (
    'timeout', 'timed out', 'connection', 'connect',
    'rate limit', 'rate_limit', 'too many requests', '429',
    '500', '502', '503', '504',
    'temporarily unavailable', 'service unavailable',
    'overloaded', 'try again',
)


def _is_transient_embedding_error(exc):
    """Decide whether an embedding-API error is worth retrying."""
    msg = str(exc).lower()
    return any(hint in msg for hint in _TRANSIENT_ERROR_HINTS)


# Set to True the first time the provider rejects the 'dimensions' parameter
# (models without Matryoshka support, e.g. BAAI/bge-m3, return a 400). Later
# calls then omit the parameter instead of failing the whole pipeline. Probing
# the provider beats a hardcoded list of dimension-capable model names, which
# would silently disable a setting that newer models do support.
_dimensions_rejected = False


def _is_dimensions_rejection(exc):
    """True when the error looks like the model rejecting the 'dimensions' kwarg."""
    return 'dimension' in str(exc).lower()


def _api_embed(texts, user_id=None):
    """Call the OpenAI-compatible embeddings endpoint and return numpy vectors.

    Retries on transient errors (rate limits, timeouts, 5xx, connection
    blips) with exponential backoff. Auth or model-not-found errors fail
    fast since retrying will not help. On permanent failure, returns an
    empty list so callers see a clear sentinel; ``process_recording_chunks``
    treats a length mismatch between input and output as a failure and
    rolls back, so the recording's existing chunks are preserved instead
    of being silently deleted with nothing inserted in their place.

    Retry parameters are tunable via ``EMBEDDING_API_MAX_RETRIES`` (default
    3) and ``EMBEDDING_API_BACKOFF_SECONDS`` (default 1.5).

    When ``user_id`` is provided and the response includes a ``usage``
    block, record the call against the daily token-usage aggregate.
    """
    global _dimensions_rejected, _api_embed_down_until

    client = get_embedding_api_client()
    if client is None or not texts:
        return []

    # Batch large inputs: providers cap the number of inputs per embeddings
    # request (Gemini far lower than OpenAI), and a long recording can have
    # hundreds of chunks. All-or-nothing semantics are preserved: any failed
    # batch fails the whole call so callers keep their existing chunks.
    batch_size = max(1, int(os.environ.get('EMBEDDING_API_BATCH_SIZE', '96')))
    if len(texts) > batch_size:
        out = []
        for i in range(0, len(texts), batch_size):
            part = _api_embed(texts[i:i + batch_size], user_id=user_id)
            if len(part) != len(texts[i:i + batch_size]):
                return []
            out.extend(part)
        return out

    # Circuit breaker: after a full retry cycle fails, skip the API entirely
    # for a cooldown window instead of burning ~9s of retries on every call
    # (searches fall back to keyword matching immediately; indexing fails
    # fast with the usual empty-list sentinel and is retried later).
    if time.time() < _api_embed_down_until:
        current_app.logger.debug(
            "Embedding API in failure cooldown; skipping call and using fallback")
        return []

    # encoding_format='float' explicitly: OpenAI SDK v2 defaults to base64,
    # which some OpenAI-compatible providers cannot produce; float is the
    # format every compatible provider accepts.
    kwargs = {'input': texts, 'model': EMBEDDING_MODEL, 'encoding_format': 'float'}
    if EMBEDDING_DIMENSIONS is not None and not _dimensions_rejected:
        kwargs['dimensions'] = EMBEDDING_DIMENSIONS

    last_exc = None
    for attempt in range(1, _API_EMBED_MAX_ATTEMPTS + 1):
        try:
            response = client.embeddings.create(**kwargs)

            if user_id is not None and getattr(response, 'usage', None) is not None:
                try:
                    usage = response.usage
                    # Pydantic v2 stores non-schema fields under model_extra;
                    # fall back to attribute access for SDKs that surface them
                    # directly.
                    extras = getattr(usage, 'model_extra', None) or {}
                    prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
                    total_tokens = getattr(usage, 'total_tokens', prompt_tokens) or prompt_tokens
                    cost = extras.get('cost') if 'cost' in extras else getattr(usage, 'cost', None)
                    from src.services.token_tracking import token_tracker
                    token_tracker.record_usage(
                        user_id=user_id,
                        operation_type='embedding',
                        prompt_tokens=int(prompt_tokens),
                        completion_tokens=0,
                        total_tokens=int(total_tokens),
                        model_name=EMBEDDING_MODEL,
                        cost=float(cost) if cost is not None else None,
                    )
                except Exception as track_err:
                    current_app.logger.warning(f"Failed to record embedding usage: {track_err}")

            _api_embed_down_until = 0
            return [np.array(d.embedding, dtype=np.float32) for d in response.data]

        except Exception as e:
            last_exc = e
            if 'dimensions' in kwargs and _is_dimensions_rejection(e):
                _dimensions_rejected = True
                kwargs.pop('dimensions')
                current_app.logger.warning(
                    f"Embedding model '{EMBEDDING_MODEL}' rejected the 'dimensions' "
                    f"parameter (EMBEDDING_DIMENSIONS={EMBEDDING_DIMENSIONS}): {e}. "
                    "Retrying without it; the setting will be ignored for the rest "
                    "of this process."
                )
                continue
            transient = _is_transient_embedding_error(e)
            if not transient or attempt == _API_EMBED_MAX_ATTEMPTS:
                current_app.logger.error(
                    f"Embedding API call failed (attempt {attempt}/{_API_EMBED_MAX_ATTEMPTS}, "
                    f"transient={transient}): {e}"
                )
                _api_embed_down_until = time.time() + _API_EMBED_COOLDOWN_SECONDS
                return []
            # Exponential backoff with light jitter so concurrent retries
            # do not all hit the provider at the same instant.
            backoff = _API_EMBED_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            backoff *= 0.85 + 0.3 * random.random()
            current_app.logger.warning(
                f"Embedding API transient error (attempt {attempt}/{_API_EMBED_MAX_ATTEMPTS}), "
                f"retrying in {backoff:.1f}s: {e}"
            )
            time.sleep(backoff)

    # Loop exhausted without returning a result.
    current_app.logger.error(f"Embedding API call exhausted retries: {last_exc}")
    return []



# Segment-aware chunking target. ~1400 chars ≈ 300-350 tokens: large enough
# that a rapid back-and-forth exchange stays together in one chunk, small
# enough to stay a precise retrieval unit.
CHUNK_TARGET_CHARS = int(os.environ.get('CHUNK_TARGET_CHARS', '1400'))


def build_transcript_segments(transcription):
    """Parse a stored transcription into [{speaker, text, start, end}].

    Returns None when the transcription is not the JSON segment format
    (plain-text transcripts fall back to character windowing).
    """
    if not transcription:
        return None
    try:
        data = json.loads(transcription)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    segments = []
    for seg in data:
        if not isinstance(seg, dict):
            continue
        text = (seg.get('sentence') or seg.get('text') or '').strip()
        if not text:
            continue
        segments.append({
            'speaker': (seg.get('speaker') or '').strip() or None,
            'text': text,
            'start': seg.get('start_time'),
            'end': seg.get('end_time'),
        })
    return segments if segments else None


def _segment_line(seg):
    """Render one segment as a transcript-style line: 'Speaker: text'."""
    return f"{seg['speaker']}: {seg['text']}" if seg['speaker'] else seg['text']


def _finalize_segment_chunk(segs):
    """Build a chunk dict from a run of consecutive segments."""
    text = "\n".join(_segment_line(s) for s in segs)
    speakers = [s['speaker'] for s in segs if s['speaker']]
    # Dominant speaker goes in the metadata column (single exact name, so the
    # speaker-rename maintenance which matches by equality keeps working);
    # every speaker remains visible inline in the chunk text itself.
    dominant = max(set(speakers), key=speakers.count) if speakers else None
    starts = [s['start'] for s in segs if s['start'] is not None]
    ends = [s['end'] for s in segs if s['end'] is not None]
    return {
        'text': text,
        'speaker_name': dominant,
        'start_time': min(starts) if starts else None,
        'end_time': max(ends) if ends else None,
    }


def chunk_transcript_segments(transcription, max_chunk_chars=None):
    """Segment-aware chunking for diarized (JSON) transcripts.

    Strategy: render each segment as a 'Speaker: text' line and pack WHOLE
    segments into chunks up to ``max_chunk_chars``, never splitting a
    speaker turn across chunks (except single turns longer than the limit,
    which are sentence-split on their own). Chunk boundaries are size-based,
    not turn-based, so a quickfire exchange of many short turns lands
    together in one chunk with every speaker labeled inline. Consecutive
    chunks overlap by one segment for continuity.

    Returns a list of chunk dicts: {text, speaker_name (dominant),
    start_time, end_time}. Plain-text transcripts fall back to the classic
    character windows with null metadata.
    """
    # Backwards-compatibility escape hatch: CHUNKING_STRATEGY=legacy keeps
    # the pre-segment-aware behavior byte-identical (500-char windows over
    # the stored string, no speaker/time metadata) for admins who do not
    # want indexing behavior to change. Existing chunks are never touched
    # by an upgrade either way; the strategy only applies when a recording
    # is (re)indexed.
    if os.environ.get('CHUNKING_STRATEGY', 'segment').strip().lower() == 'legacy':
        return [{'text': t, 'speaker_name': None, 'start_time': None, 'end_time': None}
                for t in chunk_transcription(transcription)]

    max_chunk_chars = max_chunk_chars or CHUNK_TARGET_CHARS
    segments = build_transcript_segments(transcription)
    if segments is None:
        return [{'text': t, 'speaker_name': None, 'start_time': None, 'end_time': None}
                for t in chunk_transcription(transcription, max_chunk_length=max_chunk_chars,
                                             overlap=max(50, max_chunk_chars // 20))]

    chunks = []
    current = []
    current_len = 0
    for seg in segments:
        line_len = len(_segment_line(seg)) + 1
        if line_len > max_chunk_chars:
            # A single oversized turn: flush what we have, then sentence-split
            # the turn into its own chunks carrying its speaker and times.
            if current:
                chunks.append(_finalize_segment_chunk(current))
                current, current_len = [], 0
            for piece in chunk_transcription(seg['text'], max_chunk_length=max_chunk_chars,
                                             overlap=max(50, max_chunk_chars // 20)):
                chunks.append(_finalize_segment_chunk([{**seg, 'text': piece}]))
            continue
        if current and current_len + line_len > max_chunk_chars:
            chunks.append(_finalize_segment_chunk(current))
            # One-segment overlap so a thought spanning the boundary stays
            # findable from either side.
            tail = current[-1]
            current = [tail] if len(_segment_line(tail)) + line_len <= max_chunk_chars else []
            current_len = sum(len(_segment_line(s)) + 1 for s in current)
        current.append(seg)
        current_len += line_len
    if current:
        chunks.append(_finalize_segment_chunk(current))
    return chunks


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



def generate_embeddings(texts, user_id=None):
    """
    Generate embeddings for a list of texts.

    Routes through the OpenAI-compatible API when EMBEDDING_BASE_URL is set;
    otherwise uses the locally loaded sentence-transformers model.

    Args:
        texts (list): List of text strings
        user_id (int, optional): User to attribute the API call to for token
            and cost tracking. Local-mode calls do not consume billable usage
            and ignore this argument.

    Returns:
        list: List of embedding vectors as numpy arrays, or empty list if no
        embedding path is available.
    """
    if not texts:
        return []

    if USE_API_EMBEDDINGS:
        return _api_embed(texts, user_id=user_id)

    if not LOCAL_EMBEDDINGS_AVAILABLE:
        current_app.logger.warning("Embeddings not available - skipping embedding generation")
        return []

    model = get_embedding_model()
    if not model:
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

    Returns True on full success, False on any failure including the case
    where embedding generation returned fewer vectors than there were
    chunks. Embeddings are generated before the old chunks are deleted, so
    a failure leaves the recording's existing chunks intact and never holds
    a database write lock across the (potentially slow) embedding API call.
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not recording.transcription:
            return False

        # Create chunks and generate their embeddings BEFORE touching the
        # transcript_chunk table. The embeddings call is a network round-trip
        # that can take 30+ seconds against a cold local model; running it
        # inside an open write transaction held SQLite's single writer lock
        # for the whole duration and starved concurrent writers such as the
        # summarize-job enqueue (issue #355). Doing the slow work first keeps
        # the delete + insert + commit window down to milliseconds.
        chunks = chunk_transcript_segments(recording.transcription)

        if not chunks:
            TranscriptChunk.query.filter_by(recording_id=recording_id).delete()
            db.session.commit()
            return True

        # Generate embeddings (recording owner gets billed for API-mode usage)
        embeddings = generate_embeddings([c['text'] for c in chunks], user_id=recording.user_id)

        # Verify we got one embedding per chunk. _api_embed returns [] on
        # exhausted retries, and a partial provider response could return
        # fewer than expected. Either case is a failure; the recording's
        # existing chunks are untouched so the admin retry pass (or a later
        # Re-embed all) can try again.
        if len(embeddings) != len(chunks):
            current_app.logger.error(
                f"Embedding generation returned {len(embeddings)} vectors for "
                f"{len(chunks)} chunks on recording {recording_id}; keeping "
                f"existing chunks."
            )
            return False

        # Swap the chunks in one short transaction: the delete is staged and
        # only the final commit makes it permanent, so a failure here still
        # rolls back to the old chunks.
        TranscriptChunk.query.filter_by(recording_id=recording_id).delete()
        for i, (chunk_data, embedding) in enumerate(zip(chunks, embeddings)):
            chunk = TranscriptChunk(
                recording_id=recording_id,
                user_id=recording.user_id,
                chunk_index=i,
                content=chunk_data['text'],
                speaker_name=(chunk_data['speaker_name'] or None) and chunk_data['speaker_name'][:100],
                start_time=chunk_data['start_time'],
                end_time=chunk_data['end_time'],
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

        # Build base query for chunks from accessible recordings with eager loading
        chunks_query = TranscriptChunk.query.options(joinedload(TranscriptChunk.recording)).filter(
            TranscriptChunk.recording_id.in_(accessible_recording_ids)
        )
        
        # Apply filters if provided. The tag, speaker, and date filters all need
        # a join to Recording; join it at most once so combining more than one of
        # them does not raise a duplicate-JOIN / ambiguous-relationship error.
        if filters:
            if (filters.get('tag_ids') or filters.get('speaker_names')
                    or filters.get('date_from') or filters.get('date_to')):
                chunks_query = chunks_query.join(Recording)

            if filters.get('tag_ids'):
                chunks_query = chunks_query.join(
                    RecordingTag, Recording.id == RecordingTag.recording_id
                ).filter(RecordingTag.tag_id.in_(filters['tag_ids']))

            if filters.get('speaker_names'):
                # Filter by the participants field on the recording.
                speaker_conditions = [
                    Recording.participants.ilike(f'%{speaker_name}%')
                    for speaker_name in filters['speaker_names']
                ]
                chunks_query = chunks_query.filter(db.or_(*speaker_conditions))
                current_app.logger.info(f"Applied speaker filter for: {filters['speaker_names']}")

            if filters.get('recording_ids'):
                chunks_query = chunks_query.filter(
                    TranscriptChunk.recording_id.in_(filters['recording_ids'])
                )

            if filters.get('date_from'):
                chunks_query = chunks_query.filter(Recording.meeting_date >= filters['date_from'])
            if filters.get('date_to'):
                chunks_query = chunks_query.filter(Recording.meeting_date <= filters['date_to'])

        # Text search - filter stop words and rank by match count
        stop_words = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been',
                       'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                       'would', 'could', 'should', 'may', 'might', 'shall', 'can',
                       'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
                       'up', 'about', 'into', 'through', 'during', 'before', 'after',
                       'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both',
                       'it', 'its', 'this', 'that', 'these', 'those', 'what', 'which',
                       'who', 'whom', 'how', 'when', 'where', 'why',
                       'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she',
                       'his', 'her', 'they', 'them', 'their'}

        query_words = [w for w in query.lower().split() if w not in stop_words and len(w) > 1]

        if not query_words:
            # If all words were stop words, fall back to using original query words
            query_words = [w for w in query.lower().split() if len(w) > 1]

        if query_words:
            from sqlalchemy import or_, case

            # Filter: match ANY keyword (OR) to get candidates
            text_conditions = []
            for word in query_words:
                text_conditions.append(TranscriptChunk.content.ilike(f'%{word}%'))
            chunks_query = chunks_query.filter(or_(*text_conditions))

            # Rank IN SQL before limiting. The previous code applied
            # LIMIT before any ranking, so with a common word in the query
            # ("rate", "team") the candidate set filled up with arbitrary
            # low-id chunks and the actually-relevant chunks never entered
            # the ranking at all. Score = number of matched words, with the
            # full phrase counting double so exact-phrase hits always
            # outrank scattered single-word matches.
            score_expr = sum(
                case((TranscriptChunk.content.ilike(f'%{word}%'), 1), else_=0)
                for word in query_words
            )
            phrase = query.lower().strip()
            if len(query_words) > 1 and len(phrase) >= 5:
                score_expr = score_expr + case(
                    (TranscriptChunk.content.ilike(f'%{phrase}%'), len(query_words)),
                    else_=0,
                )
            chunks = (chunks_query
                      .order_by(score_expr.desc(), TranscriptChunk.id.desc())
                      .limit(top_k * 5)
                      .all())

            # Refine in Python over the now well-chosen candidates: exact
            # word-boundary matches beat substring matches ("rate" inside
            # "separate"), and phrase presence keeps its bonus.
            scored_chunks = []
            for chunk in chunks:
                content_lower = chunk.content.lower()
                match_count = 0.0
                for word in query_words:
                    if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
                        match_count += 1.0
                    elif word in content_lower:
                        match_count += 0.5
                if len(query_words) > 1 and phrase in content_lower:
                    match_count += len(query_words)
                # Full word-boundary matches on every word score 1.0; the
                # phrase bonus only breaks ties above, capped back to 1.0.
                score = min(match_count / len(query_words), 1.0)
                scored_chunks.append((chunk, score))

            # Sort by score descending, take top_k
            scored_chunks.sort(key=lambda x: x[1], reverse=True)
            return scored_chunks[:top_k]

        # No usable query words
        return []
        
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

        if cosine_similarity is None:
            current_app.logger.info("scikit-learn not installed - using basic text search as fallback")
            return basic_text_search_chunks(user_id, query, filters, top_k)

        # Generate embedding for the query (via API or local model). Attribute
        # the API call to the searching user so embedding cost tracking shows
        # who issued the query.
        if USE_API_EMBEDDINGS:
            api_vectors = _api_embed([query], user_id=user_id)
            if not api_vectors:
                return basic_text_search_chunks(user_id, query, filters, top_k)
            query_embedding = api_vectors[0]
        else:
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
        
        # Apply filters if provided. The tag, speaker, and date filters all need
        # a join to Recording; join it at most once so combining more than one of
        # them does not raise a duplicate-JOIN / ambiguous-relationship error.
        if filters:
            if (filters.get('tag_ids') or filters.get('speaker_names')
                    or filters.get('date_from') or filters.get('date_to')):
                chunks_query = chunks_query.join(Recording)

            if filters.get('tag_ids'):
                chunks_query = chunks_query.join(
                    RecordingTag, Recording.id == RecordingTag.recording_id
                ).filter(RecordingTag.tag_id.in_(filters['tag_ids']))

            if filters.get('speaker_names'):
                # Filter by the participants field on the recording.
                speaker_conditions = [
                    Recording.participants.ilike(f'%{speaker_name}%')
                    for speaker_name in filters['speaker_names']
                ]
                chunks_query = chunks_query.filter(db.or_(*speaker_conditions))
                current_app.logger.info(f"Applied speaker filter for: {filters['speaker_names']}")

            if filters.get('recording_ids'):
                chunks_query = chunks_query.filter(
                    TranscriptChunk.recording_id.in_(filters['recording_ids'])
                )

            if filters.get('date_from'):
                chunks_query = chunks_query.filter(Recording.meeting_date >= filters['date_from'])
            if filters.get('date_to'):
                chunks_query = chunks_query.filter(Recording.meeting_date <= filters['date_to'])

        # Get chunks that have embeddings
        chunks = chunks_query.filter(TranscriptChunk.embedding.isnot(None)).all()
        
        if not chunks:
            return []

        # Calculate similarities. Previously this iterated chunk-by-chunk and
        # called sklearn's cosine_similarity on a 1xN vs 1xN pair for every
        # chunk, which on a library of ~17k chunks took 15-20 seconds per
        # query because of Python-call overhead per chunk. Stacking all
        # chunk vectors into one matrix and doing a single vectorised dot
        # product brings that down by two to three orders of magnitude.
        expected_dim = int(query_embedding.shape[0])
        kept_chunks = []
        kept_vectors = []
        skipped_dim_mismatch = 0
        for chunk in chunks:
            if chunk.embedding is None:
                continue
            try:
                v = deserialize_embedding(chunk.embedding)
            except Exception as e:
                current_app.logger.warning(f"Error deserialising chunk {chunk.id}: {e}")
                continue
            if v is None:
                continue
            if v.shape[0] != expected_dim:
                # Stale vector from a previous embedding configuration.
                # Skip silently in batch rather than warning per chunk so a
                # large library cannot flood the log on every search.
                skipped_dim_mismatch += 1
                continue
            kept_chunks.append(chunk)
            kept_vectors.append(v)

        if skipped_dim_mismatch:
            current_app.logger.warning(
                f"Skipped {skipped_dim_mismatch} chunks with mismatched "
                f"embedding dimensions (expected {expected_dim}). Run "
                f"Re-embed all to refresh them."
            )

        if not kept_vectors:
            return []

        # One sklearn call instead of len(chunks). Returns shape (1, N).
        embeddings_matrix = np.vstack(kept_vectors)
        similarities = cosine_similarity(
            query_embedding.reshape(1, -1),
            embeddings_matrix,
        )[0]

        # Top-k via argpartition is faster than a full sort for large N.
        if top_k >= len(kept_chunks):
            order = np.argsort(-similarities)
        else:
            top_idx = np.argpartition(-similarities, top_k)[:top_k]
            order = top_idx[np.argsort(-similarities[top_idx])]

        return [(kept_chunks[i], float(similarities[i])) for i in order]
        
    except Exception as e:
        current_app.logger.error(f"Error in semantic search: {e}")
        return []

# --- Helper Functions for Document Processing ---



