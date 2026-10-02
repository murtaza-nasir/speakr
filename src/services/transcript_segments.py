"""Read a stored transcript as segments, whatever key set it was saved with.

Transcripts are stored as JSON lists of segments. Older connectors and edits
saved 'start'/'end'/'text' where newer ones save 'start_time'/'end_time'/
'sentence'. Every reader that needs segments goes through segments_of(), so
the key sets are handled in one place.
"""

import json


def _number(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def segments_of(transcription):
    """[{index, speaker, text, start_time, end_time}] for a JSON transcript,
    None for a plain-text transcript or no transcript."""
    if not transcription:
        return None
    try:
        data = json.loads(transcription)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    segments = []
    for index, seg in enumerate(data):
        if not isinstance(seg, dict):
            continue
        text = seg.get('sentence')
        if text is None:
            text = seg.get('text', '')
        segments.append({
            'index': index,
            'speaker': seg.get('speaker') or None,
            'text': str(text or ''),
            'start_time': _number(seg.get('start_time', seg.get('start'))),
            'end_time': _number(seg.get('end_time', seg.get('end'))),
        })
    return segments
