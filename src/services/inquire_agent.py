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
from src.models import Recording, RecordingTag, Tag, User
from src.services.embeddings import (
    get_accessible_recording_ids,
    semantic_search_chunks,
    basic_text_search_chunks,
)
from src.services.llm import call_chat_completion, TokenBudgetExceeded, get_chat_config

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
    if filters.get('speaker_names'):
        q = q.filter(db.or_(*[
            Recording.participants.ilike(f'%{name}%')
            for name in filters['speaker_names']
        ]))
    if filters.get('date_from'):
        q = q.filter(Recording.meeting_date >= datetime.combine(filters['date_from'], dtime.min))
    if filters.get('date_to'):
        q = q.filter(Recording.meeting_date <= datetime.combine(filters['date_to'], dtime.max))

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


def _recording_brief(rec):
    d = {
        'recording_id': rec.id,
        'title': rec.title or 'Untitled Recording',
        'has_transcript': bool(rec.transcription),
        'has_summary': bool(rec.summary),
        'has_notes': bool(rec.notes),
    }
    if rec.meeting_date:
        d['meeting_date'] = rec.meeting_date.strftime('%Y-%m-%d %H:%M')
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

    def __init__(self, user_id, filters, allowed_ids, availability):
        self.user_id = user_id
        self.filters = filters or {}
        self.allowed_ids = allowed_ids
        self.availability = availability

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
            'meeting_date': chunk.recording.meeting_date.strftime('%Y-%m-%d') if chunk.recording.meeting_date else None,
            'speaker': chunk.speaker_name,
            'time': _fmt_time(chunk.start_time),
            'seek_seconds': int(chunk.start_time) if chunk.start_time is not None else None,
            'chunk_index': chunk.chunk_index,
            'snippet': chunk.content,
            'similarity': round(float(similarity), 3),
        })
    return {'query': query, 'search_type': search_type, 'result_count': len(hits), 'results': hits}


def tool_list_recordings(ctx, args):
    q = Recording.query.filter(Recording.id.in_(ctx.allowed_ids))
    if args.get('participant'):
        q = q.filter(Recording.participants.ilike(f"%{args['participant']}%"))
    for arg, op in (('date_from', 'ge'), ('date_to', 'le')):
        if args.get(arg):
            try:
                parsed = datetime.fromisoformat(str(args[arg]))
            except ValueError:
                return {'error': f"{arg} must be an ISO date (YYYY-MM-DD)"}
            if op == 'ge':
                q = q.filter(Recording.meeting_date >= parsed)
            else:
                q = q.filter(Recording.meeting_date <= datetime.combine(parsed.date(), dtime.max))
    sort = args.get('sort', 'newest')
    q = q.order_by(Recording.meeting_date.asc() if sort == 'oldest' else Recording.meeting_date.desc())
    limit = min(max(int(args.get('limit', 25) or 25), 1), 100)
    total = q.count()
    recs = q.limit(limit).all()
    return {
        'total_in_scope': total,
        'returned': len(recs),
        'recordings': [_recording_brief(r) for r in recs],
    }


DEFAULT_TRANSCRIPT_PAGE = 150


def tool_get_transcript(ctx, args):
    rec, err = ctx.check_scope(args.get('recording_id'))
    if err:
        return {'error': err}
    if not rec.transcription:
        return {'error': f"Recording {rec.id} has no transcript"}

    offset = max(int(args.get('offset', 0) or 0), 0)
    limit = min(max(int(args.get('limit', DEFAULT_TRANSCRIPT_PAGE) or DEFAULT_TRANSCRIPT_PAGE), 1), 400)

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
    info = _recording_brief(rec)
    try:
        info['tags'] = [t.name for t in rec.tags]
    except Exception:
        pass
    return info


_DATE_PROPS = {
    'date_from': {'type': 'string', 'description': 'ISO date YYYY-MM-DD (inclusive)'},
    'date_to': {'type': 'string', 'description': 'ISO date YYYY-MM-DD (inclusive)'},
}


