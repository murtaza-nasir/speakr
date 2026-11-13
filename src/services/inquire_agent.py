"""Agentic Inquire: a ReAct-style tool loop over the user's recordings.

Replaces the single-shot RAG pipeline (router -> enrich -> one retrieval ->
answer) with an iterative agent that can search transcripts, browse recording
metadata, and read transcripts/summaries/notes until it can answer, then
streams the answer. See temp/design_inquire_agent.md for the full design.

Key properties:
- Every tool is scoped to the requesting user's ACLs AND the UI filter set
  (the agent can narrow within the filters, never escape them).
- Content availability is structural: a disabled content type's tool is not
  in the schema at all. Notes are only ever the user's OWN notes.
- Two tool-invocation modes: native OpenAI function calling, and a
  prompted-JSON fallback for endpoints without tool support. `auto` probes
  native once per (base_url, model) and remembers.
- Hard budgets: max steps, per-tool-result token cap, wall-clock cap, and the
  existing per-user token budget (checked by call_chat_completion each step).
- On failure before any answer content, falls back to the caller-provided
  legacy pipeline so Inquire never regresses.
"""

import json
import os
import re
import time as time_mod
from datetime import datetime, time as dtime

from src.database import db
from src.models import Folder, Recording, RecordingTag, Tag, User
from src.utils.search import LIKE_ESCAPE, contains_pattern
from src.services.embeddings import (
    get_accessible_recording_ids,
    semantic_search_chunks,
    basic_text_search_chunks,
)
from src.services.llm import call_chat_completion, TokenBudgetExceeded, get_chat_config
from src.utils.localization import translate
from src.services.assistant_present import (step_label, step_result, summary_label, summary_line,
                                              tool_icon)

# ---------------------------------------------------------------------------
# Configuration (read live so tests and admins can tweak without restart)
# ---------------------------------------------------------------------------

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('true', '1', 'yes')


def agent_enabled():
    return _env_bool('ENABLE_INQUIRE_AGENT', False)


def _max_steps():
    return max(1, _env_int('INQUIRE_AGENT_MAX_STEPS', 8))


def _tool_result_token_cap():
    return max(500, _env_int('INQUIRE_AGENT_TOOL_RESULT_TOKENS', 4000))


def _timeout_seconds():
    return max(15, _env_int('INQUIRE_AGENT_TIMEOUT_SECONDS', 120))


def _context_budget_tokens():
    return max(4000, _env_int('INQUIRE_CONTEXT_BUDGET_TOKENS', 24000))


def _tool_mode():
    mode = os.environ.get('INQUIRE_AGENT_TOOL_MODE', 'auto').strip().lower()
    return mode if mode in ('auto', 'native', 'prompt') else 'auto'


# Remembers, per (base_url, model), whether native tool calling works, so
# `auto` mode only pays the probe once per process lifetime.
_NATIVE_TOOLS_SUPPORTED = {}


def _estimate_tokens(text):
    """Cheap char/4 heuristic; no tokenizer dependency."""
    return len(text) // 4 if text else 0


# ---------------------------------------------------------------------------
# Availability (decision: transcripts always on; summaries/notes optional)
# ---------------------------------------------------------------------------

def get_availability(user):
    """Effective content availability for a user.

    A NULL column means "use the admin default" so changing the env default
    later affects users who never made an explicit choice.
    """
    allow_summaries = getattr(user, 'inquire_allow_summaries', None)
    if allow_summaries is None:
        allow_summaries = _env_bool('INQUIRE_DEFAULT_ALLOW_SUMMARIES', True)
    allow_notes = getattr(user, 'inquire_allow_notes', None)
    if allow_notes is None:
        allow_notes = _env_bool('INQUIRE_DEFAULT_ALLOW_NOTES', False)
    return {'summaries': bool(allow_summaries), 'notes': bool(allow_notes)}


# ---------------------------------------------------------------------------
# Scope: the set of recording ids the agent may touch this request
# ---------------------------------------------------------------------------

def resolve_allowed_recording_ids(user_id, filters):
    """Accessible recordings intersected with the UI filter set.

    Mirrors the filter semantics of semantic_search_chunks so search results
    and direct reads can never disagree about what is in scope.
    """
    accessible = set(get_accessible_recording_ids(user_id))
    if not accessible:
        return set()

    q = db.session.query(Recording.id).filter(Recording.id.in_(accessible))
    filters = filters or {}

    if filters.get('recording_ids'):
        q = q.filter(Recording.id.in_(filters['recording_ids']))
    if filters.get('tag_ids'):
        q = q.join(RecordingTag, Recording.id == RecordingTag.recording_id) \
             .filter(RecordingTag.tag_id.in_(filters['tag_ids']))
    if filters.get('folder_ids'):
        q = q.filter(Recording.folder_id.in_(filters['folder_ids']))
    if filters.get('speaker_names'):
        q = q.filter(db.or_(*[
            Recording.participants.ilike(contains_pattern(name), escape=LIKE_ESCAPE)
            for name in filters['speaker_names']
        ]))
    # Local calendar days of the user, as UTC bounds (#412).
    from src.utils.timezones import local_date_bounds, user_timezone_by_id
    _start, _end = local_date_bounds(filters.get('date_from'), filters.get('date_to'),
                                     user_timezone_by_id(user_id))
    if _start is not None:
        q = q.filter(Recording.meeting_date >= _start)
    if _end is not None:
        q = q.filter(Recording.meeting_date < _end)

    return {row[0] for row in q.all()}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

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
    if args.get('speakers'):
        filters['speaker_names'] = [str(s) for s in args['speakers']][:10]
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
    if args.get('participant'):
        q = q.filter(Recording.participants.ilike(contains_pattern(args['participant']), escape=LIKE_ESCAPE))
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


