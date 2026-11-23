"""One event stream for the assistant's engines (Ambrose phase 2a, design
section 2).

Two engines answer: the single-call recording engine (the whole transcript in
the prompt, src/services/recording_chat.py) and the library engine (the
Inquire agent, or retrieval when the agent is off). Both reach the browser as
the same `data: {...}` events:

  delta {text}            answer text, in order
  thinking {text}         surfaced reasoning, never part of the answer
  source {n, marker, ...} a segment citation; sent before the delta that
                          carries its marker, so the browser can number it
  agent_step {id, name, tool, label, icon, status, detail}
                          one step; labels are built and translated on the
                          server (assistant_present.py)
  agent_summary {steps, line, label}
  status, context_usage, context_compacted, conversation_memory
  end_of_stream {finish_reason?, truncated?}, error {error, budget_exceeded?}

delta and thinking are the model's text unchanged (#632): Markdown that may
hold raw HTML from the question, a transcript or the model. They are not
escaped, since that would break the Markdown and code examples; every consumer
renders delta through a sanitizing Markdown renderer (window.renderMarkdownSafe,
static/js/markdown-safe.js: marked, then DOMPurify) or as plain text, and
thinking as plain text. API v1 documents the same for its `answer`
(docs/user-guide/api-reference.md, Inquire).

recording_events() adds `source` events to the recording engine's stream;
library_events() turns the retrieval stages (`status`) into steps. The event
names the Ask page and API v1 already read are kept; `source` is new.

tools_for_surface() is the tool list for each place the assistant answers
(decision 2): an incognito recording gets none, an open recording the library
tools, the Ask page all of them.
"""

import json

from src.services.assistant_present import RAG_ICONS, rag_step_label
from src.services.citations import SegmentCitations

LIBRARY_TOOLS = ('search_transcripts', 'search_text', 'list_recordings', 'find_similar_recordings',
                 'find_people', 'get_transcript', 'get_recording_metadata', 'get_summary', 'get_notes')

_SURFACE_TOOLS = {
    'incognito': (),
    'recording': LIBRARY_TOOLS,
    'ask': None,   # every tool the agent has
}


def tools_for_surface(surface):
    """The tool names offered on a surface: () for none, None for all."""
    if surface not in _SURFACE_TOOLS:
        raise ValueError(f'unknown assistant surface: {surface}')
    tools = _SURFACE_TOOLS[surface]
    return None if tools is None else list(tools)


