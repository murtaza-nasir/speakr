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


# --- names of people and files (privacy review B2, #620) ----------------------
#
# At INFO and above the log names users, groups, tags, speakers and recordings
# by id. A file is named by an identifier, never by the name a user gave it
# and never by its extension alone:
#
#   staged upload   <timestamp>_<uuid>_<name>     <stored file 20261115101500_0123456789ab.mp3>
#   stored media    <timestamp>_<name>            <stored file 20261115101500.mp3 of recording 42>
#   convert staging <uuid>_<timestamp>_<name>     <stored file 20261115101500_0123456789ab.mp3>
#                   <uuid>_<other name>           <staged file 0123456789ab.mp3>
#   S3 download     speakr_s3_<random>.<ext>      <s3 copy ab12cd34.mp3 of recording 42>
#   any other file  watch folder, export, temp    <file 4f3a1c9b2d.m4a>
#
# The last form is a keyed digest (HMAC-SHA-256 with a key derived from
# SECRET_KEY) of the file name: the same name has the same label in every
# worker process and after a restart, and a guessed name cannot be confirmed
# without the key. Names are logged at DEBUG.

import contextlib as _contextlib
import contextvars as _contextvars
import hashlib as _hashlib
import hmac as _hmac
import os as _os
import re as _re

_STORED_NAME = _re.compile(r'^\.?(\d{14})_([0-9a-f]{32})_')
_KEY_TIMESTAMP = _re.compile(r'^\.?(\d{14})_')
_CONVERT_STAGED = _re.compile(r'^([0-9a-f]{32})_(?:(\d{14})_)?')
_S3_COPY = _re.compile(r'^speakr_s3_([A-Za-z0-9_]{8})')
_CHUNK = _re.compile(r'_chunk_(\d{1,6})(?:\.[A-Za-z0-9]{1,10})?$')
_EXTENSION = _re.compile(r'^\.[A-Za-z0-9]{1,10}$')
# <prefix>/YYYY/MM/<recording id>/<file>: the directories of a storage key.
_KEY_DIRECTORY = _re.compile(r'(?:^|[/\\])\d{4}[/\\]\d{2}[/\\](\d{1,12})$')

_DIGEST_LENGTH = 10
_label_key = None
_fallback_key = None
_private_dirs = None
_private_pattern = None


def configure(secret_key=None, private_dirs=None):
    """Set the key of the file digests and the folders that hold user files.

    Called once at application start with SECRET_KEY, so that every worker
    process labels a file the same way, also after a restart. The key used is
    derived from the secret; the secret is not kept. private_dirs are the
    upload, staging, watch and export folders: a path below one of them is
    redacted in error text even when its name has spaces.
    """
    global _label_key, _private_dirs, _private_pattern
    if secret_key:
        if not isinstance(secret_key, bytes):
            secret_key = str(secret_key).encode('utf-8', 'surrogatepass')
        _label_key = _hmac.new(secret_key, b'speakr log file label v1', _hashlib.sha256).digest()
    if private_dirs is not None:
        _private_dirs = tuple(private_dirs)
        _private_pattern = None


def _digest_key():
    """The configured key, or a random key of this process: without the
    application (a unit test, a script) labels are stable for the process
    only and are still not derivable from the file name."""
    global _fallback_key
    if _label_key is not None:
        return _label_key
    if _fallback_key is None:
        _fallback_key = _os.urandom(32)
    return _fallback_key


def _name_digest(name):
    data = str(name).encode('utf-8', 'surrogatepass')
    return _hmac.new(_digest_key(), data, _hashlib.sha256).hexdigest()[:_DIGEST_LENGTH]


def _extension_of(name):
    extension = _os.path.splitext(name)[1]
    return extension.lower() if _EXTENSION.match(extension) else ''


def _split(path):
    """(directory, base name) of a path with either kind of separator."""
    text = str(path).rstrip('/\\')
    cut = max(text.rfind('/'), text.rfind('\\'))
    return (text[:cut] if cut >= 0 else ''), text[cut + 1:]