def build_toolbox(availability):
    """(schemas, executors) for the enabled tools only.

    Disabled content types are omitted from the schema entirely — the model
    never sees a tool it is not allowed to use.
    """
    tools = [
        ('search_transcripts', tool_search_transcripts,
         'Search the transcripts of the recordings in scope. Default mode combines semantic search '
         'with a literal keyword fallback when semantic results are thin. Returns matching snippets '
         'with recording ids, titles, speakers and timestamps. Use short, content-bearing queries; '
         'call multiple times with different phrasings. For exact terms, acronyms or jargon '
         '(project names, metric names), also try mode="keyword" with just that term. Raise top_k '
         'when surveying a topic broadly.',
         {'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'Search phrase'},
              'top_k': {'type': 'integer', 'description': 'Max results, 1-40 (default 8)'},
              'mode': {'type': 'string', 'enum': ['auto', 'semantic', 'keyword'],
                       'description': 'auto (default): semantic with keyword fallback; '
                                      'keyword: literal word/phrase matching only'},
              'speakers': {'type': 'array', 'items': {'type': 'string'},
                           'description': 'Restrict to recordings whose participants include these names'},
              **_DATE_PROPS,
          },
          'required': ['query']}),
        ('list_recordings', tool_list_recordings,
         'List the recordings in scope with their metadata (title, date, participants, duration, '
         'which artifacts exist). Use this to survey what is available or find a recording by date/participant.',
         {'type': 'object',
          'properties': {
              'sort': {'type': 'string', 'enum': ['newest', 'oldest']},
              'limit': {'type': 'integer', 'description': 'Max rows, 1-100 (default 25)'},
              'participant': {'type': 'string', 'description': 'Only recordings whose participants include this name'},
              **_DATE_PROPS,
          }}),
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
         'Get one recording\'s metadata: title, date, participants, duration, tags, which artifacts exist.',
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

    schemas = [{'type': 'function',
                'function': {'name': name, 'description': desc, 'parameters': params}}
               for name, _fn, desc, params in tools]
    executors = {name: fn for name, fn, _d, _p in tools}
    return schemas, executors


# Human-readable activity lines for the UI. (label, detail) builders.
_TOOL_ICONS = {
    'search_transcripts': 'fa-magnifying-glass',
    'list_recordings': 'fa-list',
    'get_transcript': 'fa-file-lines',
    'get_summary': 'fa-file-alt',
    'get_notes': 'fa-note-sticky',
    'get_recording_metadata': 'fa-circle-info',
}


def _activity_label(tool, args):
    if tool == 'search_transcripts':
        return f"Searching: “{str(args.get('query', ''))[:80]}”"
    if tool == 'list_recordings':
        return "Scanning recordings"
    if tool == 'get_transcript':
        off = args.get('offset') or 0
        return f"Reading transcript of recording {args.get('recording_id')}" + (f" (from segment {off})" if off else "")
    if tool == 'get_summary':
        return f"Reading summary of recording {args.get('recording_id')}"
    if tool == 'get_notes':
        return f"Reading notes on recording {args.get('recording_id')}"
    if tool == 'get_recording_metadata':
        return f"Checking recording {args.get('recording_id')}"
    return f"Running {tool}"


def _activity_result(tool, result):
    """(status, detail) once a tool has run."""
    if isinstance(result, dict) and result.get('error'):
        return 'error', str(result['error'])[:160]
    if tool == 'search_transcripts':
        n = result.get('result_count', 0)
        if not n:
            return 'empty', 'no matches'
        recs = {r['recording_id'] for r in result.get('results', [])}
        detail = f"{n} match{'es' if n != 1 else ''} in {len(recs)} recording{'s' if len(recs) != 1 else ''}"
        stype = result.get('search_type')
        if stype and stype != 'semantic':
            detail += f" ({stype.replace('semantic+keyword', 'incl. keyword')})"
        return 'done', detail
    if tool == 'list_recordings':
        return 'done', f"{result.get('returned', 0)} of {result.get('total_in_scope', 0)} recordings"
    if tool == 'get_transcript':
        if 'total_segments' in result:
            got = result.get('returned_segments', 0)
            return 'done', f"{got} segments (of {result.get('total_segments', '?')}) — {result.get('title', '')}"
        return 'done', str(result.get('title', ''))
    if tool in ('get_summary', 'get_notes', 'get_recording_metadata'):
        return 'done', str(result.get('title', ''))[:120]
    return 'done', ''


