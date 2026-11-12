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

recording_events() adds `source` events to the recording engine's stream;
library_events() turns the retrieval stages (`status`) into steps. The event
names the Ask page and API v1 already read are kept; `source` is new.

tools_for_surface() is the tool list for each place the assistant answers
(decision 2): an incognito recording gets none, an open recording the library
tools, the Ask page all of them.
"""

import json
import re

from src.services.assistant_present import RAG_ICONS, rag_step_label

LIBRARY_TOOLS = ('search_transcripts', 'list_recordings', 'get_transcript', 'get_recording_metadata',
                 'get_summary', 'get_notes')

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


def filters_from_request(data):
    """The library filters of an Ask page chat request, in the agent's form:
    the keys of its filter column (filter_tags, filter_folders,
    filter_speakers, filter_recording_ids, filter_date_from, filter_date_to).
    The filters are a ceiling: resolve_allowed_recording_ids intersects them
    with access. The recording panel has no filters."""
    from datetime import datetime
    data = data or {}
    filters = {}
    for key, name in (('filter_tags', 'tag_ids'), ('filter_folders', 'folder_ids'),
                      ('filter_speakers', 'speaker_names'), ('filter_recording_ids', 'recording_ids')):
        if data.get(key):
            filters[name] = data[key]
    if data.get('filter_date_from'):
        filters['date_from'] = datetime.fromisoformat(data['filter_date_from']).date()
    if data.get('filter_date_to'):
        filters['date_to'] = datetime.fromisoformat(data['filter_date_to']).date()
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

_CITE = re.compile(r'\[S(\d+)(?:\s+"([^"\]]*)")?\]')
# What the start of an unfinished citation can look like: "[", "[S", "[S12",
# "[S12 ", '[S12 "some wor', '[S12 "some words"'.
_PARTIAL = re.compile(r'\[(?:S(?:\d+(?:\s+(?:"[^"\]]*"?)?)?)?)?\Z')
_MAX_HELD = 400


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().lower()


class SegmentCitations:
    """Watches streamed answer text for [S12 "quote"] citations.

    feed() returns the payloads to send for a piece of text: a `source` event
    for each new citation, then the text itself as a `delta`. The text keeps
    the model's own markers, so the history the browser sends back is what
    the model wrote; the browser shows each marker as its number. A marker
    split across chunks is held back until it is complete.

    Numbering and quote checks follow recording_chat.parse_sources.
    """

    def __init__(self, segments):
        self.by_index = {seg['index']: seg for seg in segments or []}
        self.numbers = {}
        self.count = 0
        self.markers = set()
        self.held = ''

    def _source(self, match):
        index, quote = int(match.group(1)), match.group(2)
        key = (index, _norm(quote))
        seg = self.by_index.get(index)
        if key not in self.numbers:
            self.count += 1
            self.numbers[key] = self.count
        return {
            'n': self.numbers[key],
            'marker': match.group(0),
            'segment_index': index if seg else None,
            'start_time': seg['start_time'] if seg else None,
            'end_time': seg['end_time'] if seg else None,
            'speaker': seg['speaker'] if seg else None,
            'quote': quote,
            'verified': bool(seg and quote and _norm(quote) in _norm(seg['text'])),
        }

    def feed(self, text):
        buf = self.held + (text or '')
        self.held = ''
        out, events = [], []
        while buf:
            i = buf.find('[')
            if i == -1:
                out.append(buf)
                break
            out.append(buf[:i])
            rest = buf[i:]
            match = _CITE.match(rest)
            if match:
                if match.group(0) not in self.markers:
                    self.markers.add(match.group(0))
                    if out and ''.join(out):
                        events.append({'delta': ''.join(out)})
                        out = []
                    events.append({'source': self._source(match)})
                out.append(match.group(0))
                buf = rest[match.end():]
                continue
            if _PARTIAL.match(rest) and len(rest) < _MAX_HELD:
                self.held = rest
                break
            out.append('[')
            buf = rest[1:]
        text_out = ''.join(out)
        if text_out:
            events.append({'delta': text_out})
        return events

    def flush(self):
        held, self.held = self.held, ''
        return [{'delta': held}] if held else []


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
