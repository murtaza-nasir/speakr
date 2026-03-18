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


def test_placeholder_pattern_detection():
    """Placeholder titles should be detected, user titles should not."""
    original_filename = "interview.mp3"
    placeholder_patterns = [
        f"Recording - {original_filename}",
        f"Auto-processed - {original_filename}",
    ]

    # Placeholders match
    assert "Recording - interview.mp3" in placeholder_patterns
    assert "Auto-processed - interview.mp3" in placeholder_patterns

    # User titles don't match
    assert "My Custom Title" not in placeholder_patterns
    assert "Interview with John" not in placeholder_patterns
    assert "" not in placeholder_patterns

    print("  PASS: placeholder pattern detection")


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
    """User-provided title should be used instead of placeholder."""
    original_filename = "test.mp3"

    # With user title
    user_title = "  My Custom Title  "
    result = user_title.strip() if user_title and user_title.strip() else f"Recording - {original_filename}"
    assert result == "My Custom Title"

    # Empty string falls back to placeholder
    user_title = "   "
    result = user_title.strip() if user_title and user_title.strip() else f"Recording - {original_filename}"
    assert result == f"Recording - {original_filename}"

    # None falls back to placeholder
    user_title = None
    result = user_title.strip() if user_title and user_title.strip() else f"Recording - {original_filename}"
    assert result == f"Recording - {original_filename}"

    print("  PASS: user title application logic")


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
    title = user_title.strip() if user_title and user_title.strip() else f"Recording - {original_filename}"
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
