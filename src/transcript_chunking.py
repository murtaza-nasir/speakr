"""
Transcript chunking helpers.

Segment-aware (and legacy character-window) chunking for diarized and
plain-text transcripts. Moved out of embeddings so chunking ownership is
clear and testable independently of the embedding pipeline.
"""

import os
import json


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


