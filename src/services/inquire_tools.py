"""Inquire agent tools: the implementations and the toolbox assembly.

Every tool is scoped to the requesting user's ACLs AND the UI filter set (a
tool can narrow within the filters, never escape them). Content availability
is structural: a disabled content type's tool is not in the schema at all, and
notes are only ever the user's OWN notes. The agent loop that offers and calls
these tools lives in src/services/inquire_agent.py.
"""

import json
import re
from datetime import datetime

from src.database import db
from src.models import Recording
from src.services.embeddings import basic_text_search_chunks, semantic_search_chunks
from src.utils.search import LIKE_ESCAPE, contains_pattern, transcript_contains


def _truncate_to_tokens(text, cap_tokens, pagination_hint=None):
    """Cap tool output; tell the model what was cut and how to get more."""
    cap_chars = cap_tokens * 4
    if len(text) <= cap_chars:
        return text
    marker = f"\n\n[TRUNCATED: output exceeded {cap_tokens} tokens."
    if pagination_hint:
        marker += f" {pagination_hint}"
    marker += "]"
    return text[:cap_chars] + marker


def _recording_brief(rec, zone=None):
    d = {
        'recording_id': rec.id,
        'title': rec.title or 'Untitled Recording',
        'has_transcript': bool(rec.transcription),
        'has_summary': bool(rec.summary),
        'has_notes': bool(rec.notes),
    }
    if rec.meeting_date:
        from src.utils.timezones import to_local, user_timezone
        d['meeting_date'] = to_local(rec.meeting_date, zone or user_timezone(rec.owner)).strftime('%Y-%m-%d %H:%M')
    if rec.participants:
        d['participants'] = rec.participants
    if rec.audio_duration_seconds:
        d['duration_seconds'] = int(rec.audio_duration_seconds)
    return d


def _parse_transcript_segments(transcription):
    """Return (segments, is_json). Segment: {speaker, text, start_time}."""
    if not transcription:
        return [], False
    try:
        data = json.loads(transcription)
        if isinstance(data, list):
            segments = []
            for seg in data:
                if isinstance(seg, dict):
                    segments.append({
                        'speaker': seg.get('speaker'),
                        'text': seg.get('sentence') or seg.get('text') or '',
                        'start_time': seg.get('start_time'),
                    })
            return segments, True
    except (ValueError, TypeError):
        pass
    return [], False