def _identified(name):
    """(kind, identifier) for a name of one of the known schemes, else None."""
    stored = _STORED_NAME.match(name)
    if stored:
        return 'stored file', f"{stored.group(1)}_{stored.group(2)[:12]}"
    stamped = _KEY_TIMESTAMP.match(name)
    if stamped:
        return 'stored file', stamped.group(1)
    staged = _CONVERT_STAGED.match(name)
    if staged:
        if staged.group(2):
            return 'stored file', f"{staged.group(2)}_{staged.group(1)[:12]}"
        return 'staged file', staged.group(1)[:12]
    copy = _S3_COPY.match(name)
    if copy:
        return 's3 copy', copy.group(1)
    return None


_scope_recording = _contextvars.ContextVar('speakr_log_recording', default=None)


@_contextlib.contextmanager
def recording_scope(recording_id):
    """Every file label made in the block ends with "of recording <id>".

    Set around a job of the queue: the temporary copies, converted files and
    chunks of the job have no id in their names, and an S3 download copy has
    no trace of the recording at all.
    """
    token = _scope_recording.set(recording_id)
    try:
        yield
    finally:
        _scope_recording.reset(token)


def _suffixes(name, directory, recording_id):
    chunk = _CHUNK.search(name)
    text = f" chunk {int(chunk.group(1))}" if chunk else ''
    if recording_id is None:
        in_key = _KEY_DIRECTORY.search(directory)
        if in_key and _KEY_TIMESTAMP.match(name):
            recording_id = in_key.group(1)
    if recording_id is None:
        recording_id = _scope_recording.get()
    if recording_id is not None:
        text += f" of recording {recording_id}"
    return text


def loggable_path(path, recording_id=None):
    """A file for a log line at INFO or above, without the name a user chose.

    With recording_id the label ends with "of recording <id>"; a stored file
    below <prefix>/YYYY/MM/<id>/ has it without being told.
    """
    if not path:
        return '<no file>'
    directory, name = _split(path)
    extension = _extension_of(name)
    known = _identified(name)
    kind, identifier = known if known else ('file', _name_digest(name))
    return f"<{kind} {identifier}{extension}{_suffixes(name, directory, recording_id)}>"


def loggable_locator(locator):
    """The stored media of a recording for a log line at INFO or above.

    A storage key is <prefix>/YYYY/MM/<recording id>/<timestamp>_<original
    filename>, and recording.audio_path holds it as a locator (local://<key>,
    s3://<bucket>/<key>) or, for an older row, as a path in the upload folder.
    The directory part, the timestamp and the extension identify the object;
    the name the user gave the file is left out, and a name of no known
    scheme is replaced by its keyed digest. Not for a path whose directories
    are named by a user or an operator (a watch folder): loggable_path is
    for those.
    """
    if not locator:
        return '<no file>'
    text = str(locator).rstrip('/')
    directory, slash, name = text.rpartition('/')
    extension = _extension_of(name)
    known = _identified(name)
    identifier = known[1] if known else _name_digest(name)
    return f"<stored file {directory}{slash}{identifier}{extension}>"


# --- file names in error text ------------------------------------------------
#
# FFmpeg, the operating system and the storage client put the path of the file
# in their error text. Every step below reads the text once: no pattern can
# run back over a long text (a megabyte is cleaned in about a second at most).

_LABEL = _re.compile(r'<(?:stored file|staged file|s3 copy|file|no file)[^<>\n]{0,300}>')
_QUOTED = _re.compile(
    r"""(\[(?:Errno|WinError) \d+\][^'"\n]{0,120}: |-> )?(?<![A-Za-z0-9])"""
    r"""(?:'([^'\n]{1,1024})'|"([^"\n]{1,1024})")""")