def _names_line(labels, tool_names, cap=60):
    """The tag names and folder paths of the recordings in scope, for the
    prompt, when a tool that filters by them is offered."""
    filtering = {'search_transcripts', 'search_text', 'list_recordings'}
    if tool_names is not None and not filtering & set(tool_names):
        return ''
    tags = labels.tag_names_in_use()
    folders = labels.folder_paths_in_use()
    if not tags and not folders:
        return ''
    from src.services.library_labels import capped
    lines = []
    if tags:
        lines.append(f"Tags on these recordings: {capped(tags, cap)}.")
    if folders:
        lines.append(f"Folders: {capped(folders, cap)}.")
    lines.append("To narrow a search or a list by tag or folder, pass these exact names in `tags` or `folder` "
                 "(a folder includes its subfolders).")
    return '\n'.join(lines)


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
    if q is not None and isinstance(args.get('speakers'), list) and args['speakers']:
        q = q.filter(db.or_(*[Recording.participants.ilike(contains_pattern(str(s)), escape=LIKE_ESCAPE)
                              for s in args['speakers'][:10]]))
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
        return {**result, 'result_count': 0, 'results': []}

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

    if 'transcript' in fields:
        chunks = (TranscriptChunk.query.options(joinedload(TranscriptChunk.recording))
                  .join(Recording, Recording.id == TranscriptChunk.recording_id)
                  .filter(TranscriptChunk.recording_id.in_(ids), matches(TranscriptChunk.content))
                  .order_by(Recording.meeting_date.desc(), TranscriptChunk.recording_id, TranscriptChunk.chunk_index)
                  .limit(top_k * 4).all())
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
            # The stored JSON escapes quotes, backslashes and non-ASCII text,
            # so only plain ASCII terms narrow the query in SQL.
            plain = [t for t in terms if t.isascii() and '"' not in t and '\\' not in t]
            if plain:
                rq = rq.filter(and_(*[Recording.transcription.ilike(contains_pattern(t), escape=LIKE_ESCAPE)
                                      for t in plain]))
            from src.services.transcript_segments import segments_of
            for rec in rq.order_by(Recording.meeting_date.desc()).limit(top_k * 4).all():
                segments = segments_of(rec.transcription)
                if segments is None:
                    segments = [{'speaker': None, 'text': rec.transcription, 'start_time': None}]
                for seg in segments:
                    if _line_matches(seg['text'], terms):
                        hits.append({**base(rec), 'field': 'transcript', 'speaker': seg['speaker'],
                                     'time': _fmt_time(seg['start_time']),
                                     'seek_seconds': int(seg['start_time']) if seg['start_time'] is not None
                                     else None,
                                     'snippet': _snippet(seg['text'], terms)})

    truncated = len(hits) > top_k
    hits = hits[:top_k]
    result.update({'result_count': len(hits), 'results': hits})
    if truncated:
        result['truncated'] = True
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