def _fmt_time(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


class ToolContext:
    """Everything a tool needs, resolved once per request."""

    def __init__(self, user_id, filters, allowed_ids, availability, marked_ids=None):
        self.user_id = user_id
        self.filters = filters or {}
        self.allowed_ids = allowed_ids
        self.availability = availability
        # Recordings whose transcript pages carry segment markers ([S12 12:34
        # Dana]): the recording open in the panel, so pages read past the
        # prompt's length limit can be cited like the prompt's own lines.
        self.marked_ids = set(marked_ids or ())
        self._labels = None

    @property
    def labels(self):
        """The visible tags and folders of the recordings in scope, read once."""
        if self._labels is None:
            from src.services.library_labels import LibraryLabels
            self._labels = LibraryLabels(self.user_id, self.allowed_ids)
        return self._labels

    def check_scope(self, recording_id):
        try:
            recording_id = int(recording_id)
        except (TypeError, ValueError):
            return None, "recording_id must be an integer"
        if recording_id not in self.allowed_ids:
            return None, (f"Recording {recording_id} is not available "
                          "(it does not exist, you lack access, or it is outside the active filters)")
        rec = db.session.get(Recording, recording_id)
        if not rec:
            return None, f"Recording {recording_id} not found"
        return rec, None


def tool_search_transcripts(ctx, args):
    query = (args.get('query') or '').strip()
    if not query:
        return {'error': "query is required"}
    top_k = min(max(int(args.get('top_k', 8) or 8), 1), 40)
    mode = str(args.get('mode', 'auto') or 'auto').lower()
    if mode not in ('auto', 'semantic', 'keyword'):
        mode = 'auto'

    # Narrow within the UI filters: tool args can only tighten, never widen.
    filters = dict(ctx.filters)
    filters['recording_ids'] = list(ctx.allowed_ids)
    narrowed, notes = _label_filter(ctx, args)
    if narrowed is not None:
        if not narrowed:
            return {'query': query, 'search_type': 'none', 'result_count': 0, 'results': [],
                    'note': ' '.join(notes) or 'No recording in scope matches these tags and folder.'}
        filters['recording_ids'] = sorted(narrowed)
    from src.utils.speaker_labels import real_speaker_names
    speakers = real_speaker_names(_name_list(args.get('speakers')))
    if speakers:
        filters['speaker_names'] = speakers
    for key, arg in (('date_from', 'date_from'), ('date_to', 'date_to')):
        if args.get(arg):
            try:
                parsed = datetime.fromisoformat(str(args[arg])).date()
                base = filters.get(key)
                # intersect with the UI date range
                if key == 'date_from':
                    filters[key] = max(base, parsed) if base else parsed
                else:
                    filters[key] = min(base, parsed) if base else parsed
            except ValueError:
                return {'error': f"{arg} must be an ISO date (YYYY-MM-DD)"}

    from src.utils.timezones import to_local, user_timezone_by_id
    _zone = user_timezone_by_id(ctx.user_id)
    if mode == 'keyword':
        results = basic_text_search_chunks(ctx.user_id, query, filters, top_k)
        search_type = 'keyword'
    else:
        results = semantic_search_chunks(ctx.user_id, query, filters, top_k)
        search_type = 'semantic'
        # Semantic misses are not proof of absence: exact terms, acronyms and
        # rare tokens ("MR rate") can rank poorly in embedding space (and the
        # embedding API may be degraded). In auto mode, back an empty or thin
        # semantic result with a literal keyword pass over the same scope.
        if mode == 'auto' and len(results) < 2:
            keyword_results = basic_text_search_chunks(ctx.user_id, query, filters, top_k)
            if keyword_results:
                seen = {c.id for c, _s in results if c}
                results = list(results) + [(c, s) for c, s in keyword_results if c and c.id not in seen]
                search_type = 'semantic+keyword'

    hits = []
    for chunk, similarity in results[:top_k]:
        if not chunk or not chunk.recording or chunk.recording_id not in ctx.allowed_ids:
            continue
        hits.append({
            'recording_id': chunk.recording_id,
            'title': chunk.recording.title or 'Untitled Recording',
            'meeting_date': to_local(chunk.recording.meeting_date, _zone).strftime('%Y-%m-%d') if chunk.recording.meeting_date else None,
            'speaker': chunk.speaker_name,
            'time': _fmt_time(chunk.start_time),
            'seek_seconds': int(chunk.start_time) if chunk.start_time is not None else None,
            'chunk_index': chunk.chunk_index,
            'snippet': chunk.content,
            'similarity': round(float(similarity), 3),
            **_label_fields(ctx, chunk.recording_id),
        })
    out = {'query': query, 'search_type': search_type, 'result_count': len(hits), 'results': hits}
    if notes:
        out['note'] = ' '.join(notes)
    return out


def tool_list_recordings(ctx, args):
    q = Recording.query.filter(Recording.id.in_(ctx.allowed_ids))
    names = _name_list(args.get('participants')) or _name_list(args.get('participant'))
    if names:
        q = q.filter(db.or_(*[Recording.participants.ilike(contains_pattern(n), escape=LIKE_ESCAPE)
                              for n in names]))
    narrowed, notes = _label_filter(ctx, args)
    if narrowed is not None:
        q = q.filter(Recording.id.in_(sorted(narrowed)))
    from src.utils.timezones import local_date_bounds, user_timezone_by_id
    _zone = user_timezone_by_id(ctx.user_id)
    _bounds = {}
    for arg in ('date_from', 'date_to'):
        if args.get(arg):
            try:
                _bounds[arg] = datetime.fromisoformat(str(args[arg])).date()
            except ValueError:
                return {'error': f"{arg} must be an ISO date (YYYY-MM-DD)"}
    # Local calendar days of the user, as UTC bounds (#412).
    _start, _end = local_date_bounds(_bounds.get('date_from'), _bounds.get('date_to'), _zone)
    if _start is not None:
        q = q.filter(Recording.meeting_date >= _start)
    if _end is not None:
        q = q.filter(Recording.meeting_date < _end)
    sort = args.get('sort', 'newest')
    q = q.order_by(Recording.meeting_date.asc() if sort == 'oldest' else Recording.meeting_date.desc())
    limit = min(max(int(args.get('limit', 25) or 25), 1), 100)
    total = q.count()
    recs = q.limit(limit).all()
    out = {
        'total_in_scope': total,
        'returned': len(recs),
        'recordings': [{**_recording_brief(r, _zone), **_label_fields(ctx, r.id)} for r in recs],
    }
    if notes:
        out['note'] = ' '.join(notes)
    return out


DEFAULT_TRANSCRIPT_PAGE = 150


def tool_get_transcript(ctx, args):
    rec, err = ctx.check_scope(args.get('recording_id'))
    if err:
        return {'error': err}
    if not rec.transcription:
        return {'error': f"Recording {rec.id} has no transcript"}

    offset = max(int(args.get('offset', 0) or 0), 0)
    limit = min(max(int(args.get('limit', DEFAULT_TRANSCRIPT_PAGE) or DEFAULT_TRANSCRIPT_PAGE), 1), 400)

    if rec.id in ctx.marked_ids:
        from src.services.transcript_segments import canonical_segments
        from src.services.recording_chat import _marked_transcript
        marked = canonical_segments(rec)
        if marked:
            window = marked[offset:offset + limit]
            return {
                'recording_id': rec.id,
                'title': rec.title,
                'total_segments': len(marked),
                'offset': offset,
                'returned_segments': len(window),
                'next_offset': offset + limit if offset + limit < len(marked) else None,
                'transcript': _marked_transcript(window),
            }

    segments, is_json = _parse_transcript_segments(rec.transcription)
    if is_json:
        window = segments[offset:offset + limit]
        lines = []
        for seg in window:
            prefix = f"{seg['speaker']}: " if seg.get('speaker') else ''
            t = _fmt_time(seg.get('start_time'))
            suffix = f" [{t}]" if t else ''
            lines.append(f"{prefix}{seg['text']}{suffix}")
        return {
            'recording_id': rec.id,
            'title': rec.title,
            'total_segments': len(segments),
            'offset': offset,
            'returned_segments': len(window),
            'next_offset': offset + limit if offset + limit < len(segments) else None,
            'transcript': "\n".join(lines),
        }

    # Plain-text transcript: character-window pagination.
    page_chars = limit * 200
    text = rec.transcription
    window = text[offset:offset + page_chars]
    return {
        'recording_id': rec.id,
        'title': rec.title,
        'total_chars': len(text),
        'offset': offset,
        'next_offset': offset + page_chars if offset + page_chars < len(text) else None,
        'transcript': window,
        'note': 'Plain-text transcript; offset/limit are character-based here (limit unit = 200 chars).',
    }


def tool_get_summary(ctx, args):
    rec, err = ctx.check_scope(args.get('recording_id'))
    if err:
        return {'error': err}
    if not rec.summary:
        return {'error': f"Recording {rec.id} has no summary"}
    return {'recording_id': rec.id, 'title': rec.title, 'summary': rec.summary}


def tool_get_notes(ctx, args):
    rec, err = ctx.check_scope(args.get('recording_id'))
    if err:
        return {'error': err}
    # Decision (2026-08-28): only the user's OWN notes, never notes on
    # recordings shared with them, regardless of toggles.
    if rec.user_id != ctx.user_id:
        return {'error': "Notes are only available on your own recordings"}
    if not rec.notes:
        return {'error': f"Recording {rec.id} has no notes"}
    return {'recording_id': rec.id, 'title': rec.title, 'notes': rec.notes}


def tool_get_recording_metadata(ctx, args):
    rec, err = ctx.check_scope(args.get('recording_id'))
    if err:
        return {'error': err}
    from src.utils.timezones import user_timezone_by_id
    info = _recording_brief(rec, user_timezone_by_id(ctx.user_id))
    # Only the tags and folder this user can see.
    info.update(_label_fields(ctx, rec.id))
    return info


# ---------------------------------------------------------------------------
# Tags and folders (Ambrose search)
# ---------------------------------------------------------------------------

def _label_fields(ctx, recording_id):
    """The tags and folder path of a recording as the user sees them; keys
    left out when empty."""
    out = {}
    tags = ctx.labels.tags_of(recording_id)
    if tags:
        out['tags'] = tags
    folder = ctx.labels.folder_path_of(recording_id)
    if folder:
        out['folder'] = folder
    return out


def _label_filter(ctx, args):
    """(recording ids in scope with any of `tags` and inside `folder`, or
    None when neither is given; notes for names that match nothing)."""
    ids, notes = None, []
    tags = args.get('tags')
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(tags, list) and any(str(t).strip() for t in tags):
        hits, unknown = ctx.labels.match_tags([str(t) for t in tags][:20])
        ids = hits
        if unknown:
            notes.append(f"No recording in scope has the tag {', '.join(unknown)}; "
                         "use the exact tag names listed in the instructions.")
    folder = args.get('folder')
    if isinstance(folder, str) and folder.strip():
        hits = ctx.labels.match_folder(folder)
        if not hits:
            notes.append(f"No recording in scope is in a folder \"{folder.strip()}\"; "
                         "use a folder path listed in the instructions.")
        ids = hits if ids is None else ids & hits
    if ids is not None:
        ids = ids & set(ctx.allowed_ids)
    return ids, notes


def _parse_dates(args):
    """({'date_from': date, 'date_to': date}, error)."""
    bounds = {}
    for arg in ('date_from', 'date_to'):
        if args.get(arg):
            try:
                bounds[arg] = datetime.fromisoformat(str(args[arg])).date()
            except ValueError:
                return None, f"{arg} must be an ISO date (YYYY-MM-DD)"
    return bounds, None


# ---------------------------------------------------------------------------
# Literal text search (Ambrose search)
# ---------------------------------------------------------------------------

TEXT_FIELDS = ('transcript', 'title', 'summary', 'notes')
_SNIPPET_CHARS = 120


def _snippet(text, terms):
    """About 240 characters of `text` around the first term found."""
    text = ' '.join((text or '').split())
    lower = text.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) != -1]
    at = min(positions) if positions else 0
    start = max(at - _SNIPPET_CHARS, 0)
    end = min(at + _SNIPPET_CHARS + max((len(t) for t in terms), default=0), len(text))
    out = text[start:end]
    return ('…' if start else '') + out + ('…' if end < len(text) else '')