_TOKEN = _re.compile(r"[^\s'\"<>\x00]+")
_PATH_START = _re.compile(r'(?:^|(?<=[=:(\[{,;]))(?:~?/|\.{1,2}/|[A-Za-z]:[\\/])')
_SEGMENT_SCHEME = _re.compile(r'(?:^|(?<=[/\\=:(\[{,;]))\.?(?:\d{14}_|[0-9a-f]{32}_|speakr_s3_[A-Za-z0-9_]{8})')
_URL_NAME = _re.compile(r'(?<=/)\.?(?:\d{14}_|[0-9a-f]{32}_)[^/?#]+')
_TRAILING = '.,;:)]}'
# Folders of the operating system and of installed software: a path below
# them names no user file and is part of what makes an error readable.
_SYSTEM_DIRS = ('/usr/', '/lib/', '/lib64/', '/bin/', '/sbin/', '/etc/', '/proc/', '/sys/', '/dev/', '/run/')
# Source and library files, as a traceback names them.
_CODE_EXTENSIONS = ('.py', '.pyc', '.pyx', '.so')
# A quoted name without a directory is taken for a file with one of these.
_FILE_EXTENSIONS = frozenset((
    '.mp3 .wav .m4a .mp4 .webm .weba .ogg .oga .opus .flac .aac .wma .amr .3gp .3gpp .mov .mkv .mka .avi '
    '.wmv .m4v .mpeg .mpg .mpga .aiff .aif .caf .ts .md .txt .json .srt .vtt .docx .pdf .processing .remux '
    '.tmp .part').split())

# An absolute path written without quotes whose name has spaces, as the
# operating system and FFmpeg write it before ": <reason>": up to an
# extension of a media or text file that ends the path.
_SPACED_PATH = _re.compile(
    r'(?<![\w/.\-:])/(?:(?! [/~])[^\n\'"<>\x00:=(]){1,160}?(?:'
    + '|'.join(_re.escape(e) for e in sorted(_FILE_EXTENSIONS, key=len, reverse=True))
    + r')(?=[:\s,;)\]]|$)', _re.IGNORECASE)


def _private_dir_pattern():
    """Paths below the folders that hold user files, spaces included, up to
    an extension that ends the path."""
    global _private_pattern, _private_dirs
    if _private_dirs is None:
        upload = _os.environ.get('UPLOAD_FOLDER', '/data/uploads')
        _private_dirs = (upload, _os.environ.get('FILE_STORAGE_STAGING_DIR', ''),
                         _os.environ.get('AUTO_PROCESS_WATCH_DIR', '/data/auto-process'),
                         _os.environ.get('AUTO_EXPORT_DIR', '/data/exports'))
    if _private_pattern is None:
        roots = sorted({d.rstrip('/') for d in _private_dirs if d and d.strip('/')}, key=len, reverse=True)
        if roots:
            _private_pattern = _re.compile(
                r'(?<![\w/.\-])(?:' + '|'.join(_re.escape(r) for r in roots) + r')'
                r'/(?:(?! [/~])[^\n\'"<>\x00]){0,300}?\.[A-Za-z0-9]{1,10}(?=[:\s,;)\]]|$)')
        else:
            _private_pattern = False
    return _private_pattern or None


def _clean_token(token, label):
    """One run of text without spaces or quotes: a path, a key, a URL or a word."""
    if '/' not in token and '\\' not in token and '_' not in token:
        return token
    core = token.rstrip(_TRAILING)
    tail = token[len(core):]
    scheme_at = core.find('://')
    if scheme_at >= 0:
        scheme = core[:scheme_at]
        for storage in ('s3', 'local'):
            if scheme.endswith(storage) and not scheme[:-len(storage)][-1:].isalnum():
                cut = scheme_at - len(storage)
                return core[:cut] + label(loggable_locator(core[cut:])) + tail
        # Another URL: only a stored name in its path is replaced.
        return _URL_NAME.sub(lambda m: label(loggable_path(m.group(0))), core) + tail
    start = _PATH_START.search(core)
    if start:
        path = core[start.start():]
        name = _split(path)[1]
        if (not path.startswith(_SYSTEM_DIRS) and not name.endswith(_CODE_EXTENSIONS)
                and (_identified(name) or _extension_of(name))):
            return core[:start.start()] + label(loggable_path(path)) + tail
        return token
    scheme = _SEGMENT_SCHEME.search(core)
    if scheme:
        # A storage key or a bare stored name: the directories before the
        # name belong to it (recordings/2026/11/42/<name>).
        begin = scheme.start()
        while begin > 0 and core[begin - 1] not in '=:([{,;':
            begin -= 1
        return core[:begin] + label(loggable_path(core[begin:])) + tail
    return token


