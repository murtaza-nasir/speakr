"""Limits on what a client may send to the assistant's chat routes.

Every route that answers a question checks the body here first, so the Ask
page (POST /api/inquire/chat), POST /api/v1/inquire, the recording panel
(POST /chat), the incognito chat and POST /api/v1/recordings/<id>/chat refuse
the same input before any model call.

The question (#634) is at most MAX_QUESTION_CHARS characters: len() of the
string, not its UTF-8 bytes. A typed or dictated question is far shorter; a
pasted transcript or document is longer, and the transcript already reaches
the model by other means. A longer question is refused with 400, never cut:
a question that loses its last clause can ask something else.

The history (#633) is a list of {role, content} objects, the role user or
assistant and the content a string; anything else is refused with 400. A
well-formed history is never refused for its length: the bundled chats resend
the whole conversation with every question, so a refusal would end a long
conversation. The oldest entries are dropped instead, keeping the newest
MAX_HISTORY_MESSAGES entries (20 questions and answers) and at most
MAX_HISTORY_CHARS characters of content (about 15,000 tokens by the char/4
estimate of inquire_compaction). A follow-up question refers to the newest
turns.
"""

from src.utils.localization import api_error, format_number, translate

MAX_QUESTION_CHARS = 10_000
MAX_HISTORY_MESSAGES = 40
MAX_HISTORY_CHARS = 60_000
# The conversation memory the Ask page sends back: the summary the agent
# wrote when it compacted the conversation. It is the server's own text, so
# a value that is not a string is ignored and a longer one is cut.
MAX_MEMORY_CHARS = 20_000


def bounded_memory(raw):
    """The conversation memory a client sent back, as a string of at most
    MAX_MEMORY_CHARS characters; '' for anything that is not a string."""
    if not isinstance(raw, str):
        return ''
    return raw[:MAX_MEMORY_CHARS]


def question_too_long(value):
    """True when the question is a string longer than the shared limit."""
    return isinstance(value, str) and len(value) > MAX_QUESTION_CHARS


def question_error(value, v1=False):
    """The 400 body for a question over the limit, or None when it fits.

    The web routes answer the code message_too_long; API v1 answers
    invalid_parameter, as for its other bad parameters. Both say the same
    localized sentence.
    """
    if not question_too_long(value):
        return None
    limit = format_number(MAX_QUESTION_CHARS)
    if v1:
        return {'error': translate('server.apiError.message_too_long', max=limit), 'code': 'invalid_parameter'}
    return api_error('message_too_long', max=limit)


def checked_question(raw, missing, v1=False):
    """(question, None) for a usable question, stripped of surrounding
    spaces; (None, 400 body) otherwise. `missing` is the body of the
    route for no question: a value that is not a string, or one that is
    blank once stripped ("   ", "\\n"), is none (#670). A NUL character is
    refused (it cannot be stored or sent on), and so is a question over
    MAX_QUESTION_CHARS (question_error). The one check of every route
    that takes a question."""
    if not isinstance(raw, str) or not raw.strip():
        return None, missing
    question = raw.strip()
    if '\x00' in question:
        if v1:
            return None, {'error': translate('server.apiError.message_contains_nul'), 'code': 'invalid_parameter'}
        return None, api_error('message_contains_nul')
    too_long = question_error(question, v1=v1)
    if too_long:
        return None, too_long
    return question, None


def history_is_valid(raw):
    """True for a list of objects, each with a role of user or assistant and
    string content. Other keys (the Ask page's activity and sources) are
    allowed and read by the engines."""
    return isinstance(raw, list) and all(
        isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)
        for m in raw)


def bounded_history(history):
    """The newest entries of a valid history within MAX_HISTORY_MESSAGES and
    MAX_HISTORY_CHARS characters of content, oldest dropped first. When
    entries were dropped and the kept part starts with an answer, that answer
    goes too, so the model sees a question first. A history within both
    limits is returned unchanged."""
    kept = history[-MAX_HISTORY_MESSAGES:]
    total, start = 0, len(kept)
    for i in range(len(kept) - 1, -1, -1):
        total += len(kept[i]['content'])
        if total > MAX_HISTORY_CHARS:
            break
        start = i
    kept = kept[start:]
    if len(kept) < len(history) and kept and kept[0]['role'] == 'assistant':
        kept = kept[1:]
    return kept


def checked_history(raw, v1=False):
    """(history, None) for a usable history, bounded with bounded_history();
    (None, 400 body) when it is not a list of messages. A missing or null
    history is an empty one."""
    if raw is None:
        return [], None
    if not history_is_valid(raw):
        if v1:
            return None, {'error': translate('server.apiError.invalid_message_history'),
                          'code': 'invalid_parameter'}
        return None, api_error('invalid_message_history')
    return bounded_history(raw), None


def history_trimmed_event(raw, kept, language=None):
    """The SSE step that says earlier messages were left out (#633), or ''
    when the history was used whole. Shown in the answer's activity list."""
    if not isinstance(raw, list) or not isinstance(kept, list) or len(kept) >= len(raw):
        return ''
    import json
    dropped = len(raw) - len(kept)
    step = {'id': 'history-trimmed', 'name': 'history_trimmed', 'tool': 'history_trimmed',
            'icon': 'fa-scissors', 'status': 'done',
            'label': translate('server.assistant.step.historyTrimmed', language, count=dropped)}
    return f"data: {json.dumps({'agent_step': step})}\n\n"

