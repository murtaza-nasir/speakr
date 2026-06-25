"""Coverage + security regression tests for src/services/storage/locator.py.

Mutation testing (2026-06-25) found that removing the path-traversal guard in
``local_path_from_key`` broke NO test: the check that stops a storage key like
``../../etc/passwd`` from escaping the configured storage root was untested.
A regression there would let a crafted locator read/write arbitrary host paths.
These tests close that gap and cover the locator parsing helpers.

These are pure path/string functions, so no app context or DB is needed.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.storage import locator as loc


@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "../../../etc/passwd",
    "a/../../b",
    "../outside",
    "/../../etc/shadow",
])
def test_local_path_from_key_blocks_traversal(evil):
    """A key that resolves outside the storage root must raise, not return a path."""
    root = tempfile.mkdtemp()
    with pytest.raises(ValueError):
        loc.local_path_from_key(root, evil)


def test_local_path_from_key_normal_key_stays_under_root():
    root = tempfile.mkdtemp()
    p = loc.local_path_from_key(root, "recordings/2026/06/x.mp3")
    assert p.startswith(os.path.realpath(root) + os.sep) or p.startswith(root + os.sep)
    assert p.endswith("x.mp3")


def test_relative_key_from_local_path_outside_raises():
    root = tempfile.mkdtemp()
    with pytest.raises(ValueError):
        loc.relative_key_from_local_path("/etc/passwd", root)


def test_relative_key_from_local_path_roundtrip():
    root = tempfile.mkdtemp()
    key = "recordings/2026/a.mp3"
    abspath = loc.local_path_from_key(root, key)
    assert loc.relative_key_from_local_path(abspath, root) == key


def test_parse_locator_schemes():
    assert loc.parse_locator("local://recordings/x.mp3").scheme == "local"
    assert loc.parse_locator("s3://bucket/key.mp3").scheme == "s3"
    assert loc.parse_locator("/data/uploads/old.mp3").scheme == "legacy_local_abs"
    assert loc.parse_locator("recordings/rel.mp3").scheme == "legacy_local_rel"
    assert loc.parse_locator("") is None
    assert loc.parse_locator(None) is None


def test_parse_locator_bad_s3_raises():
    with pytest.raises(ValueError):
        loc.parse_locator("s3://bucketonly")  # no '/' -> missing key


def test_build_locators_normalize():
    assert loc.build_local_locator("recordings/x.mp3") == "local://recordings/x.mp3"
    assert loc.build_local_locator("/leading/slash") == "local://leading/slash"
    assert loc.build_s3_locator("bkt", "k/x.mp3") == "s3://bkt/k/x.mp3"


# ---------------------------------------------------------------------------
# is_probably_windows_abs / is_absolute_local_path
# Mutation survivors (2026-06-25): the length guard and both return branches
# in is_probably_windows_abs, plus the return branches of is_absolute_local_path.
# ---------------------------------------------------------------------------

def test_is_probably_windows_abs_true_cases():
    assert loc.is_probably_windows_abs("C:\\x") is True
    assert loc.is_probably_windows_abs("C:/x") is True
    assert loc.is_probably_windows_abs("D:\\path\\file") is True
    # exactly len 3 must still be True (kills a `< 3` -> `<= 3` style mutation)
    assert loc.is_probably_windows_abs("C:\\") is True


def test_is_probably_windows_abs_false_cases():
    # None / empty / too short -> the `not path or len(path) < 3` guard
    assert loc.is_probably_windows_abs(None) is False
    assert loc.is_probably_windows_abs("") is False
    assert loc.is_probably_windows_abs("ab") is False
    assert loc.is_probably_windows_abs("C:") is False
    # third char is not a separator -> the final `return ...` evaluates False
    assert loc.is_probably_windows_abs("C:x") is False
    # second char is not a colon
    assert loc.is_probably_windows_abs("abc") is False


def test_is_absolute_local_path():
    # empty -> the `if not value: return False` branch
    assert loc.is_absolute_local_path("") is False
    # windows abs -> the `return True` branch (would fall through to
    # os.path.isabs() and become False on POSIX if that return is broken)
    assert loc.is_absolute_local_path("C:\\x") is True
    assert loc.is_absolute_local_path("/abs/path") is True
    assert loc.is_absolute_local_path("relative/path") is False


# ---------------------------------------------------------------------------
# parse_locator s3 validation (line 64: `if not bucket or not key`)
# ---------------------------------------------------------------------------

def test_parse_locator_s3_missing_bucket_raises():
    # empty bucket: `not bucket` is True. An `or`->`and` mutation would let
    # this through, so it must raise.
    with pytest.raises(ValueError):
        loc.parse_locator("s3:///key.mp3")


def test_parse_locator_s3_missing_key_raises():
    # trailing slash leaves an empty key after normalization.
    with pytest.raises(ValueError):
        loc.parse_locator("s3://bucket/")
