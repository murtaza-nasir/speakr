"""Filters for log records of libraries that carry request data.

httpx logs every request at INFO with its full URL. The transcription
connectors send the initial prompt and the hotwords of a recording as query
parameters, so that line put them in the default log. The connector and the
upload path logged the same parameters themselves; loggable_params replaces
the two user texts by their length.
"""

import logging


class NoQueryString(logging.Filter):
    """Drop the query string from the URL in httpx's "HTTP Request" record."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(self._clean(arg) for arg in args)
        return True

    @staticmethod
    def _clean(arg):
        query = getattr(arg, 'query', None)
        copy_with = getattr(arg, 'copy_with', None)
        if query and callable(copy_with):
            try:
                return copy_with(query=None)
            except Exception:
                return arg
        return arg


# Request parameters written by the user: logged as a length at INFO and in
# full at DEBUG only (discussion #603).
USER_TEXT_PARAMS = ('initial_prompt', 'hotwords')


def loggable_params(params):
    """Request or job parameters with user text replaced by its length."""
    if not isinstance(params, dict):
        return params
    return {key: (f"<{len(str(value))} characters>" if key in USER_TEXT_PARAMS and value else value)
            for key, value in params.items()}


def install():
    """Attach the filters. Safe to call again."""
    httpx_logger = logging.getLogger('httpx')
    if not any(isinstance(f, NoQueryString) for f in httpx_logger.filters):
        httpx_logger.addFilter(NoQueryString())


# --- names of people and files (privacy review B2) ----------------------------
#
# At INFO and above the log names users, groups, tags, speakers and recordings
# by id, and a file by its extension. A stored upload is named
# <timestamp>_<uuid>_<original filename>: its timestamp and the start of its
# uuid identify the file (the same value is in recording.audio_path) without
# the name the user gave it. Names are logged at DEBUG.

import os as _os
import re as _re

_STORED_NAME = _re.compile(r'^(\d{14})_([0-9a-f]{32})_')
_EXTENSION = _re.compile(r'^\.[A-Za-z0-9]{1,10}$')
_STORED_IN_TEXT = _re.compile(r'\d{14}_[0-9a-f]{32}_[^\s\'":,;]+')


def loggable_path(path):
    """A file for a log line at INFO or above, without the name a user chose."""
    if not path:
        return '<no file>'
    name = _os.path.basename(str(path).rstrip('/'))
    extension = _os.path.splitext(name)[1]
    extension = extension.lower() if _EXTENSION.match(extension) else ''
    stored = _STORED_NAME.match(name)
    if stored:
        return f"<stored file {stored.group(1)}_{stored.group(2)[:12]}{extension}>"
    return f"<file {extension}>" if extension else '<file>'


_KEY_TIMESTAMP = _re.compile(r'^(\d{14})_')


def loggable_locator(locator):
    """The stored media of a recording for a log line at INFO or above.

    A storage key is <prefix>/YYYY/MM/<recording id>/<timestamp>_<original
    filename>, and recording.audio_path holds it as a locator (local://<key>,
    s3://<bucket>/<key>) or, for an older row, as a path in the upload folder.
    The directory part, the timestamp and the extension identify the object;
    the name the user gave the file is left out. Not for a path whose
    directories are named by a user or an operator (a watch folder):
    loggable_path is for those.
    """
    if not locator:
        return '<no file>'
    text = str(locator).rstrip('/')
    directory, slash, name = text.rpartition('/')
    extension = _os.path.splitext(name)[1]
    extension = extension.lower() if _EXTENSION.match(extension) else ''
    stored = _STORED_NAME.match(name)
    stamped = _KEY_TIMESTAMP.match(name)
    if stored:
        name = f"{stored.group(1)}_{stored.group(2)[:12]}{extension}"
    elif stamped:
        name = f"{stamped.group(1)}{extension}"
    else:
        name = f"*{extension}"
    return f"<stored file {directory}{slash}{name}>"


def loggable_error(error, *paths):
    """The text of a file or FFmpeg error with the given files named as in loggable_path.

    FFmpeg and the operating system put the path of the file in their error
    text, and the path of a stored upload ends in the original filename.
    """
    text = str(error)
    # Any stored upload named in the text, also one not passed in.
    text = _STORED_IN_TEXT.sub(lambda m: loggable_path(m.group(0)), text)
    for path in paths:
        if not path:
            continue
        path = str(path)
        label = loggable_path(path)
        names = {path, _os.path.basename(path.rstrip('/')), _os.path.splitext(_os.path.basename(path.rstrip('/')))[0]}
        for name in sorted((n for n in names if len(n) >= 3), key=len, reverse=True):
            text = text.replace(name, label)
    return text
