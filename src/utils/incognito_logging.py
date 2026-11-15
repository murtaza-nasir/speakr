"""Incognito requests log nothing but metadata, at any log level.

While an incognito request runs (upload, chat, summary), the code it shares
with ordinary recordings (the LLM service, titling, summarization, the
transcription connectors, FFmpeg helpers) and the libraries under it (openai,
httpx, httpcore) keep logging as they always do, and at DEBUG those records
hold transcripts, prompts, replies and provider error bodies.

The rule is enforced in one place and is deny by default:

* ``incognito_route`` marks a view as incognito. A context variable is set
  for the whole view call and for every step of a streamed response.
* ``install()`` wraps the log record factory of the process. While the
  context variable is set, every record is replaced, before any handler or
  filter sees it, by a line that holds its length and where it was logged.
  The original message, its arguments, the traceback and the stack text are
  dropped. A handler added later (a file handler, pytest's caplog, gunicorn's)
  gets the replaced record too, because the record is replaced when it is
  created.
* Only records of the ``speakr.incognito`` logger (``incognito_log``) pass
  unchanged. A line written to it must hold metadata only: user id, sizes,
  lengths, counts, durations, language, model and connector names, status
  codes, and exceptions through ``describe_error``.

So a log line added later to shared code, or by a library upgrade, is
withheld in an incognito request without anyone remembering to mark it.

The incognito path runs in the request thread. Work handed to another thread
does not inherit a context variable: wrap the callable with ``carry_context``
when that is ever needed.
"""

import contextvars
import functools
import logging
import os
import re
from contextlib import contextmanager

# Moved to a neutral module: ordinary requests describe provider errors the
# same way at INFO and above. Imported here for the callers of this module.
from src.utils.error_text import describe_error  # noqa: F401

SAFE_LOGGER_NAME = 'speakr.incognito'

# Metadata-only lines of the incognito path. Never pass content, a filename
# given by the user, or str(exception) to this logger.
incognito_log = logging.getLogger(SAFE_LOGGER_NAME)

_active = contextvars.ContextVar('speakr_incognito_request', default=False)
_installed = False


def incognito_active():
    """Whether the current context belongs to an incognito request."""
    return _active.get()


@contextmanager
def incognito_context():
    """Mark the enclosed code as part of an incognito request."""
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)


def carry_context(func):
    """Return func bound to the current context, for a thread or an executor.

    A new thread starts with an empty context, so a callable handed to one
    from an incognito request would log as an ordinary request.
    """
    context = contextvars.copy_context()

    @functools.wraps(func)
    def bound(*args, **kwargs):
        return context.copy().run(func, *args, **kwargs)
    return bound


def short_code(value):
    """A request value that is logged only when it is a short code (a language)."""
    value = '' if value is None else str(value)
    return value if re.fullmatch(r'[A-Za-z0-9_\-]{0,16}', value) else 'other'


def _withheld(msg, args, exc_info, fn, lno):
    try:
        text = str(msg)
        if args:
            text = text % args
        length = len(text)
    except Exception:
        length = 0
    line = f"[Incognito] log record withheld ({length} characters, {os.path.basename(str(fn))}:{lno})"
    error = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
    if isinstance(error, BaseException):
        line += f" [{describe_error(error)}]"
    return line


def install():
    """Wrap the log record factory of the process. Safe to call again."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(name, level, fn, lno, msg, args, exc_info, func=None, sinfo=None, **kwargs):
        if _active.get() and name != SAFE_LOGGER_NAME and not name.startswith(SAFE_LOGGER_NAME + '.'):
            msg = _withheld(msg, args, exc_info, fn, lno)
            args, exc_info, sinfo = None, None, None
        return previous(name, level, fn, lno, msg, args, exc_info, func, sinfo, **kwargs)

    logging.setLogRecordFactory(factory)
    _installed = True


def _iterate_in_context(iterable):
    """Run every step of a streamed response body as part of the request."""
    iterator = iter(iterable)
    try:
        while True:
            with incognito_context():
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            yield item
    finally:
        close = getattr(iterator, 'close', None)
        if close is not None:
            with incognito_context():
                close()


def incognito_route(view):
    """Mark a view as incognito: it and its streamed response log metadata only.

    Placed directly above the view function, under login_required. An
    exception the view lets through is answered with a fixed message here,
    so that Flask does not log its text and traceback after the context ends.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        from flask import current_app, jsonify
        from werkzeug.exceptions import HTTPException
        with incognito_context():
            try:
                response = current_app.make_response(view(*args, **kwargs))
            except HTTPException:
                raise
            except Exception as e:
                incognito_log.error(f"[Incognito] Unhandled error: {describe_error(e)}")
                response = jsonify({'incognito': True, 'error': 'An unexpected error occurred.'})
                response.status_code = 500
                return response
        if response.is_streamed:
            response.response = _iterate_in_context(response.response)
        return response
    wrapper.incognito_route = True
    return wrapper