_DATE_PROPS = {
    'date_from': {'type': 'string', 'description': 'ISO date YYYY-MM-DD (inclusive)'},
    'date_to': {'type': 'string', 'description': 'ISO date YYYY-MM-DD (inclusive)'},
}

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
              'speakers': {'type': 'array', 'items': {'type': 'string'},
                           'description': 'Restrict to recordings whose participants include these names'},
              **_LABEL_PROPS,
              **_DATE_PROPS,
          },
          'required': ['query']}),
        ('search_text', tool_search_text,
         'Literal text search for exact words, names, numbers or phrases, case-insensitive. Searches '
         'transcripts, titles' + (', summaries' if availability.get('summaries') else '')
         + (" and the user's own notes" if availability.get('notes') else '') + '. Returns each match with '
         'its recording, field and a snippet; transcript matches also have speaker, time and seek_seconds. '
         'An empty result means the text does not appear.',
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
              'speakers': {'type': 'array', 'items': {'type': 'string'},
                           'description': 'Restrict to recordings whose participants include these names'},
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
              'participant': {'type': 'string', 'description': 'Only recordings whose participants include this name'},
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


# Step labels, results and the steps line: src/services/assistant_present.py.


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _today_for(user_ctx):
    """Today in the user's timezone (#412); UTC when none is known."""
    from src.utils.timezones import now_local, is_valid_timezone
    zone = user_ctx.get('timezone')
    return now_local(zone if is_valid_timezone(zone) else 'UTC').strftime('%Y-%m-%d')


_TOOL_PHRASES = (
    ('search_transcripts', 'search transcripts'),
    ('search_text', 'search for exact words'),
    ('list_recordings', 'list recordings'),
    ('find_similar_recordings', 'find similar recordings'),
    ('get_transcript', 'read transcripts'),
    ('get_summary', 'read stored summaries'),
    ('get_notes', "read the user's own notes"),
    ('get_recording_metadata', 'read metadata'),
)


def _tools_sentence(availability, tool_names):
    """The prompt describes only the tools that are offered (decision 2)."""
    if tool_names is None:
        notes_line = "the user's own notes, " if availability.get('notes') else ''
        summaries_line = "stored summaries, " if availability.get('summaries') else ''
        return (f"You have tools to search transcripts, search for exact words, list recordings, find similar "
                f"recordings, and read transcripts, {summaries_line}{notes_line}and metadata")
    offered = set(tool_names)
    if not availability.get('notes'):
        offered.discard('get_notes')
    if not availability.get('summaries'):
        offered.discard('get_summary')
    phrases = [phrase for name, phrase in _TOOL_PHRASES if name in offered]
    if len(phrases) > 1:
        phrases = phrases[:-1] + ['and ' + phrases[-1]]
    return "You have tools to " + ', '.join(phrases)


def _search_guidance(tool_names):
    """How to choose between the search tools, for the tools offered."""
    offered = None if tool_names is None else set(tool_names)

    def has(name):
        return offered is None or name in offered

    lines = []
    if has('search_text') and has('search_transcripts'):
        lines.append("Use search_text for exact words, names, numbers and quotes, and search_transcripts for "
                     "topics and meaning.")
    elif has('search_text'):
        lines.append("Use search_text for exact words, names, numbers and quotes.")
    if has('find_similar_recordings'):
        lines.append("When asked for recordings similar or related to one recording, call "
                     "find_similar_recordings first.")
    return ' '.join(lines)


def _system_prompt(user_ctx, filters_text, availability, tool_mode_prompt_block, tool_names=None,
                   names_line=''):
    language_instruction = (
        f"Respond in {user_ctx['output_language']}. " if user_ctx.get('output_language') else '')
    from src.utils.assistant_name import assistant_identity
    identity = assistant_identity(user_ctx.get('assistant_name') or 'Ambrose', user_ctx.get('name'),
                                  user_ctx.get('title'), user_ctx.get('company'))
    return f"""{identity} Here you help them research their recording library (meetings, calls, voice \
notes). {language_instruction}Today's date is {_today_for(user_ctx)}.

{_tools_sentence(availability, tool_names)}{filters_text}. Work iteratively: search or browse first, read more where needed, and only \
answer once you have enough evidence. Prefer summaries to survey long recordings, transcripts for exact \
quotes and details. If a search finds nothing, try different phrasings or list_recordings before giving up. \
Never invent content that is not in the tool results; say clearly when something cannot be found.\
{(' ' + _search_guidance(tool_names)) if _search_guidance(tool_names) else ''}\
{(chr(10) + chr(10) + names_line) if names_line else ''}

{tool_mode_prompt_block}When you give your final answer:
- Use clear markdown (headings per recording where it helps, bullets, **bold** speaker names).
- Cite sources inline as markdown links. When the supporting search result has a time and \
seek_seconds, link directly to that moment using the result's `time` value verbatim (the colon \
form like 12:34 — never the raw seek_seconds number): \
[Title @ time](/recordings/<recording_id>?t=<seek_seconds>) — for example \
[Weekly Sync @ 12:34](/recordings/73?t=754) or [Board Meeting @ 1:31:03](/recordings/80?t=5463). \
Without a timestamp, link the recording: \
[Title](/recordings/<recording_id>). Cite the recording each substantive point came from, at most \
one citation per bullet or paragraph. These links open the recording and start playback at that \
moment. The link text must ALWAYS be the recording's exact title (plus " @ time" when known) — \
never a date, a number, or other text — because the interface derives a numbered source list from \
it: citations render as compact numbered markers and the titles appear once at the end. For the \
same reason, avoid repeating a recording's title in the prose immediately around its citation. \
Never write a "Sources" or "References" section yourself — the interface appends one \
automatically; end your answer after the last point. When the user refers to a bare number \
("what about 5", "open 3"), they mean that numbered citation from your previous answer; the \
displayed numbering is included with each earlier answer in this conversation — use it, never guess.
- Order information from the most recent recordings first unless the question implies otherwise."""


_PROMPT_MODE_BLOCK = """TOOL CALLING PROTOCOL (this endpoint has no native tool support):
To use a tool, reply with ONLY a single JSON object, nothing else:
{"tool": "<tool_name>", "arguments": { ... }}
Available tools and their arguments are listed below. One tool call per reply.
To give your final answer, reply with the answer text directly (never start a final answer with "{").

TOOLS:
%TOOLS%

"""


# ---------------------------------------------------------------------------
# Context assembly + compaction
# ---------------------------------------------------------------------------

KEEP_VERBATIM_MESSAGES = 8  # last 4 turns


def citation_lines(sources):
    """The numbering the interface displayed for an answer's citations, one
    line per source: a library source names its recording, a segment source
    (the recording panel) its segment, time and speaker."""
    if not isinstance(sources, list):
        return []
    lines = []
    for s in sources[:30]:
        if not isinstance(s, dict):
            continue
        try:
            n = int(s.get('n'))
        except (TypeError, ValueError):
            continue
        if s.get('recording_id') is not None:
            try:
                rid = int(s.get('recording_id'))
            except (TypeError, ValueError):
                continue
            title = str(s.get('title') or '')[:120]
            lines.append(f"{n} = {title} (recording {rid})")
        elif s.get('segment_index') is not None:
            try:
                index = int(s.get('segment_index'))
            except (TypeError, ValueError):
                continue
            bits = [f"segment S{index} of this recording"]
            if isinstance(s.get('start_time'), (int, float)):
                bits.append(f"at {_fmt_time(s['start_time'])}")
            if s.get('speaker'):
                bits.append(f"({str(s['speaker'])[:60]})")
            if s.get('quote'):
                bits.append(f"\"{str(s['quote'])[:120]}\"")
            lines.append(f"{n} = {' '.join(bits)}")
    return lines


def sanitize_history(message_history):
    """Client history -> [{role, content, note?}].

    Assistant messages carry provenance out-of-band in ``note`` (research
    performed, plus the citation numbering the UI actually displayed, so a
    follow-up like "tell me more about 5" resolves to what the user saw).
    The note is injected as a SEPARATE system message at build time, never
    folded into the assistant's own content: models imitate the tail of
    their previous answers, and an inline annotation ends up reproduced
    verbatim at the bottom of new answers.
    """
    out = []
    for msg in message_history or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        content = msg.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str) or not content.strip():
            continue
        entry = {'role': role, 'content': content}
        if role == 'assistant':
            note_bits = []
            if msg.get('activity'):
                note_bits.append(f"research performed: {str(msg['activity'])[:300]}")
            lines = citation_lines(msg.get('sources'))
            if lines:
                note_bits.append(f"citations were displayed to the user numbered: {'; '.join(lines)}")
            if note_bits:
                entry['note'] = ("Note about the assistant's previous answer (system record, "
                                 "never reproduce in answers): " + " | ".join(note_bits))
        out.append(entry)
    return out