def _line_matches(line, terms):
    lower = line.lower()
    return all(t in lower for t in terms)


# Transcript matches search_text returns for one recording (#679): every
# match is its own segment (time, speaker, citation), so without a cap one
# long recording that says the phrase often fills top_k by itself.
SEARCH_TEXT_PER_RECORDING = 3


def tool_search_text(ctx, args):
    from sqlalchemy import and_
    from sqlalchemy.orm import joinedload
    from src.models import TranscriptChunk
    from src.transcript_chunking import _segment_line, build_transcript_segments

    query = ' '.join(str(args.get('query') or '').split())[:200]
    if not query:
        return {'error': "query is required"}
    match = str(args.get('match') or 'phrase').lower()
    if match not in ('phrase', 'all_words'):
        match = 'phrase'
    terms = [w.lower() for w in query.split()][:12] if match == 'all_words' else [query.lower()]

    available = ['transcript', 'title']
    if ctx.availability.get('summaries'):
        available.append('summary')
    if ctx.availability.get('notes'):
        available.append('notes')
    notes = []
    asked = args.get('fields')
    if isinstance(asked, str):
        asked = [asked]
    if isinstance(asked, list) and asked:
        asked = {str(f).strip().lower() for f in asked}
        fields = [f for f in available if f in asked]
        skipped = sorted(asked - set(available))
        if skipped:
            notes.append(f"Not searched (not available): {', '.join(skipped)}")
        if not fields:
            return {'error': f"none of these fields can be searched; available: {', '.join(available)}"}
    else:
        fields = available
    try:
        top_k = min(max(int(args.get('top_k', 20) or 20), 1), 50)
    except (TypeError, ValueError):
        top_k = 20

    ids = set(ctx.allowed_ids)
    narrowed, label_notes = _label_filter(ctx, args)
    notes += label_notes
    if narrowed is not None:
        ids &= narrowed
    bounds, err = _parse_dates(args)
    if err:
        return {'error': err}
    q = db.session.query(Recording.id).filter(Recording.id.in_(ids)) if ids else None
    from src.utils.speaker_labels import real_speaker_names
    speakers = real_speaker_names(_name_list(args.get('speakers')))
    if q is not None and speakers:
        q = q.filter(db.or_(*[Recording.participants.ilike(contains_pattern(s), escape=LIKE_ESCAPE)
                              for s in speakers]))
    from src.utils.timezones import local_date_bounds, to_local, user_timezone_by_id
    zone = user_timezone_by_id(ctx.user_id)
    start, end = local_date_bounds(bounds.get('date_from'), bounds.get('date_to'), zone)
    if q is not None and start is not None:
        q = q.filter(Recording.meeting_date >= start)
    if q is not None and end is not None:
        q = q.filter(Recording.meeting_date < end)
    ids = {rid for (rid,) in q} if q is not None else set()

    result = {'query': query, 'match': match, 'fields': fields}
    if notes:
        result['note'] = ' '.join(notes)
    if not ids:
        return {**result, 'result_count': 0, 'recording_count': 0, 'results': []}

    def matches(column):
        return and_(*[column.ilike(contains_pattern(t), escape=LIKE_ESCAPE) for t in terms])

    def base(rec):
        hit = {'recording_id': rec.id, 'title': rec.title or 'Untitled Recording',
               'meeting_date': to_local(rec.meeting_date, zone).strftime('%Y-%m-%d') if rec.meeting_date else None}
        hit.update(_label_fields(ctx, rec.id))
        return hit

    hits = []
    for field, column, extra in (('title', Recording.title, None),
                                 ('summary', Recording.summary, None),
                                 # Only the user's own notes (get_notes rule).
                                 ('notes', Recording.notes, Recording.user_id == ctx.user_id)):
        if field not in fields:
            continue
        rq = Recording.query.filter(Recording.id.in_(ids), matches(column))
        if extra is not None:
            rq = rq.filter(extra)
        for rec in rq.order_by(Recording.meeting_date.desc()).limit(top_k).all():
            hits.append({**base(rec), 'field': field, 'snippet': _snippet(getattr(rec, field), terms)})

    cap = SEARCH_TEXT_PER_RECORDING
    shown, more = {}, set()

    def take(rid):
        """Whether one more transcript match of this recording fits the cap."""
        if shown.get(rid, 0) >= cap:
            more.add(rid)
            return False
        shown[rid] = shown.get(rid, 0) + 1
        return True

    if 'transcript' in fields:
        # The first matching chunks of each recording only (a chunk overlaps
        # its neighbor, so two chunks can give one segment), so one recording
        # cannot fill the rows read for all of them.
        ranked = (db.session.query(
                      TranscriptChunk.id.label('chunk_id'),
                      db.func.row_number().over(partition_by=TranscriptChunk.recording_id,
                                                order_by=TranscriptChunk.chunk_index).label('n'))
                  .filter(TranscriptChunk.recording_id.in_(ids), matches(TranscriptChunk.content))
                  .subquery())
        chunks = (TranscriptChunk.query.options(joinedload(TranscriptChunk.recording))
                  .join(ranked, ranked.c.chunk_id == TranscriptChunk.id)
                  .join(Recording, Recording.id == TranscriptChunk.recording_id)
                  .filter(ranked.c.n <= cap * 2 + 1)
                  .order_by(Recording.meeting_date.desc(), TranscriptChunk.recording_id, TranscriptChunk.chunk_index)
                  .limit(top_k * 8).all())
        segments_by_rec, seen = {}, set()
        for chunk in chunks:
            lines = [line for line in chunk.content.split('\n') if _line_matches(line, terms)]
            line = lines[0] if lines else chunk.content
            speaker, start_time = chunk.speaker_name, chunk.start_time
            if lines:
                rid = chunk.recording_id
                if rid not in segments_by_rec:
                    segments_by_rec[rid] = build_transcript_segments(chunk.recording.transcription) or []
                for seg in segments_by_rec[rid]:
                    if _segment_line(seg) != line:
                        continue
                    if chunk.start_time is not None and seg['start'] is not None and not (
                            chunk.start_time - 0.01 <= seg['start'] <= (chunk.end_time or seg['start']) + 0.01):
                        continue
                    speaker, start_time = seg['speaker'], seg['start']
                    if speaker and line.startswith(f"{speaker}: "):
                        line = line[len(speaker) + 2:]
                    break
            key = (chunk.recording_id, start_time, line[:80])
            if key in seen:
                continue  # the segment two chunks share
            seen.add(key)
            if not take(chunk.recording_id):
                continue
            hits.append({**base(chunk.recording), 'field': 'transcript',
                         'speaker': speaker, 'time': _fmt_time(start_time),
                         'seek_seconds': int(start_time) if start_time is not None else None,
                         'snippet': _snippet(line, terms)})

        # Recordings with no indexed chunks (indexing failed or has not run):
        # their stored transcript, read segment by segment.
        indexed = {rid for (rid,) in db.session.query(TranscriptChunk.recording_id)
                   .filter(TranscriptChunk.recording_id.in_(ids)).distinct()}
        unindexed = ids - indexed
        if unindexed:
            rq = Recording.query.filter(Recording.id.in_(unindexed), Recording.transcription.isnot(None))
            # SQL folds case only for ASCII letters, so a term with a cased
            # letter outside ASCII (é, д) is matched below in Python alone.
            exact = [t for t in terms if all(c.isascii() or c.upper() == c for c in t)]
            if exact:
                rq = rq.filter(and_(*[transcript_contains(Recording.transcription, t) for t in exact]))
            from src.services.transcript_segments import segments_of
            for rec in rq.order_by(Recording.meeting_date.desc()).limit(top_k * 4).all():
                segments = segments_of(rec.transcription)
                if segments is None:
                    segments = [{'speaker': None, 'text': rec.transcription, 'start_time': None}]
                for seg in segments:
                    if _line_matches(seg['text'], terms) and take(rec.id):
                        hits.append({**base(rec), 'field': 'transcript', 'speaker': seg['speaker'],
                                     'time': _fmt_time(seg['start_time']),
                                     'seek_seconds': int(seg['start_time']) if seg['start_time'] is not None
                                     else None,
                                     'snippet': _snippet(seg['text'], terms)})

    truncated = len(hits) > top_k
    hits = hits[:top_k]
    returned = {hit['recording_id'] for hit in hits}
    result.update({'result_count': len(hits), 'recording_count': len(returned), 'results': hits})
    if truncated:
        result['truncated'] = True
    capped = sorted(rid for rid in more if rid in returned)
    if capped:
        result['more_matches_in'] = capped
        result['note'] = ' '.join(filter(None, [
            result.get('note'),
            f"At most {cap} transcript matches per recording are listed; the recordings in more_matches_in "
            f"have more (read their transcripts for all of them)."]))
    return result


