"""Keyword and semantic search over recordings for API v1 (mailr spec G3).

Keyword mode: the query splits into terms (a quoted phrase stays one term).
A recording is a candidate when every term appears in one of the selected
fields (ILIKE, on SQLite and PostgreSQL); at most MAX_CANDIDATES candidates,
newest first, are read. Each field then gives hits: the title and the
participants as a whole, notes and summary per line, the transcript per
segment. The access filter runs first, so a recording the caller cannot read
is never a candidate.
"""

import re
from datetime import datetime

from sqlalchemy import func, or_

from src.database import db

FIELDS = ('title', 'participants', 'notes', 'summary', 'transcript')
WEIGHTS = {'title': 1.0, 'summary': 0.9, 'notes': 0.8, 'participants': 0.8, 'transcript': 0.7}
MAX_CANDIDATES = 200
TEXT_LIMIT = 400

_TERM = re.compile(r'"([^"]+)"|(\S+)')


class SearchError(ValueError):
    def __init__(self, message, code='invalid_parameter', status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def parse_terms(query):
    terms = []
    for phrase, word in _TERM.findall(query or ''):
        term = (phrase or word).strip()
        if term and term.lower() not in (t.lower() for t in terms):
            terms.append(term)
    return terms


def _like(term):
    escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def _spans(text, terms):
    lower = text.lower()
    spans = []
    for term in terms:
        needle = term.lower()
        start = lower.find(needle)
        while start != -1:
            spans.append([start, start + len(needle)])
            start = lower.find(needle, start + 1)
    return sorted(spans)


def _cut(text, spans):
    """At most TEXT_LIMIT characters around the first match; spans follow."""
    if len(text) <= TEXT_LIMIT:
        return text, spans
    first = spans[0][0] if spans else 0
    start = max(0, min(first - TEXT_LIMIT // 4, len(text) - TEXT_LIMIT))
    end = start + TEXT_LIMIT
    piece = text[start:end]
    prefix = '…' if start > 0 else ''
    suffix = '…' if end < len(text) else ''
    shifted = [[a - start + len(prefix), b - start + len(prefix)] for a, b in spans if a >= start and b <= end]
    return prefix + piece + suffix, shifted


def _lines(text):
    for line in (text or '').splitlines():
        if line.strip():
            yield line.strip()


def _hit(recording, field, text, terms, when, notes_for, segment=None):
    spans = _spans(text, terms)
    if not spans:
        return None
    matched = {t.lower() for t in terms if t.lower() in text.lower()}
    cut, cut_spans = _cut(text, spans)
    return {
        'recording_id': recording.id,
        'title': recording.title,
        'meeting_date': _z(recording.meeting_date),
        'field': field,
        'segment_index': segment['index'] if segment else None,
        'start_time': segment['start_time'] if segment else None,
        'end_time': segment['end_time'] if segment else None,
        'speaker': segment['speaker'] if segment else None,
        'text': cut,
        'match_spans': cut_spans,
        'score': round(len(matched) / len(terms) * WEIGHTS[field], 4),
        '_when': when,
        '_terms': matched,
    }


def _z(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ') if value else None


def _parse_date(value, end_of_day=False):
    from datetime import timedelta
    from src.utils.dates import to_utc_naive
    try:
        parsed = to_utc_naive(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except (AttributeError, ValueError):
        raise SearchError(f'Invalid date: {value!r}')
    if end_of_day and len(value.strip()) == 10:
        return parsed + timedelta(days=1), True
    return parsed, False


def candidate_query(user, terms, fields, recording_ids=None, tag_id=None, folder_id=None,
                    date_from=None, date_to=None, date_field='meeting_date'):
    from src.models import Recording, RecordingTag
    query = Recording.query.filter(Recording.user_id == user.id)       # scope=own (G4 adds shared)
    if recording_ids:
        query = query.filter(Recording.id.in_(recording_ids))
    if tag_id:
        query = query.filter(Recording.id.in_(
            db.session.query(RecordingTag.recording_id).filter(RecordingTag.tag_id == tag_id)))
    if folder_id is not None:
        query = query.filter(Recording.folder_id.is_(None) if folder_id == 'none'
                             else Recording.folder_id == folder_id)
    when = (func.coalesce(Recording.meeting_date, Recording.created_at) if date_field == 'meeting_date'
            else Recording.created_at)
    if date_from:
        start, _ = _parse_date(date_from)
        query = query.filter(when >= start)
    if date_to:
        end, exclusive = _parse_date(date_to, end_of_day=True)
        query = query.filter(when < end if exclusive else when <= end)
    columns = {'title': Recording.title, 'participants': Recording.participants, 'notes': Recording.notes,
               'summary': Recording.summary, 'transcript': Recording.transcription}
    for term in terms:
        pattern = _like(term)
        query = query.filter(or_(*[columns[f].ilike(pattern, escape='\\') for f in fields]))
    return query.order_by(when.desc(), Recording.id.desc()), when


def keyword_search(user, query_text, fields=FIELDS, speaker=None, limit=20, page=1, **filters):
    from src.services.transcript_segments import segments_of
    terms = parse_terms(query_text)
    if not terms:
        raise SearchError('q must contain a word')
    if speaker:
        fields = ('transcript',)
    candidates, _ = candidate_query(user, terms, fields, **filters)
    hits = []
    for recording in candidates.limit(MAX_CANDIDATES).all():
        when = recording.meeting_date or recording.created_at or datetime.min
        found = []
        if 'title' in fields and recording.title:
            found.append(_hit(recording, 'title', recording.title, terms, when, None))
        if 'participants' in fields and recording.participants:
            found.append(_hit(recording, 'participants', recording.participants, terms, when, None))
        if 'notes' in fields:
            for line in _lines(recording.get_user_notes(user)):
                found.append(_hit(recording, 'notes', line, terms, when, None))
        if 'summary' in fields:
            for line in _lines(recording.summary):
                found.append(_hit(recording, 'summary', line, terms, when, None))
        if 'transcript' in fields and recording.transcription:
            segments = segments_of(recording.transcription)
            if segments is None:
                if not speaker:
                    for line in _lines(recording.transcription):
                        found.append(_hit(recording, 'transcript', line, terms, when, None))
            else:
                for seg in segments:
                    if speaker and (seg['speaker'] or '').strip().lower() != speaker.strip().lower():
                        continue
                    hit = _hit(recording, 'transcript', seg['text'], terms, when, None, segment=seg)
                    found.append(hit)
        found = [h for h in found if h]
        # Every term must appear somewhere in this recording's real text (the
        # candidate filter also matched JSON keys of the stored transcript).
        covered = set().union(*(h['_terms'] for h in found)) if found else set()
        if covered >= {t.lower() for t in terms}:
            hits.extend(found)
    hits.sort(key=lambda h: (-h['score'], -h['_when'].timestamp() if h['_when'] != datetime.min else 0,
                             -h['recording_id'], h['segment_index'] if h['segment_index'] is not None else -1))
    start = (page - 1) * limit
    page_hits = hits[start:start + limit]
    for h in page_hits:
        h.pop('_when', None)
        h.pop('_terms', None)
    return page_hits, len(hits) > start + limit


def semantic_available():
    from src.services import embeddings as emb
    import os
    inquire = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
    return inquire and bool(emb.EMBEDDINGS_AVAILABLE)


def semantic_search(user, query_text, limit=20, recording_ids=None, tag_id=None, date_from=None,
                    date_to=None, speaker=None, **_ignored):
    from src.models import Recording
    from src.services.embeddings import semantic_search_chunks
    own = [rid for (rid,) in db.session.query(Recording.id).filter(Recording.user_id == user.id)]
    if recording_ids:
        own = [rid for rid in own if rid in set(recording_ids)]
    if not own:
        return []
    chunk_filters = {'recording_ids': own}
    if tag_id:
        chunk_filters['tag_ids'] = [tag_id]
    if date_from:
        chunk_filters['date_from'] = date_from[:10]
    if date_to:
        chunk_filters['date_to'] = date_to[:10]
    results = semantic_search_chunks(user.id, query_text, chunk_filters, top_k=limit * 3 if speaker else limit)
    hits = []
    for chunk, similarity in results:
        if speaker and (chunk.speaker_name or '').strip().lower() != speaker.strip().lower():
            continue
        rec = chunk.recording
        hits.append({
            'recording_id': chunk.recording_id,
            'title': rec.title if rec else None,
            'meeting_date': _z(rec.meeting_date) if rec else None,
            'field': 'transcript',
            'segment_index': None,
            'start_time': chunk.start_time,
            'end_time': chunk.end_time,
            'speaker': chunk.speaker_name,
            'text': chunk.content[:TEXT_LIMIT],
            'match_spans': [],
            'score': round(float(similarity), 4),
        })
        if len(hits) >= limit:
            break
    return hits
