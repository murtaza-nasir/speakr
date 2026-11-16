"""File labels of the log at INFO and above (#620).

A file is named by an identifier: the timestamp of its storage key, the start
of a staging id, the random part of an S3 download copy, or a keyed digest of
its name. It is never named by the name a user gave it, and never by its
extension alone. Error text is cleaned of every such name.

No database: the helpers of src/utils/log_filters.py only.
"""

import hashlib
import hmac
import logging
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import log_filters
from src.utils.log_filters import (FileNamesOutOfTracebacks, loggable_error, loggable_locator, loggable_path,
                                   loggable_text, loggable_traceback, recording_scope)

UUID = "0123456789abcdef0123456789abcdef"
SECRET = "label-test-secret-7f3a"
STORED = "/data/uploads/recordings/2026/11/42/20261115101500_Jane_Doe_intake.mp3"
STAGED = f"/data/uploads/_staging/20261115101500_{UUID}_Jane_Doe_intake.mp3"
WATCHED = "/watch/user7/Jane Doe intake.M4A"
NAME_PARTS = ("Jane", "Doe", "intake")

# A label with nothing that identifies a file: "<file .mp3>", "<file>",
# "<stored file *.mp3>", "<stored file dir/*.mp3>".
GHOST = re.compile(r"<(?:stored file|staged file|s3 copy|file)(?: [^<>]*/)? ?\*?(?:\.\w+)?>")
DIGEST = r"[0-9a-f]{10}"


@pytest.fixture(autouse=True)
def label_key():
    """A known key and known folders for each test; the state of the module is put back."""
    saved = (log_filters._label_key, log_filters._fallback_key, log_filters._private_dirs,
             log_filters._private_pattern)
    log_filters.configure(secret_key=SECRET,
                          private_dirs=["/data/uploads", "/data/auto-process", "/data/exports"])
    yield
    (log_filters._label_key, log_filters._fallback_key, log_filters._private_dirs,
     log_filters._private_pattern) = saved


def _digest(name, secret=SECRET):
    key = hmac.new(secret.encode(), b"speakr log file label v1", hashlib.sha256).digest()
    return hmac.new(key, name.encode(), hashlib.sha256).hexdigest()[:10]


def _no_name(text):
    assert not any(part in text for part in NAME_PARTS), text
    assert not GHOST.search(text), text
    return text


# --- one label per naming scheme ----------------------------------------------

@pytest.mark.parametrize("path, label", [
    # Upload in the staging folder: <timestamp>_<uuid>_<name>.
    (STAGED, "<stored file 20261115101500_0123456789ab.mp3>"),
    # Stored media below <prefix>/YYYY/MM/<recording id>/: <timestamp>_<name>.
    (STORED, "<stored file 20261115101500.mp3 of recording 42>"),
    ("recordings/2026/11/42/20261115101500_Jane_Doe_intake.MP3", "<stored file 20261115101500.mp3 of recording 42>"),
    # A stored name outside a key directory, and a key of an upload without an id.
    ("/somewhere/20261115101500_Jane_Doe_intake.mp3", "<stored file 20261115101500.mp3>"),
    ("recordings/2026/11/tmp-0a1b2c3d/20261115101500_Jane_Doe_intake.mp3", "<stored file 20261115101500.mp3>"),
    # Files derived from a stored name keep its timestamp.
    ("/data/uploads/recordings/2026/11/42/20261115101500_Jane_Doe_intake_converted.flac",
     "<stored file 20261115101500.flac of recording 42>"),
    ("/data/uploads/recordings/2026/11/42/.20261115101500_Jane_Doe_intake.mp4.k3j2x1.remux",
     "<stored file 20261115101500.remux of recording 42>"),
    ("/tmp/tmpk3j2/20261115101500_Jane_Doe_intake_chunk_003.mp3", "<stored file 20261115101500.mp3 chunk 3>"),
    # Staging of the convert job: <uuid>_<base name of the converted file>.
    (f"/data/uploads/_staging/{UUID}_20261115101500_Jane_Doe_intake_converted.mp3",
     "<stored file 20261115101500_0123456789ab.mp3>"),
    (f"/data/uploads/_staging/{UUID}_speakr_s3_ab12cd34_converted.mp3", "<staged file 0123456789ab.mp3>"),
    (f"/data/uploads/_staging/{UUID}_Jane_Doe_intake.mp3", "<staged file 0123456789ab.mp3>"),
    # Download copy of an S3 object, and files derived from it.
    ("/tmp/speakr_s3_ab12cd34.mp3", "<s3 copy ab12cd34.mp3>"),
    ("/tmp/tmpk3j2/speakr_s3_ab12cd34_chunk_011.mp3", "<s3 copy ab12cd34.mp3 chunk 11>"),
    # No file.
    (None, "<no file>"),
    ("", "<no file>"),
])
def test_label_of_each_naming_scheme(path, label):
    assert loggable_path(path) == label
    _no_name(label)