def estimate_context_tokens(system_prompt, memory, history, toolbox_schemas):
    total = _estimate_tokens(system_prompt) + _estimate_tokens(memory or '')
    total += sum(_estimate_tokens(m['content']) + _estimate_tokens(m.get('note', ''))
                 for m in history)
    total += _estimate_tokens(json.dumps(toolbox_schemas))
    return total


def compact_history(user_id, memory, history, keep_last=KEEP_VERBATIM_MESSAGES):
    """Fold all but the last `keep_last` messages into a rolling memory block.

    Returns (new_memory, kept_history, folded_count). Raises nothing: on any
    LLM failure it hard-truncates instead (the caller keeps going).
    """
    if len(history) <= keep_last:
        return memory, history, 0
    to_fold = history[:-keep_last]
    kept = history[-keep_last:]
    transcript = "\n\n".join(
        f"{m['role'].upper()}: {m['content']}"
        + (f"\n[{m['note']}]" if m.get('note') else '')
        for m in to_fold)
    prompt = f"""Summarize this conversation-so-far into a compact memory block that lets an assistant \
continue the conversation seamlessly. Capture: topics discussed, questions asked and their answers (with \
any recording titles/ids referenced), decisions or conclusions, and open threads. Be dense and factual; \
under 400 words.

{"Existing memory from even earlier turns:" + chr(10) + memory + chr(10) + chr(10) if memory else ""}Conversation to fold in:
{transcript[:24000]}"""
    try:
        resp = call_chat_completion(
            messages=[{'role': 'system', 'content': 'You compress conversations into dense, factual memory blocks.'},
                      {'role': 'user', 'content': prompt}],
            temperature=0.2, max_tokens=700,
            user_id=user_id, operation_type='inquire_compaction')
        new_memory = (resp.choices[0].message.content or '').strip()
        if not new_memory:
            raise ValueError('empty compaction result')
        return new_memory, kept, len(to_fold)
    except Exception:
        # Degrade to hard truncation — never block the user's question.
        marker = '[Earlier conversation truncated]'
        combined = (memory + '\n' if memory else '') + marker
        return combined, kept, len(to_fold)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