def _summary_line(tool_counts):
    names = {
        'search_transcripts': ('search', 'searches'),
        'list_recordings': ('listing', 'listings'),
        'get_transcript': ('transcript read', 'transcript reads'),
        'get_summary': ('summary read', 'summary reads'),
        'get_notes': ('notes read', 'notes reads'),
        'get_recording_metadata': ('metadata check', 'metadata checks'),
    }
    parts = []
    for tool, count in tool_counts.items():
        singular, plural = names.get(tool, (tool, tool))
        parts.append(f"{count} {singular if count == 1 else plural}")
    return ' · '.join(parts)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _system_prompt(user_ctx, filters_text, availability, tool_mode_prompt_block):
    language_instruction = (
        f"Respond in {user_ctx['output_language']}. " if user_ctx.get('output_language') else '')
    notes_line = "the user's own notes, " if availability.get('notes') else ''
    summaries_line = "stored summaries, " if availability.get('summaries') else ''
    return f"""You are a research assistant analyzing {user_ctx['name']}'s audio recording library \
(meetings, calls, voice notes). {user_ctx['name']} is a(n) {user_ctx['title']} at {user_ctx['company']}. \
{language_instruction}Today's date is {datetime.utcnow().strftime('%Y-%m-%d')}.

You have tools to search transcripts, list recordings, and read transcripts, {summaries_line}{notes_line}\
and metadata{filters_text}. Work iteratively: search or browse first, read more where needed, and only \
answer once you have enough evidence. Prefer summaries to survey long recordings, transcripts for exact \
quotes and details. If a search finds nothing, try different phrasings or list_recordings before giving up. \
Never invent content that is not in the tool results; say clearly when something cannot be found.

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
            sources = msg.get('sources')
            if isinstance(sources, list) and sources:
                lines = []
                for s in sources[:20]:
                    if not isinstance(s, dict):
                        continue
                    try:
                        n = int(s.get('n'))
                        rid = int(s.get('recording_id'))
                    except (TypeError, ValueError):
                        continue
                    title = str(s.get('title') or '')[:120]
                    lines.append(f"{n} = {title} (recording {rid})")
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


def run_inquire_agent(app, user_id, user_ctx, user_message, message_history,
                      conversation_memory, filters, legacy_fallback=None,
                      llm_call=call_chat_completion):
    """SSE generator implementing the agent loop.

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
            schemas, executors = build_toolbox(availability)
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
        if f.get('speaker_names'):
            filter_bits.append(f"speakers: {', '.join(f['speaker_names'])}")
        if f.get('date_from') or f.get('date_to'):
            filter_bits.append(f"dates {f.get('date_from', '...')} to {f.get('date_to', '...')}")
        if f.get('recording_ids'):
            filter_bits.append(f"{len(f['recording_ids'])} selected recordings")
        filters_text = (f". The user has limited this conversation to {len(allowed_ids)} recordings "
                        f"({'; '.join(filter_bits)})" if filter_bits
                        else f". {len(allowed_ids)} recordings are in scope")

        # Tool mode resolution
        cfg = get_chat_config()
        mode_key = (cfg.get('base_url'), cfg.get('model_name'))
        mode = _tool_mode()
        if mode == 'auto':
            known = _NATIVE_TOOLS_SUPPORTED.get(mode_key)
            mode = 'prompt' if known is False else 'native'

        def build_messages(current_mode, memory, history):
            block = ''
            if current_mode == 'prompt':
                tool_lines = "\n".join(
                    f"- {s['function']['name']}: {s['function']['description']} "
                    f"Arguments schema: {json.dumps(s['function']['parameters'])}"
                    for s in schemas)
                block = _PROMPT_MODE_BLOCK.replace('%TOOLS%', tool_lines)
            sys_prompt = _system_prompt(user_ctx, filters_text, availability, block)
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

        # Context accounting + compaction before the first call
        budget = _context_budget_tokens()
        est = estimate_context_tokens(_system_prompt(user_ctx, filters_text, availability, ''),
                                      memory, history, schemas)
        usage_pct = min(int(est * 100 / budget), 100)
        yield _sse({'context_usage': usage_pct})
        if est > budget * 0.6 and len(history) > KEEP_VERBATIM_MESSAGES:
            yield _sse({'status': 'compacting', 'message': 'Summarizing earlier conversation...'})
            memory, history, folded = compact_history(user_id, memory, history)
            if folded:
                yield _sse({'context_compacted': {'memory': memory, 'keep_last': KEEP_VERBATIM_MESSAGES}})

        messages = build_messages(mode, memory, history)

        tool_ctx = ToolContext(user_id, filters, allowed_ids, availability)
        started = time_mod.monotonic()
        step_id = 0
        tool_counts = {}
        call_cache = {}
        consecutive_empty_searches = 0
        answer_filter = _ThinkingFilter()

        def finish(summary_needed=True):
            events = []
            if summary_needed and tool_counts:
                events.append(_sse({'agent_summary': {
                    'steps': sum(tool_counts.values()),
                    'line': _summary_line(tool_counts)}}))
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
            label = _activity_label(name, args or {})
            events = [_sse({'agent_step': {'id': this_step, 'tool': name,
                                           'icon': _TOOL_ICONS.get(name, 'fa-gear'),
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
            status, detail = _activity_result(name, result)
            if name == 'search_transcripts':
                consecutive_empty_searches = consecutive_empty_searches + 1 if status == 'empty' else 0
            events.append(_sse({'agent_step': {'id': this_step, 'tool': name,
                                               'icon': _TOOL_ICONS.get(name, 'fa-gear'),
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
                                      user_id=user_id, operation_type='inquire_agent')
                else:
                    response = llm_call(messages=messages, temperature=0.4,
                                        max_tokens=int(os.environ.get('CHAT_MAX_TOKENS', '2000')),
                                        user_id=user_id, operation_type='inquire_agent')
            except TokenBudgetExceeded:
                raise
            except Exception as e:
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