class FilterError(ValueError):
    """A library filter value that cannot be used (#627). `code` is the
    server.apiError key the route answers with; the message never holds
    the value that was sent."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# The id filters of the filter column, with the code each refusal answers
ID_FILTERS = (('filter_tags', 'invalid_tag_ids'),
              ('filter_folders', 'invalid_ids_list'),
              ('filter_recording_ids', 'invalid_recording_ids_list'))


def parse_filter_date(value):
    """The date of a filter value, None when absent (null or ''). Any other
    value that is not an ISO date string raises FilterError."""
    from datetime import datetime
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise FilterError('invalid_filter_date')
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        raise FilterError('invalid_filter_date') from None


def filter_list(value, code, ids=False):
    """The list of a filter value, [] when absent (null); FilterError(code)
    for any value that is not a list, or with ids=True a list holding
    anything but integer ids (#629: "1" is not coerced, True is not 1)."""
    from src.utils.request_body import is_int_list
    if value is None:
        return []
    if not isinstance(value, list):
        raise FilterError(code)
    # An id past the range of the column would overflow the lookup (#664)
    if ids and not is_int_list(value):
        raise FilterError(code)
    return value


def checked_filters(data):
    """The filter column of a request body (filter_tags, filter_folders,
    filter_speakers, filter_recording_ids, filter_date_from, filter_date_to),
    checked: the id filters as lists of integer ids, the speakers as a list
    of non-empty names or one name, the dates parsed. The first unusable value raises
    FilterError. The one check of every route that takes these filters
    (#627, #629)."""
    data = data or {}
    checked = {key: filter_list(data.get(key), code, ids=True) for key, code in ID_FILTERS}
    speakers = data.get('filter_speakers')
    if speakers is None:
        speakers = []
    if not (isinstance(speakers, str)
            or (isinstance(speakers, list) and all(isinstance(name, str) and name.strip() for name in speakers))):
        raise FilterError('invalid_speaker_names')
    checked['filter_speakers'] = speakers
    checked['filter_date_from'] = parse_filter_date(data.get('filter_date_from'))
    checked['filter_date_to'] = parse_filter_date(data.get('filter_date_to'))
    return checked


def filters_from_request(data):
    """The library filters of an Ask page chat request, in the agent's form:
    the keys of its filter column (filter_tags, filter_folders,
    filter_speakers, filter_recording_ids, filter_date_from, filter_date_to).
    The filters are a ceiling: resolve_allowed_recording_ids intersects them
    with access. The recording panel has no filters. A value that cannot be
    used raises FilterError (a ValueError)."""
    checked = checked_filters(data)
    filters = {}
    for key, name in (('filter_tags', 'tag_ids'), ('filter_folders', 'folder_ids'),
                      ('filter_recording_ids', 'recording_ids')):
        if checked[key]:
            filters[name] = checked[key]
    # Diarization labels (SPEAKER_00) name nobody: such values are ignored.
    from src.utils.speaker_labels import real_speaker_names
    names = real_speaker_names(checked['filter_speakers'])
    if names:
        filters['speaker_names'] = names
    if checked['filter_date_from']:
        filters['date_from'] = checked['filter_date_from']
    if checked['filter_date_to']:
        filters['date_to'] = checked['filter_date_to']
    return filters


def agent_user_context(user, language=None):
    """What the agent's prompts and step labels need about the user, read in
    the request (the stream runs without it)."""
    from src.utils.assistant_name import assistant_name_of
    from src.utils.timezones import user_timezone
    return {
        'name': user.name if user.name else "the user",
        'title': user.job_title if user.job_title else "professional",
        'company': user.company if user.company else "their organization",
        'output_language': user.output_language if user.output_language else None,
        'timezone': user_timezone(user),
        'assistant_name': assistant_name_of(user),
        'language': language,
    }


def panel_history(message_history):
    """The recording panel's history for the model: role and content only,
    and after an answer whose citations were shown numbered, a system note
    with that numbering, so "tell me more about 3" means what the user saw
    (numbering continues through a chat). The note is a separate message,
    never part of the answer the model would imitate."""
    from src.services.inquire_agent import citation_lines
    out = []
    for msg in message_history or []:
        if not isinstance(msg, dict):
            continue
        role, content = msg.get('role'), msg.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        out.append({'role': role, 'content': content})
        lines = citation_lines(msg.get('sources')) if role == 'assistant' else []
        if lines:
            out.append({'role': 'system', 'content': (
                "Note about the assistant's previous answer (system record, never reproduce in answers): "
                f"citations were displayed to the user numbered: {'; '.join(lines)}. When the user refers "
                "to a bare number, they mean this numbering.")})
    return out


def sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def parse_sse(chunk):
    """The payloads in one SSE string: dicts, or the raw text of a data line
    that is not JSON (the incognito route's [DONE])."""
    payloads = []
    for line in (chunk or '').split('\n'):
        if not line.startswith('data: '):
            continue
        body = line[6:]
        try:
            payloads.append(json.loads(body))
        except ValueError:
            payloads.append(body)
    return payloads


# ---------------------------------------------------------------------------
# Segment citations in a stream
# ---------------------------------------------------------------------------

def recording_events(raw_events, segments=None):
    """The recording engine's SSE strings with `source` events added.

    Without segments (a plain-text transcript) the stream passes through
    unchanged, and so does any answer that cites timestamps such as
    [00:07:35] instead of segments: the panel keeps showing those as seek
    chips (the timestamp fallback).
    """
    cites = SegmentCitations(segments) if segments else None
    for chunk in raw_events:
        if cites is None:
            yield chunk
            continue
        for payload in parse_sse(chunk):
            if isinstance(payload, dict) and isinstance(payload.get('delta'), str):
                for event in cites.feed(payload['delta']):
                    yield sse(event)
                continue
            if isinstance(payload, dict) and ('end_of_stream' in payload or 'error' in payload):
                for event in cites.flush():
                    yield sse(event)
            yield sse(payload) if isinstance(payload, dict) else f"data: {payload}\n\n"
    if cites is not None:
        for event in cites.flush():
            yield sse(event)


# ---------------------------------------------------------------------------
# Library engine: retrieval stages as steps
# ---------------------------------------------------------------------------

def library_events(raw_events, language=None):
    """The library engine's SSE strings with each retrieval stage (`status`)
    also sent as a step, labelled in the user's language. A stage's step is
    marked done when the next stage, a tool step or the answer begins."""
    current = None
    number = 0
    for chunk in raw_events:
        for payload in parse_sse(chunk):
            if not isinstance(payload, dict):
                yield f"data: {payload}\n\n"
                continue
            stage = payload.get('status')
            label = None
            if stage is not None and 'message' in payload:
                label = rag_step_label(stage, language)
            if label:
                if current:
                    yield sse({'agent_step': dict(current, status='done')})
                number += 1
                current = {'id': f'stage-{number}', 'name': stage, 'tool': stage,
                           'icon': RAG_ICONS.get(stage, 'fa-gear'), 'label': label, 'status': 'running'}
                yield sse(dict(payload, message=label))
                yield sse({'agent_step': current})
                continue
            if current and any(k in payload for k in ('delta', 'agent_step', 'end_of_stream', 'error')):
                yield sse({'agent_step': dict(current, status='done')})
                current = None
            yield sse(payload)
