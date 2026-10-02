"""Read a stored transcript as segments, whatever key set it was saved with.

Transcripts are stored as JSON lists of segments. Older connectors and edits
saved 'start'/'end'/'text' where newer ones save 'start_time'/'end_time'/
'sentence'. Every reader that needs segments goes through segments_of(), so
the key sets are handled in one place.
"""

import json


def _number(value):
    """Seconds as a float from a number, a numeric string or "[hh:]mm:ss[.f]"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and ':' in value:
        try:
            seconds = 0.0
            for part in value.strip().split(':'):
                seconds = seconds * 60 + float(part)
            return seconds
        except ValueError:
            return None
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


def canonical_segments(recording):
    """The documented API shape of a JSON transcript (mailr spec G12), or None
    for a plain-text transcript.

    Every segment has index, speaker, speaker_label, speaker_id, text,
    sentence, start_time and end_time. speaker is the linked saved speaker's
    current name when the segment is linked; speaker_label is the
    diarization label, recovered from the label map after a rename.
    """
    from src.services.speaker_links import (LABEL, _labels_by_name, _sample_speakers, owner_speakers,
                                            resolve_id)
    import json as _json
    segments = segments_of(recording.transcription)
    if segments is None:
        return None
    try:
        raw = [s for s in _json.loads(recording.transcription) if isinstance(s, dict)]
    except (TypeError, ValueError):
        raw = []
    by_name, by_id = owner_speakers(recording.user_id) if recording.user_id else ({}, {})
    samples = _sample_speakers(recording)
    labels_by_name = _labels_by_name(recording)
    label_of_speaker = {}
    for label, sid in samples.items():
        label_of_speaker.setdefault(sid, label)
    result = []
    for seg, stored in zip(segments, raw):
        name = seg['speaker']
        name_text = name.strip() if isinstance(name, str) else None
        stored_id = stored.get('speaker_id')
        # A valid stored link wins: the name in the segment may be an older
        # spelling of the same person.
        sid = stored_id if stored_id in by_id else resolve_id(name, None, by_name, by_id, samples, labels_by_name)
        if name_text and LABEL.match(name_text):
            label = name_text
        else:
            labels = labels_by_name.get((name_text or '').lower(), [])
            label = labels[0] if labels else label_of_speaker.get(sid)
        result.append({
            'index': seg['index'],
            'speaker': by_id[sid].name if sid in by_id else name,
            'speaker_label': label,
            'speaker_id': sid,
            'text': seg['text'],
            'sentence': seg['text'],
            'start_time': seg['start_time'],
            'end_time': seg['end_time'],
        })
    return result


def window(segments, start=None, end=None, max_segments=None):
    """Segments overlapping [start, end] seconds, at most max_segments.
    Returns (segments, next_start or None)."""
    picked = []
    for seg in segments:
        s_time = seg['start_time']
        e_time = seg['end_time'] if seg['end_time'] is not None else s_time
        if start is not None and (e_time is None or e_time < start):
            continue
        if end is not None and (s_time is None or s_time > end):
            continue
        picked.append(seg)
    if max_segments is not None and len(picked) > max_segments:
        following = picked[max_segments]
        return picked[:max_segments], following['start_time']
    return picked, None