# ---------------------------------------------------------------------------
# Similar recordings (Ambrose search)
# ---------------------------------------------------------------------------

SIMILARITY_WEIGHTS = {'speakers': 0.3, 'tags': 0.2, 'folder': 0.15, 'date': 0.1, 'content': 0.25}
_STOP_WORDS = frozenset(
    'a an the is are was were be been being have has had do does did will would could should may might '
    'can to of in for on with at by from up about into and but or not so it its this that these those '
    'what which who how when where why i me my we our you your he she his her they them their um uh yeah '
    'okay ok like just know think really right well also then there here one going get got'.split())


def _speaker_names(participants):
    """{lower-case name: name} of the people in a participants field, roles
    in brackets left out ("Dana (finance)" is Dana)."""
    from src.services.library_labels import is_generic_speaker
    names = {}
    for part in re.split(r'[,;\n]', participants or ''):
        name = re.sub(r'\(.*?\)', '', part).strip()
        if name and not is_generic_speaker(name):
            names.setdefault(name.lower(), name)
    return names


def _content_scores(source_id, candidate_ids):
    """({candidate id: similarity 0..1}, method). Embeddings: cosine of the
    recordings' average chunk vectors. Without them: word overlap (Jaccard)
    of the first chunks. ({}, None) when neither is possible."""
    import numpy as np
    from src.models import TranscriptChunk
    ids = list(candidate_ids) + [source_id]
    vectors = {}
    for rid, blob in db.session.query(TranscriptChunk.recording_id, TranscriptChunk.embedding) \
            .filter(TranscriptChunk.recording_id.in_(ids), TranscriptChunk.embedding.isnot(None)):
        # Stored vectors are float32 bytes; reading them needs numpy only,
        # not the embedding model.
        try:
            v = np.frombuffer(blob, dtype=np.float32) if blob else None
        except Exception:
            v = None
        if v is not None and v.size:
            vectors.setdefault(rid, []).append(v)
    if vectors.get(source_id):
        dim = vectors[source_id][0].shape[0]

        def mean(rid):
            same = [v for v in vectors.get(rid, ()) if v.shape[0] == dim]
            if not same:
                return None
            m = np.mean(np.vstack(same), axis=0)
            n = np.linalg.norm(m)
            return m / n if n else None

        src = mean(source_id)
        if src is not None:
            scores = {}
            for rid in candidate_ids:
                m = mean(rid)
                if m is not None:
                    scores[rid] = max(0.0, min(1.0, float(np.dot(src, m))))
            return scores, 'embeddings'

    words = {}
    for rid, content in db.session.query(TranscriptChunk.recording_id, TranscriptChunk.content) \
            .filter(TranscriptChunk.recording_id.in_(ids), TranscriptChunk.chunk_index < 3):
        tokens = {w for w in re.findall(r"[^\W\d_]{3,}", (content or '').lower()) if w not in _STOP_WORDS}
        words.setdefault(rid, set()).update(tokens)
    src = words.get(source_id)
    if not src:
        return {}, None
    scores = {}
    for rid in candidate_ids:
        other = words.get(rid)
        if other:
            scores[rid] = len(src & other) / len(src | other)
    return scores, 'words'