@pytest.mark.parametrize("path, extension", [
    (WATCHED, ".m4a"),                                        # watch folder input
    (WATCHED + ".processing", ".processing"),                 # the same file while it is locked
    ("/data/exports/jane/Weekly Sync with Jane Doe.md", ".md"),  # export file
    ("Jane_Doe_intake.webm", ".webm"),                        # an original file name
    ("/watch/report_20261115101500_Jane.mp3", ".mp3"),        # a timestamp that is not at the start
    ("/tmp/tmpab12cd.wav", ".wav"),
    ("no-extension", ""),
    ("/watch/Jane.Doe.an-extension-that-is-too-long", ""),
])
def test_a_file_without_an_identifier_is_named_by_a_keyed_digest(path, extension):
    label = loggable_path(path)
    name = os.path.basename(path)
    assert label == f"<file {_digest(name)}{extension}>"
    assert re.fullmatch(rf"<file {DIGEST}(?:\.[a-z0-9]+)?>", label)
    _no_name(label)


def test_the_recording_id_is_part_of_the_label_when_it_is_known():
    # Passed by the caller.
    assert loggable_path("/tmp/speakr_s3_ab12cd34.mp3", 42) == "<s3 copy ab12cd34.mp3 of recording 42>"
    assert loggable_path(WATCHED, 42) == f"<file {_digest('Jane Doe intake.M4A')}.m4a of recording 42>"
    assert loggable_path(STAGED, 42) == "<stored file 20261115101500_0123456789ab.mp3 of recording 42>"
    # Read from the directories of a storage key; the caller's id wins.
    assert loggable_path(STORED) == "<stored file 20261115101500.mp3 of recording 42>"
    assert loggable_path(STORED, 7) == "<stored file 20261115101500.mp3 of recording 7>"
    # A file that is not a stored file takes no id from its directories.
    assert loggable_path("/watch/2026/11/42/Jane Doe.mp3") == f"<file {_digest('Jane Doe.mp3')}.mp3>"


def test_recording_scope_labels_every_file_of_a_job():
    assert loggable_path("/tmp/speakr_s3_ab12cd34.mp3") == "<s3 copy ab12cd34.mp3>"
    with recording_scope(9):
        assert loggable_path("/tmp/speakr_s3_ab12cd34.mp3") == "<s3 copy ab12cd34.mp3 of recording 9>"
        assert loggable_path("/tmp/x/speakr_s3_ab12cd34_chunk_002.mp3") == "<s3 copy ab12cd34.mp3 chunk 2 of recording 9>"
        assert loggable_path("/tmp/tmpab12cd.wav") == f"<file {_digest('tmpab12cd.wav')}.wav of recording 9>"
        # An id in the storage key or from the caller comes first.
        assert loggable_path(STORED) == "<stored file 20261115101500.mp3 of recording 42>"
        assert loggable_path(WATCHED, 5).endswith(" of recording 5>")
        assert "of recording 9" in loggable_error(RuntimeError("cannot open /tmp/speakr_s3_ab12cd34.mp3"))
        with pytest.raises(RuntimeError):
            with recording_scope(10):
                assert loggable_path("/tmp/tmpab12cd.wav").endswith(" of recording 10>")
                raise RuntimeError("the job failed")
        assert loggable_path("/tmp/tmpab12cd.wav").endswith(" of recording 9>")
    assert loggable_path("/tmp/speakr_s3_ab12cd34.mp3") == "<s3 copy ab12cd34.mp3>"


