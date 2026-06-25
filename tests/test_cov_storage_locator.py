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