def tool_find_similar_recordings(ctx, args):
    from src.models import TranscriptChunk
    from src.services.library_labels import is_generic_speaker
    source, err = ctx.check_scope(args.get('recording_id'))
    if err:
        return {'error': err}
    try:
        limit = min(max(int(args.get('limit', 8) or 8), 1), 25)
    except (TypeError, ValueError):
        limit = 8
    candidates = set(ctx.allowed_ids) - {source.id}
    result = {'recording_id': source.id, 'title': source.title or 'Untitled Recording',
              'weights': SIMILARITY_WEIGHTS}
    if not candidates:
        return {**result, 'result_count': 0, 'results': []}

    recs = {r.id: r for r in Recording.query.filter(Recording.id.in_(candidates | {source.id})).all()}
    speakers = {rid: _speaker_names(r.participants) for rid, r in recs.items()}
    for rid, name in db.session.query(TranscriptChunk.recording_id, TranscriptChunk.speaker_name) \
            .filter(TranscriptChunk.recording_id.in_(list(recs)), TranscriptChunk.speaker_name.isnot(None)) \
            .distinct():
        if name and not is_generic_speaker(name):
            speakers.setdefault(rid, {}).setdefault(name.strip().lower(), name.strip())
    content, method = _content_scores(source.id, candidates)
    labels = ctx.labels
    src_speakers = speakers.get(source.id, {})
    src_tags = {t.lower(): t for t in labels.tags_of(source.id)}
    from src.utils.timezones import to_local, user_timezone_by_id
    zone = user_timezone_by_id(ctx.user_id)

    ranked = []
    for rid in candidates:
        rec = recs.get(rid)
        if rec is None:
            continue
        shared_speakers = sorted(src_speakers[k] for k in src_speakers if k in speakers.get(rid, {}))
        shared_tags = sorted(src_tags[t] for t in src_tags if t in {x.lower() for x in labels.tags_of(rid)})
        relation = labels.relation(source.id, rid)
        days = (abs((rec.meeting_date - source.meeting_date).total_seconds()) / 86400
                if rec.meeting_date and source.meeting_date else None)
        parts = {
            'speakers': len(shared_speakers) / len(src_speakers) if src_speakers else 0.0,
            'tags': len(shared_tags) / len(src_tags) if src_tags else 0.0,
            'folder': 1.0 if relation == 'same folder' else (0.5 if relation else 0.0),
            'date': 1 / (1 + days / 30) if days is not None else 0.0,
            'content': content.get(rid, 0.0),
        }
        score = sum(SIMILARITY_WEIGHTS[k] * v for k, v in parts.items())
        if score <= 0:
            continue
        reasons = {}
        if shared_speakers:
            reasons['shared_speakers'] = shared_speakers
        if shared_tags:
            reasons['shared_tags'] = shared_tags
        if relation:
            reasons['folder'] = relation
        if days is not None:
            reasons['days_apart'] = int(round(days))
        if rid in content:
            reasons['content_similarity'] = round(content[rid], 3)
        entry = {'recording_id': rid, 'title': rec.title or 'Untitled Recording',
                 'meeting_date': to_local(rec.meeting_date, zone).strftime('%Y-%m-%d') if rec.meeting_date else None,
                 'score': round(score, 3), 'reasons': reasons}
        entry.update(_label_fields(ctx, rid))
        ranked.append((score, rec.meeting_date or datetime.min, entry))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    results = [entry for _s, _d, entry in ranked[:limit]]
    result.update({'content_method': method, 'result_count': len(results), 'results': results})
    if method is None:
        result['note'] = 'Content similarity is unavailable for this recording (no indexed transcript).'
    return result