def test_locator_labels():
    key = "recordings/2026/11/42/20261115101500_Jane_Doe_intake.MP3"
    assert loggable_locator(f"local://{key}") == "<stored file local://recordings/2026/11/42/20261115101500.mp3>"
    assert loggable_locator(f"s3://bucket/{key}") == "<stored file s3://bucket/recordings/2026/11/42/20261115101500.mp3>"
    # A name of no known scheme: its digest, where the label was "*.flac" before.
    assert loggable_locator("funasr/42/Jane_Doe.flac") == f"<stored file funasr/42/{_digest('Jane_Doe.flac')}.flac>"
    assert loggable_locator("Jane Doe") == f"<stored file {_digest('Jane Doe')}>"
    assert loggable_locator(None) == "<no file>"


# --- no label is an extension alone -------------------------------------------

EVERY_PATH = [
    STAGED, STORED, WATCHED, "Jane_Doe_intake.mp3", "Jane Doe", ".mp3", "mp3", ".", "..", "/", "a/", "/watch/.hidden",
    "/watch/Jane.", "/watch/Jane Doe intake", f"{UUID}_x.mp3", "speakr_s3_ab12cd34.mp3", "speakr_s3_.mp3",
    "20261115101500_", "20261115101500_.mp3", "2026111510150_Jane.mp3", "local://recordings/2026/11/42/Jane.mp3",
    "s3://bucket/Jane Doe.mp3", "C:\\Users\\Jane Doe\\intake.mp3", "Jane\nDoe.mp3", "Jäne Döe.m4a", "x" * 5000 + ".mp3",
    "\udcff.mp3", "/data/exports/jane/Weekly Sync.md", 42, b"bytes.mp3",
]


@pytest.mark.parametrize("path", EVERY_PATH, ids=lambda p: repr(p)[:40])
def test_no_helper_returns_a_label_without_an_identifier(path):
    for label in (loggable_path(path), loggable_path(path, 42), loggable_locator(path)):
        assert not GHOST.search(label), label
        # Between the kind and the extension there is an identifier of 8 or more characters.
        assert re.fullmatch(r"<(?:stored file|staged file|s3 copy|file) (?:[^<>]*/)?[\w.-]{8,}(?: chunk \d+)?"
                            r"(?: of recording \d+)?>", label, re.S), label
    for text in (loggable_error(RuntimeError(f"cannot open {path}"), path),
                 loggable_error(RuntimeError(f"[Errno 2] No such file or directory: '{path}'"))):
        assert not GHOST.search(text), text


def test_the_ghost_pattern_of_these_tests_matches_the_old_labels():
    for old in ("<file .mp3>", "<file>", "<file >", "<stored file *.m4a>", "<stored file funasr/42/*.flac>",
                "<stored file *>", "<stored file /data/uploads/*.m4a>"):
        assert GHOST.search(old), old
    for new in ("<file 4f3a1c9b2d.mp3>", "<stored file 20261115101500.mp3>", "<s3 copy ab12cd34.mp3>",
                "<stored file local://recordings/2026/11/42/20261115101500.mp3>", "<no file>"):
        assert not GHOST.search(new), new


# --- the digest ---------------------------------------------------------------

def test_the_digest_is_stable_and_keyed():
    name = "Jane_Doe_intake.mp3"
    first = loggable_path(f"/watch/{name}")
    # The same name, the same label: in another folder, on another call, and
    # after the key is set again from the same secret (a restart, another worker).
    assert loggable_path(f"/other/folder/{name}") == first == loggable_path(f"/watch/{name}")
    log_filters._label_key = None
    log_filters.configure(secret_key=SECRET)
    assert loggable_path(f"/watch/{name}") == first == f"<file {_digest(name)}.mp3>"
    # Another name, another label.
    assert loggable_path("/watch/Jane_Doe_intake2.mp3") != first
    # Keyed: a plain hash of a guessed name confirms nothing.
    for plain in (hashlib.sha256, hashlib.sha1, hashlib.md5):
        assert plain(name.encode()).hexdigest()[:10] not in first
    assert hmac.new(SECRET.encode(), name.encode(), hashlib.sha256).hexdigest()[:10] not in first
    # Another secret, another label.
    log_filters.configure(secret_key="another-secret")
    other = loggable_path(f"/watch/{name}")
    assert other != first and other == f"<file {_digest(name, 'another-secret')}.mp3>"


