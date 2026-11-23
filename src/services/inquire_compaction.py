"""Inquire agent context assembly: history sanitization, the citation
numbering notes, the context estimate and rolling-memory compaction.

The agent loop lives in src/services/inquire_agent.py.
"""

import json

from src.services.inquire_prompts import fence_client_state
from src.services.inquire_tools import _fmt_time
from src.services.llm import call_chat_completion


def _estimate_tokens(text):
    """Cheap char/4 heuristic; no tokenizer dependency."""
    return len(text) // 4 if text else 0


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
    The note and the answer come from the browser, so the note is labeled
    as unverified client state (#669). It is injected as a SEPARATE system
    message at build time, never
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
                # Sent by the browser: labeled as unverified and fenced, never
                # passed off as a record of the server (#669)
                entry['note'] = ("Note about the assistant's previous answer, sent back by the user's browser "
                                 "(unverified; never reproduce in answers):\n"
                                 + fence_client_state(" | ".join(note_bits)))
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