# ---------------------------------------------------------------------------
# People (find_people)
# ---------------------------------------------------------------------------

FIND_PEOPLE_CAP = 10


def tool_find_people(ctx, args):
    """The user's own speaker profiles and the participants of the recordings
    in scope whose names match `name` (see src/services/person_names.py).
    Never another user's profiles; recordings outside scope are not counted."""
    from src.models import Speaker
    from src.services.library_labels import is_generic_speaker
    from src.services import person_names as pn
    from src.utils.timezones import to_local, user_timezone_by_id

    query = ' '.join(str(args.get('name') or '').split())[:120]
    if not query or not pn.name_tokens(query):
        return {'error': "name is required"}
    if is_generic_speaker(query):
        return {'name': query, 'result_count': 0, 'people': [],
                'note': f'"{query}" is a diarization label, not a person.'}

    # Participants of the recordings in scope: {folded name: (spelling, {rec id: date})}.
    participants = {}
    if ctx.allowed_ids:
        for rid, text, date in db.session.query(Recording.id, Recording.participants, Recording.meeting_date) \
                .filter(Recording.id.in_(ctx.allowed_ids), Recording.participants.isnot(None)):
            for key, name in _speaker_names(text).items():
                entry = participants.setdefault(pn.fold(key), {'name': name, 'spellings': [], 'recordings': {}})
                if name not in entry['spellings']:
                    entry['spellings'].append(name)
                entry['recordings'][rid] = date

    people = []
    claimed = set()
    for sp in Speaker.query.filter_by(user_id=ctx.user_id).order_by(Speaker.name).all():
        if is_generic_speaker(sp.name):
            continue
        names = [sp.name] + [a for a in pn.aliases_of(sp) if pn.fold(a) != pn.fold(sp.name)]
        person = {'name': sp.name, 'profile_names': [(sp.name, 'profile name')]
                  + [(a, 'alias') for a in names[1:]],
                  'email': sp.email or None, 'spellings': [], 'recordings': {}}
        for n in names:
            entry = participants.get(pn.fold(n))
            if entry:
                claimed.add(pn.fold(n))
                person['spellings'] += [s for s in entry['spellings'] if s not in person['spellings']]
                person['recordings'].update(entry['recordings'])
        people.append(person)
    for key, entry in participants.items():
        if key not in claimed:
            people.append({'name': entry['name'], 'profile_names': [], 'email': None,
                           'spellings': list(entry['spellings']), 'recordings': dict(entry['recordings'])})

    matched = []
    for person in people:
        candidates = list(person['profile_names']) + [(s, 'participant') for s in person['spellings']]
        if person['email']:
            candidates.append((pn.email_local(person['email']), 'email'))
        best, how = None, []
        for text, kind in candidates:
            m = pn.name_match(query, text)
            if not m:
                continue
            got, total, variant = m
            score = (got / total, not variant)
            if best is None or score > best:
                best = score
            if kind not in how:
                how.append(kind)
            if variant and 'spelling variant' not in how:
                how.append('spelling variant')
        if best is None:
            continue
        if best[0] < 1:
            how.append('partial')
        matched.append((best, len(person['recordings']), person, how))

    zone = user_timezone_by_id(ctx.user_id)

    def row(person, how):
        names = []
        for n in [n for n, _k in person['profile_names']] + person['spellings']:
            if n not in names:
                names.append(n)
        dates = [d for d in person['recordings'].values() if d]
        out = {'name': person['name'], 'names': names, 'match': how,
               'recording_count': len(person['recordings']),
               'latest_recording_date': to_local(max(dates), zone).strftime('%Y-%m-%d') if dates else None}
        if person['email']:
            out['email'] = person['email']
        if person['profile_names']:
            out['has_profile'] = True
        return out

    matched.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result = {'name': query, 'result_count': min(len(matched), FIND_PEOPLE_CAP),
              'people': [row(p, how) for _b, _n, p, how in matched[:FIND_PEOPLE_CAP]]}
    if len(matched) > FIND_PEOPLE_CAP:
        result['truncated'] = True
    if matched:
        result['note'] = ('To search by a person, pass every name in their `names` to `speakers` or '
                          '`participants`. A spelling variant or partial match can be a different person: '
                          'check it against the conversation.')
        return result
    close = sorted(((pn.closeness(query, n), n) for person in people
                    for n in [x for x, _k in person['profile_names']] + person['spellings']), reverse=True)
    closest = []
    for score, n in close:
        if score < 0.6 or len(closest) >= 5:
            break
        if n not in closest:
            closest.append(n)
    result['note'] = f'No speaker profile or participant matches "{query}".'
    if closest:
        result['closest'] = closest
    return result