class _ThinkingFilter:
    """Splits streamed content into visible/thinking parts (<think> tags)."""

    _OPEN = re.compile(r'<think(?:ing)?>', re.IGNORECASE)
    _CLOSE = re.compile(r'</think(?:ing)?>', re.IGNORECASE)

    def __init__(self):
        self.buffer = ''
        self.in_thinking = False
        self.thinking = ''

    def feed(self, fragment):
        """Returns (visible_text, thinking_completed_or_None)."""
        self.buffer += fragment
        visible = ''
        thinking_out = None
        while True:
            if not self.in_thinking:
                m = self._OPEN.search(self.buffer)
                if m:
                    visible += self.buffer[:m.start()]
                    self.buffer = self.buffer[m.end():]
                    self.in_thinking = True
                    self.thinking = ''
                else:
                    # Hold back a partial "<think" prefix at the tail.
                    safe_len = len(self.buffer)
                    tail = self.buffer[-9:]
                    lt = tail.rfind('<')
                    if lt != -1 and self._OPEN.match(tail[lt:]) is None and '<think'.startswith(tail[lt:lt + 6].lower()):
                        safe_len = len(self.buffer) - (len(tail) - lt)
                    visible += self.buffer[:safe_len]
                    self.buffer = self.buffer[safe_len:]
                    break
            else:
                m = self._CLOSE.search(self.buffer)
                if m:
                    self.thinking += self.buffer[:m.start()]
                    self.buffer = self.buffer[m.end():]
                    self.in_thinking = False
                    if self.thinking.strip():
                        thinking_out = self.thinking.strip()
                    self.thinking = ''
                else:
                    hold = 10
                    self.thinking += self.buffer[:-hold] if len(self.buffer) > hold else ''
                    self.buffer = self.buffer[-hold:] if len(self.buffer) > hold else self.buffer
                    break
        return visible, thinking_out

    def flush(self):
        if self.in_thinking:
            leftover = (self.thinking + self.buffer).strip()
            self.buffer = ''
            self.thinking = ''
            return '', (leftover or None)
        visible = self.buffer
        self.buffer = ''
        return visible, None


def _native_tools_known_unsupported(err_text):
    err = (err_text or '').lower()
    return any(k in err for k in ('tool', 'function', 'unsupported', 'not support'))


def _recording_tools_block(user_ctx, availability, tool_names, filters_text, scope, names_line=''):
    """The tools note for the recording panel (Ambrose 2b): a second system
    message after the recording builder's prompt, which stays as it is. It
    describes only the tools offered and how to cite other recordings."""
    parts = [
        f"{_tools_sentence(availability, tool_names)} in {user_ctx['name']}'s recording library{filters_text}. "
        "Answer questions about this recording from its transcript above. Use the tools only when a question "
        "needs other recordings, for example \"did we discuss this anywhere else?\"."
        + ((' ' + _search_guidance(tool_names)) if _search_guidance(tool_names) else '')
        + (f" This recording's id is {scope['recording_id']}." if scope.get('recording_id') else ''),
        "When you use something from another recording, cite it as a markdown link whose text is that "
        "recording's exact title: [Title @ time](/recordings/<recording_id>?t=<seek_seconds>) with the "
        "result's `time` value verbatim, or [Title](/recordings/<recording_id>) without a time. Never write a "
        "\"Sources\" section; the interface lists the sources.",
    ]
    if names_line:
        parts.append(names_line)
    if scope.get('markers'):
        parts.append('Keep citing this recording as [S12 "exact words"].')
    cut = scope.get('cut')
    if cut:
        if cut.get('unit') == 'segments':
            parts.append(
                f"The transcript above is long and stops before segment {cut['offset']} of {cut['total']}. "
                f"When a question needs the rest, read it with get_transcript (recording_id {scope['recording_id']}, "
                f"offset {cut['offset']}); its lines carry the same segment markers.")
        else:
            parts.append(
                f"The transcript above is long and stops after {cut['offset']} of {cut['total']} characters. "
                f"When a question needs the rest, read it with get_transcript (recording_id {scope['recording_id']}, "
                f"offset {cut['offset']}).")
    return '\n\n'.join(parts)