def test_the_secret_is_not_kept_and_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    secret = "s3cr3t-value-that-must-not-leak"
    log_filters.configure(secret_key=secret)
    labels = [loggable_path(p) for p in EVERY_PATH] + [loggable_error(RuntimeError(f"cannot open {WATCHED}"))]
    assert not any(secret in label for label in labels)
    held = [value for value in vars(log_filters).values() if isinstance(value, (str, bytes))]
    assert secret not in [v if isinstance(v, str) else v.decode("latin-1") for v in held]
    assert secret.encode() not in [v for v in held if isinstance(v, bytes)]
    assert secret not in caplog.text
    # A secret given as bytes gives the same key as the same secret as text.
    as_text = loggable_path(WATCHED)
    log_filters.configure(secret_key=secret.encode())
    assert loggable_path(WATCHED) == as_text


def test_without_a_secret_the_digest_has_a_random_key_of_the_process():
    log_filters._label_key = None
    log_filters._fallback_key = None
    first = loggable_path(WATCHED)
    assert re.fullmatch(rf"<file {DIGEST}\.m4a>", first)
    assert loggable_path(WATCHED) == first                      # stable while the process runs
    assert first != f"<file {_digest('Jane Doe intake.M4A')}.m4a>"
    assert hashlib.sha256(b"Jane Doe intake.M4A").hexdigest()[:10] not in first
    log_filters._fallback_key = None                            # as in another process
    assert loggable_path(WATCHED) != first
    # An empty secret is no secret.
    log_filters.configure(secret_key="")
    assert log_filters._label_key is None


def test_the_application_sets_the_key_from_its_secret_key():
    """src.app configures the labels at import, so every worker process that
    loads the application with the same SECRET_KEY labels a file the same way."""
    saved = log_filters._label_key
    log_filters._label_key = None
    try:
        from src.app import app
        log_filters.configure(secret_key=app.config["SECRET_KEY"])
        expected = f"<file {_digest('Jane Doe intake.M4A', str(app.config['SECRET_KEY']))}.m4a>"
        assert loggable_path(WATCHED) == expected
    finally:
        log_filters._label_key = saved
    import inspect
    import src.app as app_module
    source = inspect.getsource(app_module)
    assert "_configure_log_labels(secret_key=_secret_key" in source


# --- error text ---------------------------------------------------------------

def test_the_example_of_the_issue():
    """loggable_error let a stored path through when it was only in the text."""
    text = loggable_error(RuntimeError(f"cannot open {STORED}"))
    assert text == "cannot open <stored file 20261115101500.mp3 of recording 42>"
    _no_name(text)
    assert loggable_error(RuntimeError(f"cannot open {STORED}"), STORED) == text
    with_reason = loggable_error(RuntimeError(f"{STORED}: Invalid data found when processing input"))
    assert with_reason == "<stored file 20261115101500.mp3 of recording 42>: Invalid data found when processing input"