def _name_list(value, cap=10):
    """A name filter given as one name or a list of names."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:cap]


_DATE_PROPS = {
    'date_from': {'type': 'string', 'description': 'ISO date YYYY-MM-DD (inclusive)'},
    'date_to': {'type': 'string', 'description': 'ISO date YYYY-MM-DD (inclusive)'},
}

_SPEAKERS_PROP = {
    'type': 'array', 'items': {'type': 'string'},
    'description': 'Only recordings whose participants include any of these names. For a person, pass every '
                   'name find_people returned for them (profile name, aliases and participant spellings).'}

_LABEL_PROPS = {
    'tags': {'type': 'array', 'items': {'type': 'string'},
             'description': 'Only recordings with any of these tags (exact names, any case)'},
    'folder': {'type': 'string',
               'description': 'Only recordings in this folder or its subfolders (a folder name or its full '
                              'path, such as "Projects / Alpha")'},
}


def build_toolbox(availability, tool_names=None):
    """(schemas, executors) for the enabled tools only.

    Disabled content types are omitted from the schema entirely — the model
    never sees a tool it is not allowed to use. tool_names, when given, is
    the surface's tool list (assistant_events.tools_for_surface): tools not
    in it are left out as well.
    """
    tools = [
        ('search_transcripts', tool_search_transcripts,
         'Search the transcripts of the recordings in scope by meaning. Default mode combines semantic '
         'search with a keyword fallback when semantic results are thin. Returns matching snippets with '
         'recording ids, titles, speakers and timestamps. Use short, content-bearing queries; call multiple '
         'times with different phrasings. Use it for topics and ideas; for exact words, names, numbers or '
         'quotes use search_text. Raise top_k when surveying a topic broadly.',
         {'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'Search phrase'},
              'top_k': {'type': 'integer', 'description': 'Max results, 1-40 (default 8)'},
              'mode': {'type': 'string', 'enum': ['auto', 'semantic', 'keyword'],
                       'description': 'auto (default): semantic with keyword fallback; '
                                      'keyword: literal word/phrase matching only'},
              'speakers': _SPEAKERS_PROP,
              **_LABEL_PROPS,
              **_DATE_PROPS,
          },
          'required': ['query']}),
        ('search_text', tool_search_text,
         'Literal text search for exact words, names, numbers or phrases, case-insensitive. Searches '
         'transcripts, titles' + (', summaries' if availability.get('summaries') else '')
         + (" and the user's own notes" if availability.get('notes') else '') + '. Returns each match with '
         'its recording, field and a snippet; transcript matches also have speaker, time and seek_seconds. '
         'A recording gives at most ' + str(SEARCH_TEXT_PER_RECORDING) + ' transcript matches; '
         'more_matches_in lists the recordings that have more. result_count counts matches, '
         'recording_count the distinct recordings. An empty result means the text does not appear.',
         {'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The exact text to find'},
              'match': {'type': 'string', 'enum': ['phrase', 'all_words'],
                        'description': 'phrase (default): the exact phrase; all_words: every word, in any order, '
                                       'within one transcript passage or one field'},
              'fields': {'type': 'array', 'items': {'type': 'string', 'enum': [
                  f for f in TEXT_FIELDS if (f != 'summary' or availability.get('summaries'))
                  and (f != 'notes' or availability.get('notes'))]},
                  'description': 'Where to look (default: all of them)'},
              'top_k': {'type': 'integer', 'description': 'Max results, 1-50 (default 20)'},
              'speakers': _SPEAKERS_PROP,
              **_LABEL_PROPS,
              **_DATE_PROPS,
          },
          'required': ['query']}),
        ('list_recordings', tool_list_recordings,
         'List the recordings in scope with their metadata (title, date, participants, duration, tags, '
         'folder, which artifacts exist). Use this to survey what is available or find a recording by '
         'date, participant, tag or folder.',
         {'type': 'object',
          'properties': {
              'sort': {'type': 'string', 'enum': ['newest', 'oldest']},
              'limit': {'type': 'integer', 'description': 'Max rows, 1-100 (default 25)'},
              'participants': {**_SPEAKERS_PROP},
              'participant': {'type': 'string',
                              'description': 'One name; the same as participants with a single name'},
              **_LABEL_PROPS,
              **_DATE_PROPS,
          }}),
        ('find_similar_recordings', tool_find_similar_recordings,
         'Find the recordings most like one recording. Ranks the other recordings in scope by shared '
         'speakers, shared tags, the same folder (or a parent or subfolder), how close the dates are, and '
         'content similarity. Returns each with its score and the reasons. Use it first for questions about '
         'similar or related recordings, then read the ones that matter.',
         {'type': 'object',
          'properties': {
              'recording_id': {'type': 'integer'},
              'limit': {'type': 'integer', 'description': 'Max results, 1-25 (default 8)'},
          },
          'required': ['recording_id']}),
        ('find_people', tool_find_people,
         'Look up a person by name in the user\'s own speaker profiles (names, aliases, emails) and the '
         'participants of the recordings in scope. Matching ignores case and accents, matches whole name '
         'parts (a first name or a surname alone, or an email such as m.najjar) and spelling variants of '
         'transliterated names (Mohammad, Mohammed, Muhammad). Returns each person with every name they '
         'appear under, how the name matched, and how many recordings they are in. Call it before an '
         'exact-word search for a person, then pass all their names to the speakers or participants filter.',
         {'type': 'object',
          'properties': {'name': {'type': 'string', 'description': 'The name as the user wrote it'}},
          'required': ['name']}),
        ('get_transcript', tool_get_transcript,
         'Read a window of a recording\'s full transcript, in order. Paginated: the result reports '
         'total_segments and next_offset; call again with offset=next_offset to continue.',
         {'type': 'object',
          'properties': {
              'recording_id': {'type': 'integer'},
              'offset': {'type': 'integer', 'description': 'Segment offset to start from (default 0)'},
              'limit': {'type': 'integer', 'description': f'Segments to return (default {DEFAULT_TRANSCRIPT_PAGE}, max 400)'},
          },
          'required': ['recording_id']}),
        ('get_recording_metadata', tool_get_recording_metadata,
         'Get one recording\'s metadata: title, date, participants, duration, tags, folder, which artifacts exist.',
         {'type': 'object',
          'properties': {'recording_id': {'type': 'integer'}},
          'required': ['recording_id']}),
    ]
    if availability.get('summaries'):
        tools.append((
            'get_summary', tool_get_summary,
            'Read the stored AI summary of one recording. Cheap way to survey a long recording before '
            'deciding whether to read its transcript.',
            {'type': 'object',
             'properties': {'recording_id': {'type': 'integer'}},
             'required': ['recording_id']}))
    if availability.get('notes'):
        tools.append((
            'get_notes', tool_get_notes,
            'Read the user\'s own notes on one recording (only available on recordings they own).',
            {'type': 'object',
             'properties': {'recording_id': {'type': 'integer'}},
             'required': ['recording_id']}))

    if tool_names is not None:
        offered = set(tool_names)
        tools = [tool for tool in tools if tool[0] in offered]
    schemas = [{'type': 'function',
                'function': {'name': name, 'description': desc, 'parameters': params}}
               for name, _fn, desc, params in tools]
    executors = {name: fn for name, fn, _d, _p in tools}
    return schemas, executors