def _quoted(match, label, ids):
    after_errno = match.group(1)
    quote, content = ("'", match.group(2)) if match.group(2) is not None else ('"', match.group(3))
    if '\x00' in content:
        return match.group(0)
    name = _split(content)[1]
    extension = _extension_of(name)
    if content.startswith(_SYSTEM_DIRS) or name.endswith(_CODE_EXTENSIONS):
        return match.group(0)
    if after_errno and not extension and _re.fullmatch(r'[A-Za-z0-9_.\-]+', content) and not _identified(name):
        return match.group(0)          # a program that was not found: 'ffmpeg'
    if not (after_errno or _PATH_START.match(content) or _identified(name) or extension in _FILE_EXTENSIONS):
        return match.group(0)          # ordinary quoted text; its words are checked as tokens
    if content.startswith(('s3://', 'local://')):
        text = loggable_locator(content)
    else:
        text = loggable_path(content, ids.get(name))
    return f"{after_errno or ''}{quote}{label(text)}{quote}"


_NAME_STOP = frozenset(' \t\r\n\'"<>\x00=([{,;')


def _replace_name(text, name, make):
    """Replace every occurrence of a known file name, with the directories
    written directly before it."""
    parts, position = [], 0
    while True:
        at = text.find(name, position)
        if at < 0:
            break
        start, floor = at, max(position, at - 1024)
        while start > floor and text[start - 1] not in _NAME_STOP:
            start -= 1
        prefix = text[start:at]
        if not prefix.endswith(('/', '\\')) or ('://' in prefix and not prefix.startswith(('s3://', 'local://'))):
            start, prefix = at, ''
        parts.append(text[position:start])
        parts.append(make(prefix))
        position = at + len(name)
    parts.append(text[position:])
    return ''.join(parts)


def loggable_text(text, *paths, recording_id=None):
    """Free text (an error of FFmpeg, the operating system or the storage
    client) with every file named as in loggable_path.

    The files passed in are replaced wherever they occur, by path, by name or
    by name without extension. Beyond those, the following are replaced
    without being passed: storage locators and keys, staged and stored names,
    S3 download copies, quoted paths (also with spaces), absolute paths that
    end in a file name with an extension, and paths below the upload, staging,
    watch and export folders. Paths of the operating system (/usr, /etc, ...)
    and URLs of services stay, apart from a stored name inside a URL. Not
    recognized: a relative name with spaces that is neither quoted nor passed
    in, and an unquoted absolute path with spaces outside those folders whose
    extension is not one of a media or text file.
    """
    text = str(text).replace('\x00', ' ')
    labels = []

    def label(value):
        labels.append(value)
        return f"\x00{len(labels) - 1}\x00"

    passed = [str(path) for path in paths if path]
    ids = {_split(path)[1]: recording_id for path in passed} if recording_id is not None else {}
    # Labels already in the text (an error raised with one) are left as they are.
    text = _LABEL.sub(lambda m: label(m.group(0)), text)
    text = _QUOTED.sub(lambda m: _quoted(m, label, ids), text)
    private = _private_dir_pattern()
    if private is not None:
        text = private.sub(lambda m: label(loggable_path(m.group(0), ids.get(_split(m.group(0))[1]))), text)
    text = _SPACED_PATH.sub(
        lambda m: m.group(0) if m.group(0).startswith(_SYSTEM_DIRS)
        else label(loggable_path(m.group(0), ids.get(_split(m.group(0))[1]))), text)
    for path in passed:
        name = _split(path)[1]
        for known in sorted({name, _os.path.splitext(name)[0]}, key=len, reverse=True):
            if len(known) >= 3 and known in text:
                text = _replace_name(text, known, lambda prefix: label(loggable_path(prefix + name, recording_id)))
    text = _TOKEN.sub(lambda m: _clean_token(m.group(0), label), text)
    return _re.sub(r'\x00(\d+)\x00', lambda m: labels[int(m.group(1))], text)


def loggable_error(error, *paths, recording_id=None):
    """The text of a file or FFmpeg error with files named as in loggable_path.

    FFmpeg and the operating system put the path of the file in their error
    text, and the path of a stored upload ends in the original filename.
    """
    return loggable_text(error, *paths, recording_id=recording_id)