@pytest.mark.parametrize("text, expected", [
    # Operating system: quoted, with spaces, one or two paths.
    (f"[Errno 2] No such file or directory: '{STORED}'",
     "[Errno 2] No such file or directory: '<stored file 20261115101500.mp3 of recording 42>'"),
    (f"[Errno 2] No such file or directory: '{WATCHED}'",
     "[Errno 2] No such file or directory: '<file {d1}.m4a>'"),
    ("[Errno 2] No such file or directory: 'Jane Doe intake.M4A'",
     "[Errno 2] No such file or directory: '<file {d1}.m4a>'"),
    ('[Errno 2] No such file or directory: "/watch/Jane Doe\'s intake.M4A"',
     '[Errno 2] No such file or directory: "<file {d3}.m4a>"'),
    (f"[Errno 13] Permission denied: '{WATCHED}' -> '{WATCHED}.processing'",
     "[Errno 13] Permission denied: '<file {d1}.m4a>' -> '<file {d2}.processing>'"),
    ("[Errno 28] No space left on device: '/data/exports/jane/Weekly Sync with Jane Doe.md'",
     "[Errno 28] No space left on device: '<file {d4}.md>'"),
    # FFmpeg: the input in quotes, then the path before the reason.
    (f"Input #0, mp3, from '{STORED}':\n  Duration: N/A, bitrate: 128 kb/s\n{STORED}: Invalid data found when processing input",
     "Input #0, mp3, from '<stored file 20261115101500.mp3 of recording 42>':\n  Duration: N/A, bitrate: 128 kb/s\n"
     "<stored file 20261115101500.mp3 of recording 42>: Invalid data found when processing input"),
    (f"ffmpeg -i {STAGED} -y out failed: {STAGED}: Invalid data found when processing input",
     "ffmpeg -i <stored file 20261115101500_0123456789ab.mp3> -y out failed: "
     "<stored file 20261115101500_0123456789ab.mp3>: Invalid data found when processing input"),
    # Without quotes and with spaces: below a folder of user files, and anywhere up to a media extension.
    ("/data/auto-process/user7/Jane Doe intake.M4A: No such file or directory",
     "<file {d1}.m4a>: No such file or directory"),
    (f"{WATCHED}: No such file or directory", "<file {d1}.m4a>: No such file or directory"),
    ("/data/exports/jane/Jane Doe intake.odd: Permission denied", "<file {d5}.odd>: Permission denied"),
    # Storage: keys, locators, a presigned address.
    ("An error occurred (NoSuchKey) when calling the GetObject operation: The specified key "
     "recordings/2026/11/42/20261115101500_Jane_Doe_intake.mp3 does not exist.",
     "An error occurred (NoSuchKey) when calling the GetObject operation: The specified key "
     "<stored file 20261115101500.mp3 of recording 42> does not exist."),
    ("could not read s3://bucket/recordings/2026/11/42/20261115101500_Jane_Doe_intake.mp3, retrying",
     "could not read <stored file s3://bucket/recordings/2026/11/42/20261115101500.mp3>, retrying"),
    ("delete of 'local://recordings/2026/11/42/20261115101500_Jane_Doe_intake.mp3' failed",
     "delete of '<stored file local://recordings/2026/11/42/20261115101500.mp3>' failed"),
    ("403 for https://bucket.s3.amazonaws.com/recordings/2026/11/42/20261115101500_Jane_Doe_intake.mp3?X-Amz-Signature=abc",
     "403 for https://bucket.s3.amazonaws.com/recordings/2026/11/42/<stored file 20261115101500.mp3>?X-Amz-Signature=abc"),
    # Staging names, download copies, chunks, a bare stored name.
    (f"cannot open {STAGED}", "cannot open <stored file 20261115101500_0123456789ab.mp3>"),
    (f"cannot open /data/uploads/_staging/{UUID}_20261115101500_Jane_Doe_converted.mp3.",
     "cannot open <stored file 20261115101500_0123456789ab.mp3>."),
    (f"cannot open /data/uploads/_staging/{UUID}_Jane_Doe.mp3", "cannot open <staged file 0123456789ab.mp3>"),
    ("cannot open /tmp/speakr_s3_ab12cd34.mp3", "cannot open <s3 copy ab12cd34.mp3>"),
    ("moving (/tmp/tmpk3j2/20261115101500_Jane_Doe_intake_chunk_003.mp3) failed",
     "moving (<stored file 20261115101500.mp3 chunk 3>) failed"),
    ("file=20261115101500_Jane_Doe_intake.mp3 is unreadable", "file=<stored file 20261115101500.mp3> is unreadable"),
    # A traceback: the source files stay, the file of the error does not.
    (f'Traceback (most recent call last):\n  File "/app/src/tasks/processing.py", line 12, in f\n    open(p)\n'
     f"FileNotFoundError: [Errno 2] No such file or directory: '{STORED}'",
     'Traceback (most recent call last):\n  File "/app/src/tasks/processing.py", line 12, in f\n    open(p)\n'
     "FileNotFoundError: [Errno 2] No such file or directory: '<stored file 20261115101500.mp3 of recording 42>'"),
])
def test_file_names_are_taken_out_of_error_text(text, expected):
    expected = expected.format(d1=_digest("Jane Doe intake.M4A"), d2=_digest("Jane Doe intake.M4A.processing"),
                               d3=_digest("Jane Doe's intake.M4A"), d4=_digest("Weekly Sync with Jane Doe.md"),
                               d5=_digest("Jane Doe intake.odd"))
    assert loggable_error(RuntimeError(text)) == expected
    assert loggable_text(text) == expected
    _no_name(expected.replace("Jane Doe's", ""))
    # Cleaned text is not changed by a second pass.
    assert loggable_text(expected) == expected


