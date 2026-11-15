"""Error text of outside services: what is logged and what is stored.

An error of a transcription service or of an LLM provider can quote what was
sent to it: the prompt, the transcript, the filename. Two rules follow.

**The log.** At INFO and above an error of a provider is logged as metadata:
its class, the provider, the HTTP status, a short error code and the length
of its text (``describe_error``). The full text is logged at DEBUG only
(``log_failure``).

**What the user sees.** The text stored in ``processing_job.error_message``,
``recording.error_message``, the error shown in place of a transcript or a
summary, the failure webhook and the failure email is built by
``user_error_text``. Exactly this is kept:

* the wording around the provider's answer that Speakr wrote itself (which
  step failed, the HTTP status);
* from a structured error body, the provider's own message field (``detail``,
  ``error.message``, ``message``, ``error``, ``msg``), cut to
  ``PROVIDER_MESSAGE_MAX_CHARS``, with its short ``code`` and ``type``. The
  other fields of the body are dropped;
* an unstructured body (a traceback, a plain line) as the connector summarized
  it (``src/utils/error_body.py``: bounded, credentials and directories
  replaced).

And this is removed (``scrub_request_echo``): every run of
``ECHO_MIN_RUN`` or more characters, and every quoted string of
``ECHO_MIN_QUOTED`` or more characters, that also occurs in the content of
the request (the transcript, the summary, the notes, the title, the
participants, the original filename, the initial prompt, the hotwords).
The result is cut to ``ERROR_MESSAGE_MAX_CHARS``.
"""

import ast
import json
import logging
import os
import re

from src.utils.error_formatting import (ERROR_MESSAGE_MAX_CHARS, TRANSCRIPTION_ERROR_PREFIXES,
                                         sanitize_error_message, truncate_middle)

PROVIDER_MESSAGE_MAX_CHARS = 300
ECHO_MIN_RUN = 20
ECHO_MIN_QUOTED = 4
REMOVED = '[content removed]'

_CODE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')
_MESSAGE_KEYS = ('message', 'detail', 'msg', 'error_description', 'description', 'reason')
_QUOTED = re.compile(r'"([^"\n]{%d,})"|\'([^\'\n]{%d,})\'|`([^`\n]{%d,})`|“([^”\n]{%d,})”'
                     % ((ECHO_MIN_QUOTED,) * 4))


def describe_error(error):
    """An exception as class, provider, status, code and message length.

    The message of a provider error can quote the transcript, the prompt or
    the reply, so it is never part of the description.
    """
    parts = [type(error).__name__]
    provider = getattr(error, 'provider', None)
    if isinstance(provider, str) and _CODE.match(provider):
        parts.append(f"provider={provider}")
    status = getattr(error, 'status_code', None)
    if status is None:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
    if isinstance(status, int) and not isinstance(status, bool):
        parts.append(f"status={status}")
    code = getattr(error, 'code', None)
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(f"code={code}")
    elif isinstance(code, str) and _CODE.match(code):
        parts.append(f"code={code}")
    try:
        length = len(str(error))
    except Exception:
        length = 0
    parts.append(f"length={length}")
    return ' '.join(parts)


def log_failure(logger, level, message, error, traceback=False):
    """Log a failure whose error text can quote content.

    At ``level`` (INFO or above): ``message`` and the description of the
    error. At DEBUG: the same message with the full error text, and the
    traceback when asked. ``message`` must name what failed and for which id,
    and hold no content itself.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper())
    # stacklevel: the record names the caller, not this helper.
    logger.log(level, f"{message}: {describe_error(error)}", stacklevel=2)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"{message}: full error text: {error}", stacklevel=2,
                     exc_info=(type(error), error, error.__traceback__) if traceback else None)


def describe_body(status, body):
    """An error body as its status and length, for a log line at INFO or above."""
    return f"status={status} body={len(body or '')} characters"


# --- the provider's short message ---------------------------------------------

def _short(value, limit=PROVIDER_MESSAGE_MAX_CHARS):
    text = re.sub(r'\s+', ' ', str(value)).strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


def _message_of(data):
    """(message, code, type) of a parsed error body, or None when it has no message."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None
    code = data.get('code')
    kind = data.get('type')
    inner = data.get('error')
    message = None
    if isinstance(inner, dict):
        found = _message_of(inner)
        if found:
            message, inner_code, inner_kind = found
            code = inner_code if inner_code is not None else code
            kind = inner_kind if inner_kind is not None else kind
    elif isinstance(inner, str) and inner.strip():
        message = inner
    if message is None:
        for key in _MESSAGE_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                message = value
                break
            if isinstance(value, (list, dict)) and value:
                # FastAPI validation errors: detail is a list of {msg, loc, type}
                found = _message_of(value)
                if found:
                    message, inner_code, inner_kind = found
                    code = inner_code if inner_code is not None else code
                    kind = inner_kind if inner_kind is not None else kind
                    break
    if message is None:
        return None
    return message, code, kind


def _label(value):
    if isinstance(value, bool) or value is None:
        return None
    value = str(value)
    return value if _CODE.match(value) else None


