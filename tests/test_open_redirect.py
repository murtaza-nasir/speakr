"""Regression tests for the open-redirect class in is_safe_url.

The original implementation validated ``urljoin(host_url, target)`` while
``redirect()`` was called with the raw ``target``. A scheme-relative input
such as ``////evil.com`` looked safe to the validator (urljoin resolves it
to a same-host path) but the browser, given the raw value in the Location
header, treats it as a network-path redirect to ``evil.com``.

The current validator only allows local relative paths, so the same raw
value flows through both the safety check and the redirect call.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.security import is_safe_url


def test_rejects_scheme_relative_redirect():
    assert is_safe_url('//evil.com') is False
    assert is_safe_url('////evil.com') is False
    assert is_safe_url('//evil.com/path') is False


def test_rejects_absolute_url():
    assert is_safe_url('http://evil.com/login') is False
    assert is_safe_url('https://evil.com/login') is False
    assert is_safe_url('javascript:alert(1)') is False
    assert is_safe_url('data:text/html,<script>') is False


def test_rejects_backslash_variants():
    assert is_safe_url('/\\evil.com') is False
    assert is_safe_url('\\\\evil.com') is False
    assert is_safe_url('/path\\evil.com') is False


def test_rejects_empty_or_non_string():
    assert is_safe_url(None) is False
    assert is_safe_url('') is False
    assert is_safe_url(b'/path') is False
    assert is_safe_url(123) is False


def test_rejects_relative_paths_without_leading_slash():
    assert is_safe_url('recordings/123') is False
    assert is_safe_url('account') is False


def test_rejects_control_characters():
    assert is_safe_url('/path\nLocation: http://evil.com') is False
    assert is_safe_url('/path\r\nX-Injected: 1') is False
    assert is_safe_url('/path\x00') is False


def test_accepts_local_paths():
    assert is_safe_url('/') is True
    assert is_safe_url('/recordings') is True
    assert is_safe_url('/recordings/123') is True
    assert is_safe_url('/account?tab=preferences') is True
    assert is_safe_url('/recordings#section') is True


if __name__ == '__main__':
    test_rejects_scheme_relative_redirect()
    test_rejects_absolute_url()
    test_rejects_backslash_variants()
    test_rejects_empty_or_non_string()
    test_rejects_relative_paths_without_leading_slash()
    test_rejects_control_characters()
    test_accepts_local_paths()
    print('All open-redirect regression tests passed.')