def test_an_os_error_object_is_cleaned_like_its_text():
    error = FileNotFoundError(2, "No such file or directory", STORED)
    assert loggable_error(error) == "[Errno 2] No such file or directory: '<stored file 20261115101500.mp3 of recording 42>'"
    rename = PermissionError(13, "Permission denied", WATCHED, None, WATCHED + ".processing")
    _no_name(loggable_error(rename))


def test_files_passed_in_are_replaced_wherever_they_occur():
    # A name with spaces, without quotes, outside every known folder and with an
    # extension of no known kind: found only because it was passed in.
    path = "/mnt/share/Jane Doe intake.xyz"
    text = loggable_error(RuntimeError(f"{path}: bad; derived Jane Doe intake_converted.wav is missing"), path, recording_id=7)
    label = f"<file {_digest('Jane Doe intake.xyz')}.xyz of recording 7>"
    assert text == f"{label}: bad; derived {label}_converted.wav is missing"
    # A locator passed in, a path of the backend in the text.
    text = loggable_error(OSError(13, "Permission denied", STORED),
                          "local://recordings/2026/11/42/20261115101500_Jane_Doe_intake.mp3")
    assert text == "[Errno 13] Permission denied: '<stored file 20261115101500.mp3 of recording 42>'"
    # The id of the caller is used for a passed file inside quotes as well.
    text = loggable_error(RuntimeError("[Errno 2] No such file or directory: '/tmp/speakr_s3_ab12cd34.mp3'"),
                          "/tmp/speakr_s3_ab12cd34.mp3", recording_id=7)
    assert text == "[Errno 2] No such file or directory: '<s3 copy ab12cd34.mp3 of recording 7>'"
    assert loggable_error(RuntimeError("nothing to replace"), None, "", "ab") == "nothing to replace"


@pytest.mark.parametrize("text", [
    "Duration: N/A, bitrate: 128 kb/s, audio/mpeg, 1/25 tbr and/or 44100 Hz",
    "Stream #0:0 -> #0:1 (mp3 (mp3float) -> pcm_s16le (native))",
    "HTTP 404 for https://api.example.com/v1/audio/transcriptions at /v1/audio/transcriptions",
    "[Errno 2] No such file or directory: 'ffmpeg'",
    "[Errno 2] No such file or directory: '/usr/bin/ffprobe'",
    "/usr/lib/x86_64-linux-gnu/libavcodec.so.60: cannot open shared object file: No such file or directory",
    '  File "/app/src/tasks/processing.py", line 12, in transcribe',
    '  File "/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py", line 1967, in _exec',
    "can't open it, it doesn't exist; module 'os.path' has no attribute 'x.y'",
    "the folder /data/uploads/recordings/2026/11/42 is missing",
    "connection to postgresql://db:5432/speakr refused (version 1.2.3, 5/10 attempts)",
    "Unknown encoder 'libmp3lame'; codec 'pcm_s16le' is not supported in container 'webm'",
    "[mp3 @ 0x55d0c8a7b2c0] Estimating duration from bitrate, this may be inaccurate",
    "ffmpeg timed out after 3600s (snake_case_word and a_b_c stay)",
    "already labeled: <stored file 20261115101500_0123456789ab.mp3> and <file 4f3a1c9b2d.m4a of recording 3>",
    "FFmpeg not found. Please ensure FFmpeg is installed and in the system's PATH.",
    "",
])
def test_error_text_without_a_user_file_is_unchanged(text):
    assert loggable_error(RuntimeError(text)) == text


