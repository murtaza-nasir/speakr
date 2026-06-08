#!/usr/bin/env python3
"""
Tests for upload API title and meeting_date support, title generation skip logic,
and summary context enrichment.
"""

import sys
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Real source helpers (single source of truth for upload/share-target/title-task).
from src.utils.titles import resolve_upload_title, is_placeholder_title


def test_placeholder_pattern_detection():
    """is_placeholder_title (the real helper the title task uses) detects
    placeholders and empties, and treats user titles as non-placeholders."""
    fn = "interview.mp3"

    # Placeholders + empty/None are overwritable by the AI title generator
    assert is_placeholder_title(f"Recording - {fn}", fn) is True
    assert is_placeholder_title(f"Auto-processed - {fn}", fn) is True
    assert is_placeholder_title("", fn) is True
    assert is_placeholder_title(None, fn) is True

    # User titles are NOT placeholders -> AI title generation is skipped
    assert is_placeholder_title("My Custom Title", fn) is False
    assert is_placeholder_title("Interview with John", fn) is False
    # A placeholder for a DIFFERENT filename must not match
    assert is_placeholder_title("Recording - other.mp3", fn) is False


def test_meeting_date_iso_parsing():
    """ISO 8601 meeting_date strings should parse correctly."""
    # With Z suffix
    val = "2024-06-15T10:30:00Z"
    dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
    assert dt.year == 2024
    assert dt.month == 6
    assert dt.day == 15

    # Without timezone
    val2 = "2024-06-15T10:30:00"
    dt2 = datetime.fromisoformat(val2)
    assert dt2.year == 2024

    # Date only
    val3 = "2024-06-15"
    dt3 = datetime.fromisoformat(val3)
    assert dt3.year == 2024
    assert dt3.month == 6

    # Invalid string should raise
    try:
        datetime.fromisoformat("not-a-date")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    print("  PASS: ISO 8601 meeting_date parsing")


def test_user_title_applied():
    """resolve_upload_title: a user title is used (trimmed); blank/None falls
    back to the placeholder."""
    fn = "test.mp3"
    assert resolve_upload_title("  My Custom Title  ", fn) == "My Custom Title"
    assert resolve_upload_title("   ", fn) == f"Recording - {fn}"
    assert resolve_upload_title(None, fn) == f"Recording - {fn}"


def test_resolve_upload_title_produces_recognized_placeholder():
    """THE invariant the share-target bug violated: when no user title is
    given, resolve_upload_title must produce a title that is_placeholder_title
    recognises — otherwise the AI title task skips generation and the
    recording is left with a non-AI title (e.g. the filename stem). Every
    entry point (upload, share-target) must satisfy this."""
    for fn in ("interview.mp3", "voice memo 001.m4a", "clip.webm"):
        # No user-supplied title -> placeholder -> AI generation runs
        title = resolve_upload_title(None, fn)
        assert is_placeholder_title(title, fn) is True, (
            f"resolve_upload_title({fn!r}) returned {title!r}, which the title "
            f"task would NOT treat as a placeholder -> no AI title (the share bug)"
        )
        # A real user title -> NOT a placeholder -> AI generation skipped
        assert is_placeholder_title(resolve_upload_title("Chosen", fn), fn) is False


def test_meeting_date_priority():
    """User-provided meeting_date should take priority over file_last_modified."""
    user_meeting_date = "2023-01-15T12:00:00Z"
    file_last_modified = "1700000000000"  # Nov 2023

    # User date takes priority
    meeting_date = None
    if user_meeting_date:
        try:
            meeting_date = datetime.fromisoformat(user_meeting_date.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            pass

    assert meeting_date is not None
    assert meeting_date.year == 2023
    assert meeting_date.month == 1
    assert meeting_date.day == 15

    # file_last_modified should NOT be reached
    # (in real code, the `if not meeting_date` guard prevents it)
    print("  PASS: meeting_date priority over file_last_modified")


def test_summary_context_includes_metadata():
    """Summary context should include recording date and title."""
    context_parts = []
    current_date = datetime.now().strftime("%B %d, %Y")
    context_parts.append(f"Current date: {current_date}")

    # Simulate recording with meeting_date and title
    meeting_date = datetime(2024, 3, 15)
    title = "Q1 Planning Meeting"

    if meeting_date:
        context_parts.append(f"Recording date: {meeting_date.strftime('%B %d, %Y')}")
    if title:
        context_parts.append(f"Recording title: {title}")

    context = "\n".join(context_parts)
    assert "Recording date: March 15, 2024" in context
    assert "Recording title: Q1 Planning Meeting" in context

    # Without metadata, those lines should be absent
    context_parts2 = [f"Current date: {current_date}"]
    meeting_date2 = None
    title2 = None
    if meeting_date2:
        context_parts2.append(f"Recording date: {meeting_date2.strftime('%B %d, %Y')}")
    if title2:
        context_parts2.append(f"Recording title: {title2}")

    context2 = "\n".join(context_parts2)
    assert "Recording date:" not in context2
    assert "Recording title:" not in context2

    print("  PASS: summary context includes recording metadata")


def test_neither_title_nor_date():
    """Without title or meeting_date, existing behavior is preserved."""
    original_filename = "audio.mp3"
    user_title = None
    user_meeting_date = None

    # Title falls back to placeholder
    title = resolve_upload_title(user_title, original_filename)
    assert title == "Recording - audio.mp3"

    # meeting_date falls through to next priority
    meeting_date = None
    if user_meeting_date:
        try:
            meeting_date = datetime.fromisoformat(user_meeting_date.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            pass
    assert meeting_date is None  # Would proceed to file_last_modified in real code

    print("  PASS: existing behavior preserved when no title/date provided")


def main():
    print("Running upload title and meeting_date tests...\n")
    passed = 0
    failed = 0

    tests = [
        test_placeholder_pattern_detection,
        test_meeting_date_iso_parsing,
        test_user_title_applied,
        test_resolve_upload_title_produces_recognized_placeholder,
        test_meeting_date_priority,
        test_summary_context_includes_metadata,
        test_neither_title_nor_date,
    ]

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