def _parse_object(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    try:
        # The openai library prints the body as a Python dict.
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def provider_message(text):
    """Replace a structured error body inside ``text`` by its message field.

    "ASR request failed with status 400: {"detail": "Invalid model size",
    "request": {...}}" becomes "ASR request failed with status 400: Invalid
    model size". Text without a structured body with a message is returned
    unchanged.
    """
    if not text:
        return text
    text = str(text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
        return text
    found = _message_of(_parse_object(text[start:end + 1]))
    if not found:
        return text
    message, code, kind = found
    short = _short(message)
    labels = [f"{name} {value}" for name, value in (('code', _label(code)), ('type', _label(kind))) if value]
    if labels:
        short += f" ({', '.join(labels)})"
    return text[:start] + short + text[end + 1:]


# --- echoes of the request content ---------------------------------------------

def _plain(value):
    return re.sub(r'\s+', ' ', value).strip()


def _source_texts(sources):
    """The request content as plain strings, with stored JSON transcripts opened."""
    texts = []
    for source in sources or ():
        if not source or not isinstance(source, str):
            continue
        texts.append(_plain(source))
        stripped = source.lstrip()
        if stripped.startswith(('[', '{')):
            try:
                data = json.loads(source)
            except (ValueError, TypeError):
                continue
            parts = []

            def walk(node):
                if isinstance(node, str):
                    parts.append(node)
                elif isinstance(node, dict):
                    for item in node.values():
                        walk(item)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)
            walk(data)
            texts.extend(_plain(part) for part in parts if part.strip())
    return [t for t in texts if t]


def scrub_request_echo(text, sources):
    """Remove from ``text`` what it repeats of the request content.

    A run of ECHO_MIN_RUN or more characters that occurs in a source is
    replaced by REMOVED, and so is a quoted string of ECHO_MIN_QUOTED or more
    characters that occurs in a source, and a whole source that is shorter
    than a run (a title, a filename, a hotword) where it stands as a word.
    """
    if not text:
        return text
    texts = _source_texts(sources)
    if not texts:
        return text
    text = _plain(str(text))
    lowered_sources = [t.lower() for t in texts]

    def occurs(piece):
        piece = piece.lower()
        return any(piece in source for source in lowered_sources)

    spans = []
    # Quoted strings: a provider quotes the offending input.
    for match in _QUOTED.finditer(text):
        group = next(i for i in range(1, 5) if match.group(i) is not None)
        if occurs(match.group(group).strip()) and match.group(group).strip():
            spans.append((match.start(group), match.end(group)))
    # Whole short sources (a title, a filename and its stem, hotwords).
    for source in texts:
        if ECHO_MIN_QUOTED <= len(source) < ECHO_MIN_RUN:
            for match in re.finditer(r'(?<!\w)' + re.escape(source) + r'(?!\w)', text, re.I):
                spans.append((match.start(), match.end()))
    # Long runs.
    i, n = 0, len(text)
    while i + ECHO_MIN_RUN <= n:
        if occurs(text[i:i + ECHO_MIN_RUN]):
            j = i + ECHO_MIN_RUN
            while j < n and occurs(text[i:j + 1]):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    if not spans:
        return text
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, last = [], 0
    for start, end in merged:
        out.append(text[last:start])
        out.append(REMOVED)
        last = end
    out.append(text[last:])
    return ''.join(out)


def _is_stored_error(value):
    """A transcript or summary field that holds the text of an earlier failure.

    Such a field is not content: the error being stored now often repeats it,
    and must not be removed for that.
    """
    return value.lstrip().startswith(TRANSCRIPTION_ERROR_PREFIXES + ('[Summary generation failed',))


def recording_sources(recording, extra=()):
    """The content of a recording that a provider could repeat in an error."""
    sources = list(extra or ())
    if recording is None:
        return sources
    for name in ('transcription', 'summary', 'notes', 'title', 'participants', 'original_filename',
                 'resolved_initial_prompt', 'resolved_hotwords'):
        value = getattr(recording, name, None)
        if isinstance(value, str) and value.strip() and not _is_stored_error(value):
            sources.append(value)
            if name == 'original_filename':
                stem = os.path.splitext(value)[0]
                if stem and stem != value:
                    sources.append(stem)
            if name == 'resolved_hotwords':
                sources.extend(word.strip() for word in re.split(r'[,\n]', value) if word.strip())
    return sources


def user_error_text(error, recording=None, extra_sources=(), limit=ERROR_MESSAGE_MAX_CHARS):
    """The text of a failure as it is stored, shown, and sent by webhook and email.

    See the module docstring for exactly what is kept and what is removed.
    """
    text = provider_message(str(error) if error is not None else '')
    text = scrub_request_echo(text, recording_sources(recording, extra_sources))
    # Both ends are kept: a traceback names the exception on its last line.
    text = truncate_middle(_plain(text or ''), min(limit, ERROR_MESSAGE_MAX_CHARS))
    return sanitize_error_message(text) or ''
