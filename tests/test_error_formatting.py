"""sanitize_error_message: the error text stored on a failed recording (#606)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.error_formatting import ERROR_MESSAGE_MAX_CHARS, sanitize_error_message  # noqa: E402


def test_sanitize_error_message_redacts_and_caps():
    msg = sanitize_error_message("failed at /tmp/secret/file.mp3   with   spaces")
    assert msg == "failed at /tmp/<path> with spaces"
    long = sanitize_error_message("x" * 1000)
    assert len(long) == ERROR_MESSAGE_MAX_CHARS and long.endswith('…')


def test_sanitize_error_message_empty_and_max_length():
    assert sanitize_error_message("") == ""
    assert sanitize_error_message(None) is None
    exact = "y" * ERROR_MESSAGE_MAX_CHARS
    assert sanitize_error_message(exact) == exact


def test_sanitize_error_message_keeps_other_paths():
    assert sanitize_error_message("see /data/uploads/a/b.wav and /home/x") == "see /data/uploads/<path> and /home/x"
