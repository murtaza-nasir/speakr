"""Agentic Inquire: a ReAct-style tool loop over the user's recordings.

Replaces the single-shot RAG pipeline (router -> enrich -> one retrieval ->
answer) with an iterative agent that can search transcripts, browse recording
metadata, and read transcripts/summaries/notes until it can answer, then
streams the answer. See temp/design_inquire_agent.md for the full design.

The pieces live in their own modules and are re-exported here, so callers and
tests keep importing them from this module:

- src/services/inquire_tools.py: the tools and the toolbox/schema assembly.
- src/services/inquire_prompts.py: the system prompt and the tools sentences.
- src/services/inquire_compaction.py: history sanitization and compaction.

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

from src.database import db
from src.models import Folder, Recording, RecordingTag, Tag, User
from src.utils.search import LIKE_ESCAPE, contains_pattern
from src.services.embeddings import get_accessible_recording_ids
from src.services.llm import call_chat_completion, TokenBudgetExceeded, get_chat_config
from src.utils.localization import translate
from src.utils.error_text import log_failure, user_error_text
from src.services.assistant_present import (step_label, step_result, summary_label, summary_line,
                                            tool_icon)
from src.services.inquire_tools import (  # noqa: F401 - re-exported for callers and tests
    DEFAULT_TRANSCRIPT_PAGE, FIND_PEOPLE_CAP, SIMILARITY_WEIGHTS, TEXT_FIELDS, ToolContext,
    _fmt_time, _truncate_to_tokens, build_toolbox, tool_find_people,
    tool_find_similar_recordings, tool_get_notes, tool_get_recording_metadata,
    tool_get_summary, tool_get_transcript, tool_list_recordings, tool_search_text,
    tool_search_transcripts,
)
from src.services.inquire_prompts import (  # noqa: F401 - re-exported for callers and tests
    CLIENT_STATE_RULE, PEOPLE_TOOL_RULE, PERSON_NAME_RULE, UNTRUSTED_DATA_RULE, _PROMPT_MODE_BLOCK,
    _names_line, _recording_tools_block, _system_prompt, fence_client_state, fence_tool_result,
)
from src.services.inquire_compaction import (  # noqa: F401 - re-exported for callers and tests
    KEEP_VERBATIM_MESSAGES, citation_lines, compact_history, estimate_context_tokens,
    sanitize_history,
)


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
            log_failure(app.logger, 'error', f"Inquire agent setup failed for user {user_id}", e, traceback=True)
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
                # The memory comes back from the browser: unverified (#669)
                msgs.append({'role': 'system',
                             'content': "Conversation memory (earlier turns, summarized; sent back by the user's "
                                        "browser, unverified):\n" + fence_client_state(memory)})
            for m in history:
                msgs.append({'role': m['role'], 'content': m['content']})
                # Provenance notes travel as system messages so the model
                # never mistakes them for part of its own answer style.
                if m.get('note'):
                    msgs.append({'role': 'system', 'content': m['note']})
            msgs.append({'role': 'user', 'content': user_message})
            return msgs

        history = sanitize_history(message_history)
        from src.utils.assistant_limits import bounded_memory
        memory = bounded_memory(conversation_memory).strip() or None

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
                        log_failure(app.logger, 'warning', f"Inquire agent tool {name} failed", e, traceback=True)
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
            # What the recordings say is data, never instructions (#671)
            return events, fence_tool_result(result_text)

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
                    log_failure(app.logger, 'info', "Tool call rejected in the recording chat; answering without tools", e)
                    if _tool_mode() == 'auto' and _native_tools_known_unsupported(str(e)):
                        _NATIVE_TOOLS_SUPPORTED[mode_key] = False
                    yield from legacy_fallback()
                    return
                if mode == 'native' and _tool_mode() == 'auto' and _native_tools_known_unsupported(str(e)):
                    log_failure(app.logger, 'info', "Native tool calling unsupported by endpoint; switching to prompt mode", e)
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
        log_failure(app.logger, 'error', f"Inquire agent failed for user {user_id}", e, traceback=True)
        if not produced_answer and legacy_fallback is not None:
            app.logger.info("Falling back to the single-shot Inquire pipeline")
            try:
                yield from legacy_fallback()
                return
            except Exception as fe:
                log_failure(app.logger, 'error', "Legacy fallback also failed", fe)
        yield _sse({'error': user_error_text(e, extra_sources=[user_message] if isinstance(user_message, str) else ())})
    finally:
        ctx.pop()