def run_inquire_agent(app, user_id, user_ctx, user_message, message_history,
                      conversation_memory, filters, legacy_fallback=None,
                      llm_call=call_chat_completion, tool_names=None, recording_scope=None,
                      operation_type='inquire_agent'):
    """SSE generator implementing the agent loop.

    `tool_names`: the surface's tool list (assistant_events.tools_for_surface);
    None offers every tool.

    `recording_scope` (the recording panel, Ambrose 2b): {'messages',
    'recording_id', 'markers', 'cut'}. The recording builder's messages are
    sent as they are (system prompt with the whole transcript, history,
    question) with one system message added after the prompt that describes
    the tools; message_history and conversation_memory are not used. The open
    recording is added to the tools' scope only when its transcript is cut
    (`cut`), so the rest can be paged. If the endpoint rejects tool calls,
    `legacy_fallback` (the transcript-only answer) runs once instead.

    `legacy_fallback`: zero-arg generator function; invoked (yield from) if the
    agent fails before producing any answer content.
    `llm_call`: injectable for tests.
    """
    ctx = app.app_context()
    ctx.push()
    produced_answer = False
    try:
        try:
            user = db.session.get(User, user_id)
            availability = get_availability(user)
            allowed_ids = resolve_allowed_recording_ids(user_id, filters)
            if recording_scope and recording_scope.get('cut'):
                allowed_ids = set(allowed_ids) | {recording_scope['recording_id']}
            schemas, executors = build_toolbox(availability, tool_names)
        except Exception as e:
            app.logger.error(f"Inquire agent setup failed: {e}", exc_info=True)
            if legacy_fallback is not None:
                yield from legacy_fallback()
                return
            yield _sse({'error': str(e)})
            return

        # Filter description for the prompt
        filter_bits = []
        f = filters or {}
        if f.get('tag_ids'):
            names = [t.name for t in Tag.query.filter(Tag.id.in_(f['tag_ids'])).all()]
            filter_bits.append(f"tags: {', '.join(names)}")
        if f.get('folder_ids'):
            names = [x.name for x in Folder.query.filter(Folder.id.in_(f['folder_ids'])).all()]
            filter_bits.append(f"folders: {', '.join(names)}")
        if f.get('speaker_names'):
            filter_bits.append(f"speakers: {', '.join(f['speaker_names'])}")
        if f.get('date_from') or f.get('date_to'):
            filter_bits.append(f"dates {f.get('date_from', '...')} to {f.get('date_to', '...')}")
        if f.get('recording_ids'):
            filter_bits.append(f"{len(f['recording_ids'])} selected recordings")
        filters_text = (f". The user has limited this conversation to {len(allowed_ids)} recordings "
                        f"({'; '.join(filter_bits)})" if filter_bits
                        else f". {len(allowed_ids)} recordings are in scope")

        tool_ctx = ToolContext(user_id, filters, allowed_ids, availability,
                               marked_ids=[recording_scope['recording_id']]
                               if recording_scope and recording_scope.get('markers') else None)
        try:
            names_line = _names_line(tool_ctx.labels, tool_names) if schemas else ''
        except Exception as e:
            app.logger.warning(f"Inquire agent: tag and folder names unavailable: {e}")
            names_line = ''

        # Tool mode resolution
        cfg = get_chat_config()
        mode_key = (cfg.get('base_url'), cfg.get('model_name'))
        mode = _tool_mode()
        if mode == 'auto':
            known = _NATIVE_TOOLS_SUPPORTED.get(mode_key)
            mode = 'prompt' if known is False else 'native'
            if known is False and recording_scope and legacy_fallback is not None:
                # This endpoint rejects tool calls: the transcript-only answer.
                yield from legacy_fallback()
                return

        def build_messages(current_mode, memory, history):
            block = ''
            if current_mode == 'prompt':
                tool_lines = "\n".join(
                    f"- {s['function']['name']}: {s['function']['description']} "
                    f"Arguments schema: {json.dumps(s['function']['parameters'])}"
                    for s in schemas)
                block = _PROMPT_MODE_BLOCK.replace('%TOOLS%', tool_lines)
            if recording_scope:
                note = _recording_tools_block(user_ctx, availability, tool_names, filters_text, recording_scope,
                                              names_line)
                if block:
                    note += '\n\n' + block.rstrip()
                msgs = list(recording_scope['messages'])
                msgs.insert(1, {'role': 'system', 'content': note})
                return msgs
            sys_prompt = _system_prompt(user_ctx, filters_text, availability, block, tool_names, names_line)
            msgs = [{'role': 'system', 'content': sys_prompt}]
            if memory:
                msgs.append({'role': 'system',
                             'content': f"Conversation memory (earlier turns, summarized):\n{memory}"})
            for m in history:
                msgs.append({'role': m['role'], 'content': m['content']})
                # Provenance notes travel as system messages so the model
                # never mistakes them for part of its own answer style.
                if m.get('note'):
                    msgs.append({'role': 'system', 'content': m['note']})
            msgs.append({'role': 'user', 'content': user_message})
            return msgs

        history = sanitize_history(message_history)
        memory = (conversation_memory or '').strip() or None

        # Context accounting + compaction before the first call (the Ask
        # page; the recording panel sends its builder's messages as they are)
        budget = _context_budget_tokens()
        est = estimate_context_tokens(_system_prompt(user_ctx, filters_text, availability, '', tool_names,
                                                     names_line),
                                      memory, history, schemas)
        usage_pct = min(int(est * 100 / budget), 100)
        if not recording_scope:
            yield _sse({'context_usage': usage_pct})
        if not recording_scope and est > budget * 0.6 and len(history) > KEEP_VERBATIM_MESSAGES:
            yield _sse({'status': 'compacting', 'message': translate('server.apiMessage.summarizingConversation')})
            memory, history, folded = compact_history(user_id, memory, history)
            if folded:
                yield _sse({'context_compacted': {'memory': memory, 'keep_last': KEEP_VERBATIM_MESSAGES}})

        messages = build_messages(mode, memory, history)

        started = time_mod.monotonic()
        step_id = 0
        tool_counts = {}
        call_cache = {}
        consecutive_empty_searches = 0
        answer_filter = _ThinkingFilter()

        language = user_ctx.get('language')
        titles = {}

        def title_of(args):
            """The title of the recording a tool reads, only when it is in scope."""
            try:
                rid = int((args or {}).get('recording_id'))
            except (TypeError, ValueError):
                return None
            if rid not in allowed_ids:
                return None
            if rid not in titles:
                rec = db.session.get(Recording, rid)
                titles[rid] = rec.title if rec and rec.title else None
            return titles[rid]

        def finish(summary_needed=True):
            events = []
            if summary_needed and tool_counts:
                steps = sum(tool_counts.values())
                events.append(_sse({'agent_summary': {
                    'steps': steps,
                    'line': summary_line(tool_counts, language),
                    'label': summary_label(steps, tool_counts, language)}}))
            if memory:
                events.append(_sse({'conversation_memory': memory}))
            events.append(_sse({'end_of_stream': True}))
            return events

        def execute_tool(name, raw_args):
            nonlocal step_id, consecutive_empty_searches
            step_id += 1
            this_step = step_id
            try:
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or '{}')
                if not isinstance(args, dict):
                    args = {}
            except (ValueError, TypeError):
                args = None
            label = step_label(name, args or {}, language, title_of(args))
            events = [_sse({'agent_step': {'id': this_step, 'tool': name, 'name': name,
                                           'icon': tool_icon(name),
                                           'label': label, 'status': 'running'}})]
            if args is None:
                result = {'error': 'arguments were not valid JSON'}
            elif name not in executors:
                result = {'error': f"Unknown tool: {name}"}
            else:
                cache_key = (name, json.dumps(args, sort_keys=True))
                if cache_key in call_cache:
                    result = {'note': 'duplicate call — cached result returned; try a DIFFERENT '
                                      'query or tool instead of repeating this one',
                              **call_cache[cache_key]}
                else:
                    try:
                        result = executors[name](tool_ctx, args)
                    except Exception as e:
                        app.logger.warning(f"Inquire agent tool {name} failed: {e}", exc_info=True)
                        result = {'error': f"tool failed: {e}"}
                    call_cache[cache_key] = result
                    tool_counts[name] = tool_counts.get(name, 0) + 1
            status, detail = step_result(name, result, language)
            if name in ('search_transcripts', 'search_text'):
                consecutive_empty_searches = consecutive_empty_searches + 1 if status == 'empty' else 0
            events.append(_sse({'agent_step': {'id': this_step, 'tool': name, 'name': name,
                                               'icon': tool_icon(name),
                                               'label': label, 'status': status,
                                               'detail': detail}}))
            hint = ''
            if 'transcript' in (result or {}) and result.get('next_offset') is not None:
                hint = 'Call again with offset=next_offset for more.'
            result_text = _truncate_to_tokens(json.dumps(result, ensure_ascii=False),
                                              _tool_result_token_cap(), hint)
            return events, result_text

        max_steps = _max_steps()
        for iteration in range(max_steps + 1):
            timed_out = (time_mod.monotonic() - started) > _timeout_seconds()
            forcing_final = iteration >= max_steps or timed_out
            if forcing_final:
                messages.append({'role': 'system',
                                 'content': 'Step budget exhausted. Answer NOW with the evidence you '
                                            'already gathered; state clearly what could not be verified.'})
            if consecutive_empty_searches >= 2:
                consecutive_empty_searches = 0
                messages.append({'role': 'system',
                                 'content': 'Your last searches found nothing. Try substantially different '
                                            'terms, or use list_recordings to see what exists, or answer '
                                            'that the information does not appear in the recordings.'})

            use_tools = (mode == 'native') and not forcing_final
            try:
                if mode == 'native':
                    stream = llm_call(messages=messages,
                                      tools=schemas if use_tools else None,
                                      temperature=0.4, stream=True,
                                      max_tokens=int(os.environ.get('CHAT_MAX_TOKENS', '2000')),
                                      user_id=user_id, operation_type=operation_type)
                else:
                    response = llm_call(messages=messages, temperature=0.4,
                                        max_tokens=int(os.environ.get('CHAT_MAX_TOKENS', '2000')),
                                        user_id=user_id, operation_type=operation_type)
            except TokenBudgetExceeded:
                raise
            except Exception as e:
                if mode == 'native' and use_tools and recording_scope and legacy_fallback is not None \
                        and not produced_answer:
                    # Tool calls rejected in the recording panel: retried
                    # once without tools, as the transcript-only answer.
                    app.logger.info(f"Tool call rejected in the recording chat; answering without tools: {e}")
                    if _tool_mode() == 'auto' and _native_tools_known_unsupported(str(e)):
                        _NATIVE_TOOLS_SUPPORTED[mode_key] = False
                    yield from legacy_fallback()
                    return
                if mode == 'native' and _tool_mode() == 'auto' and _native_tools_known_unsupported(str(e)):
                    app.logger.info(f"Native tool calling unsupported by endpoint; switching to prompt mode: {e}")
                    _NATIVE_TOOLS_SUPPORTED[mode_key] = False
                    mode = 'prompt'
                    messages = build_messages(mode, memory, history)
                    continue
                raise

            if mode == 'native':
                # Consume the stream: accumulate tool calls; live-stream
                # content once it is clearly an answer (no tool deltas yet
                # and enough text), otherwise treat pre-tool content as
                # thinking.
                calls = {}
                content_buffer = ''
                streaming_live = False
                for chunk in stream:
                    if not getattr(chunk, 'choices', None):
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    for tc in (getattr(delta, 'tool_calls', None) or []):
                        entry = calls.setdefault(tc.index, {'id': tc.id, 'name': '', 'args': ''})
                        if tc.id:
                            entry['id'] = tc.id
                        if tc.function:
                            if tc.function.name:
                                entry['name'] = (entry['name'] or '') + tc.function.name
                            if tc.function.arguments:
                                entry['args'] += tc.function.arguments
                    text = getattr(delta, 'content', None)
                    if text:
                        if streaming_live:
                            visible, thinking = answer_filter.feed(text)
                            if visible:
                                produced_answer = True
                                yield _sse({'delta': visible})
                            if thinking:
                                yield _sse({'thinking': thinking})
                        else:
                            content_buffer += text
                            if not calls and len(content_buffer) > 200:
                                streaming_live = True
                                produced_answer = True
                                visible, thinking = answer_filter.feed(content_buffer)
                                content_buffer = ''
                                if visible:
                                    yield _sse({'delta': visible})
                                if thinking:
                                    yield _sse({'thinking': thinking})

                if calls:
                    if content_buffer.strip():
                        yield _sse({'thinking': content_buffer.strip()})
                    assistant_msg = {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': entry['id'] or f'call_{i}', 'type': 'function',
                         'function': {'name': entry['name'], 'arguments': entry['args'] or '{}'}}
                        for i, entry in sorted(calls.items())]}
                    messages.append(assistant_msg)
                    for i, entry in sorted(calls.items()):
                        events, result_text = execute_tool(entry['name'], entry['args'])
                        for ev in events:
                            yield ev
                        messages.append({'role': 'tool',
                                         'tool_call_id': entry['id'] or f'call_{i}',
                                         'content': result_text})
                    continue

                # No tool calls: this was the final answer.
                if not streaming_live and content_buffer:
                    visible, thinking = answer_filter.feed(content_buffer)
                    if visible:
                        produced_answer = True
                        yield _sse({'delta': visible})
                    if thinking:
                        yield _sse({'thinking': thinking})
                visible, thinking = answer_filter.flush()
                if visible:
                    produced_answer = True
                    yield _sse({'delta': visible})
                if thinking:
                    yield _sse({'thinking': thinking})
                if not produced_answer:
                    yield _sse({'delta': "I could not produce an answer. Please try rephrasing your question."})
                    produced_answer = True
                for ev in finish():
                    yield ev
                return

            else:  # prompt mode
                raw = (response.choices[0].message.content or '').strip()
                # strip a wrapping code fence, then decide: tool call or answer
                fenced = re.match(r'^```(?:json)?\s*(.*?)\s*```$', raw, re.DOTALL)
                candidate = fenced.group(1).strip() if fenced else raw
                parsed = None
                if candidate.startswith('{') and not forcing_final:
                    try:
                        parsed = json.loads(candidate)
                    except ValueError:
                        m = re.search(r'\{.*\}', candidate, re.DOTALL)
                        if m:
                            try:
                                parsed = json.loads(m.group())
                            except ValueError:
                                parsed = None
                    if parsed is None:
                        # one retry with the parse error fed back
                        messages.append({'role': 'assistant', 'content': raw})
                        messages.append({'role': 'system',
                                         'content': 'Your tool call was not valid JSON. Reply with ONLY '
                                                    'a valid JSON object {"tool": ..., "arguments": {...}} '
                                                    'or with your final answer as plain text.'})
                        continue
                if parsed is not None and isinstance(parsed, dict) and parsed.get('tool'):
                    messages.append({'role': 'assistant', 'content': candidate})
                    events, result_text = execute_tool(str(parsed.get('tool')),
                                                       parsed.get('arguments') or {})
                    for ev in events:
                        yield ev
                    messages.append({'role': 'user',
                                     'content': f"TOOL RESULT:\n{result_text}"})
                    continue

                # Final answer (single delta in prompt mode).
                cleaned = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', raw,
                                 flags=re.IGNORECASE | re.DOTALL).strip()
                produced_answer = True
                yield _sse({'delta': cleaned or raw})
                for ev in finish():
                    yield ev
                return

        # Loop exhausted without a final answer (shouldn't normally happen —
        # the forcing_final pass answers). Close out defensively.
        yield _sse({'delta': "I ran out of steps before finishing. Please try a more specific question."})
        for ev in finish():
            yield ev

    except TokenBudgetExceeded as e:
        yield _sse({'error': str(e), 'budget_exceeded': True})
    except Exception as e:
        app.logger.error(f"Inquire agent failed: {e}", exc_info=True)
        if not produced_answer and legacy_fallback is not None:
            app.logger.info("Falling back to the single-shot Inquire pipeline")
            try:
                yield from legacy_fallback()
                return
            except Exception as fe:
                app.logger.error(f"Legacy fallback also failed: {fe}")
        yield _sse({'error': str(e)})
    finally:
        ctx.pop()