@pytest.mark.parametrize("shape", [
    "x ", "1", "/a", "'", "'\"", "_", "/data/uploads/", "<file ", "[Errno 2] ", "20261115101500_",
    "0123456789abcdef", "/a.b", ":/", "\x00", "(/a", " /a b", "=/a", " /a b.mp3x", "a b /", "s3://", "local://a/",
    "' -> '", "\n", "\\", "a/", "/data/uploads/" + "a" * 290 + " ", "'" + "a" * 1023 + '"',
    STORED + ": Invalid data found when processing input\n",
    "[mp3 @ 0x55d0c8a7b2c0] Estimating duration from bitrate, this may be inaccurate\n",
], ids=lambda s: repr(s)[:24])
def test_a_megabyte_of_error_text_is_cleaned_without_backtracking(shape):
    text = shape * (1_000_000 // len(shape) + 1)
    assert len(text) >= 1_000_000
    started = time.perf_counter()
    cleaned = loggable_error(RuntimeError(text), STORED, WATCHED)
    elapsed = time.perf_counter() - started
    # About a second at most on the development host; the limit leaves room for a loaded CI runner.
    assert elapsed < 10, f"{elapsed:.1f}s for a megabyte of {shape!r}"
    assert "Jane" not in cleaned


# --- tracebacks ---------------------------------------------------------------

def _record_with_traceback(level=logging.ERROR):
    try:
        open("/nonexistent-folder-of-this-test/2026/11/42/20261115101500_Jane_Doe_intake.mp3")
    except OSError:
        return logging.LogRecord("test", level, __file__, 1, "Error processing a file", None, sys.exc_info())


def test_a_traceback_has_no_file_name_above_debug():
    formatter = FileNamesOutOfTracebacks("%(levelname)s - %(message)s")
    root = logging.getLogger()
    level = root.level
    try:
        root.setLevel(logging.INFO)
        text = formatter.format(_record_with_traceback())
        assert "Traceback (most recent call last)" in text and "FileNotFoundError: [Errno 2]" in text
        assert "<stored file 20261115101500.mp3 of recording 42>" in text
        assert os.path.basename(__file__) in text            # the source file of the frame stays
        # The source line of the frame is code, here with the path as a literal; the exception text is cleaned.
        assert '    open("/nonexistent-folder-of-this-test/2026/11/42/20261115101500_Jane_Doe_intake.mp3")' in text.split("\n")
        _no_name(text.split("\n")[-1])
        # At DEBUG names are logged anyway: the traceback is as Python wrote it.
        root.setLevel(logging.DEBUG)
        assert "20261115101500_Jane_Doe_intake.mp3" in formatter.format(_record_with_traceback())
    finally:
        root.setLevel(level)


def _open_stored(path):
    raise FileNotFoundError(2, "No such file or directory", path)  # the literal 'config.json' is code


def _formatted(run, level=logging.INFO):
    formatter = FileNamesOutOfTracebacks("%(levelname)s - %(message)s")
    root = logging.getLogger()
    saved = root.level
    try:
        root.setLevel(level)
        try:
            run()
        except Exception:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Error processing a file", None, sys.exc_info())
            return formatter.format(record)
        raise AssertionError("run() did not raise")
    finally:
        root.setLevel(saved)


def test_the_frames_of_a_traceback_are_unchanged():
    """A quoted file name in a source line is code and stays; the stored path
    in the exception text is replaced."""
    text = _formatted(lambda: _open_stored(STORED))
    lines = text.split("\n")
    assert "    raise FileNotFoundError(2, \"No such file or directory\", path)  # the literal 'config.json' is code" in lines
    assert any(line.startswith(f'  File "{__file__}", line ') and line.endswith(", in _open_stored") for line in lines)
    assert lines[-1] == ("FileNotFoundError: [Errno 2] No such file or directory: "
                         "'<stored file 20261115101500.mp3 of recording 42>'")
    _no_name(text)
    # The teeth of this test: cleaned as plain text, the source line loses its literal.
    assert "'config.json'" not in loggable_text(text)


def _chain_with_cause():
    try:
        _open_stored(STORED)
    except OSError as error:
        raise RuntimeError(f"cannot convert {WATCHED}: see 'notes.txt'") from error


def _chain_while_handling():
    try:
        _open_stored(STORED)
    except OSError:
        raise RuntimeError(f"cleanup of '{WATCHED}' failed")  # not 'config.json'


@pytest.mark.parametrize("run, separator, last", [
    (_chain_with_cause, "The above exception was the direct cause of the following exception:",
     "RuntimeError: cannot convert <file {d1}.m4a>: see '<file {d2}.txt>'"),
    (_chain_while_handling, "During handling of the above exception, another exception occurred:",
     "RuntimeError: cleanup of '<file {d1}.m4a>' failed"),
])
def test_both_messages_of_a_chained_traceback_are_cleaned(run, separator, last):
    text = _formatted(run)
    lines = text.split("\n")
    assert lines.count("Traceback (most recent call last):") == 2 and separator in lines
    assert ("FileNotFoundError: [Errno 2] No such file or directory: "
            "'<stored file 20261115101500.mp3 of recording 42>'") in lines
    assert lines[-1] == last.format(d1=_digest("Jane Doe intake.M4A"), d2=_digest("notes.txt"))
    # Every frame is as Python wrote it, the source lines with their literals included.
    assert sum(line.startswith(f'  File "{__file__}", line ') for line in lines) >= 4
    assert any("'config.json'" in line and line.startswith("    ") for line in lines)
    _no_name(text)
    # At DEBUG the traceback is as Python wrote it.
    assert STORED in _formatted(run, logging.DEBUG) and WATCHED in _formatted(run, logging.DEBUG)


def test_every_line_of_a_message_of_several_lines_is_cleaned():
    def run():
        raise RuntimeError(f"ffmpeg failed:\n  File \"{STORED}\", line 1\n    {STORED}: Invalid data\n\n"
                           f"after an empty line: {WATCHED}: No such file or directory\n"
                           f"Traceback (most recent call last):\n  {STAGED}\n    {STAGED}")
    text = _formatted(run)
    _no_name(text)
    assert text.count("<stored file 20261115101500.mp3 of recording 42>") == 2
    assert f"after an empty line: <file {_digest('Jane Doe intake.M4A')}.m4a>: No such file or directory" in text
    # A line that only looks like a traceback header inside the message starts no frames.
    assert text.count("<stored file 20261115101500_0123456789ab.mp3>") == 2


def test_traceback_text_of_other_shapes():
    # No traceback at all, and an empty text.
    assert loggable_traceback(f"RuntimeError: cannot open {STORED}") == \
        "RuntimeError: cannot open <stored file 20261115101500.mp3 of recording 42>"
    assert loggable_traceback("") == ""
    # Repeated frames of a recursion stay.
    repeated = ('Traceback (most recent call last):\n  File "/app/src/x.py", line 3, in f\n    f(\'a.json\')\n'
                '  [Previous line repeated 996 more times]\nRecursionError: maximum recursion depth exceeded')
    assert loggable_traceback(repeated) == repeated
    # An exception group is cleaned whole.
    group = ("  + Exception Group Traceback (most recent call last):\n  |   File \"/app/src/x.py\", line 3, in f\n"
             f"  | ExceptionGroup: two errors (2 sub-exceptions)\n  +-+---------------- 1 ----------------\n"
             f"    | FileNotFoundError: [Errno 2] No such file or directory: '{STORED}'\n    +------------------------------------")
    cleaned = loggable_traceback(group)
    assert "<stored file 20261115101500.mp3 of recording 42>" in cleaned and '"/app/src/x.py"' in cleaned
    _no_name(cleaned)
    # A megabyte of frames and message.
    big = "Traceback (most recent call last):\n" + '  File "/app/src/x.py", line 3, in f\n    f()\n' * 12000 \
        + f"RuntimeError: {STORED}: bad\n" * 6000
    started = time.perf_counter()
    cleaned = loggable_traceback(big)
    assert time.perf_counter() - started < 10 and "Jane" not in cleaned


def test_the_application_log_uses_the_formatter():
    import src.app  # noqa: F401  (configures the root logger at import)
    import inspect
    assert "formatter = _LogFormatter(" in inspect.getsource(src.app)
