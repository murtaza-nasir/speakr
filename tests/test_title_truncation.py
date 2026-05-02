#!/usr/bin/env python3
"""
Regression test for issue #260: Title generation sends Unicode escape sequences
(e.g. \\u0412...) instead of decoded Cyrillic characters.

Root cause was that _generate_ai_title() sliced the raw transcription JSON
*before* parsing it with json.loads(). When the slice cut a unicode escape
mid-sequence, the parse failed and format_transcription_for_llm() returned the
raw truncated text, with literal \\uXXXX escapes still in place. The title
prompt then embedded those escape sequences directly.

Fix: format the full transcription first, then truncate the formatted plain
text. This test ensures the formatted output for a Cyrillic transcript with a
small length limit contains decoded Cyrillic characters and no literal `\\u`
escapes.

Run with: docker exec speakr-dev python /app/tests/test_title_truncation.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.tasks.processing import format_transcription_for_llm


PASSED = 0
FAILED = 0


def run(name, func):
    global PASSED, FAILED
    try:
        func()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name}: EXCEPTION - {e}")
        FAILED += 1


def make_cyrillic_transcript():
    """Build an ASR-style JSON transcript stored with ensure_ascii=True (the default)."""
    segments = [
        {"speaker": "SPEAKER_00", "sentence": "Вобщем, давайте начнём встречу."},
        {"speaker": "SPEAKER_01", "sentence": "Хорошо, я готов."},
        {"speaker": "SPEAKER_00", "sentence": "Сегодня обсудим квартальный отчёт."},
    ]
    # ensure_ascii=True is Python's default. This is what's stored in the DB.
    return json.dumps(segments, ensure_ascii=True)


def test_full_transcript_decodes_cyrillic():
    """format_transcription_for_llm() decodes \\uXXXX escapes when JSON is valid."""
    raw = make_cyrillic_transcript()
    out = format_transcription_for_llm(raw)
    assert "Вобщем" in out, f"expected decoded Cyrillic, got: {out[:200]}"
    assert "\\u" not in out, f"unexpected literal escape in output: {out[:200]}"


def test_truncating_after_formatting_preserves_decoding():
    """When we format first then slice, output is decoded Cyrillic — even at small limits."""
    raw = make_cyrillic_transcript()
    formatted = format_transcription_for_llm(raw)
    # Truncate the *formatted* text (the new behavior).
    truncated = formatted[:60]
    assert "\\u" not in truncated, f"truncated output should not contain literal escapes: {truncated!r}"
    # The first segment text should still be present (or at least its prefix).
    assert any(ch in truncated for ch in "Вобщем"), f"expected Cyrillic prefix, got: {truncated!r}"


def test_truncating_before_formatting_breaks_decoding():
    """Reproduces the bug: slicing the raw JSON mid-escape breaks json.loads()."""
    raw = make_cyrillic_transcript()
    # Find a position partway through a \uXXXX escape so the slice is invalid JSON.
    escape_idx = raw.find("\\u")
    assert escape_idx > 0, "test setup: expected escape sequences in raw JSON"
    # Cut 3 chars into the 6-char escape sequence (e.g. `\u04` instead of `В`).
    bad_slice = raw[:escape_idx + 3]
    out_bad = format_transcription_for_llm(bad_slice)
    # The function falls back to returning the raw truncated text, which still
    # contains literal escapes. This documents the old, buggy behavior.
    assert "\\u" in out_bad, "expected literal escapes when JSON parse fails"


def test_short_limit_does_not_leak_escapes_after_fix():
    """End-to-end: even a tiny limit produces Cyrillic, not escape sequences."""
    raw = make_cyrillic_transcript()
    limit = 30  # very small — would have cut the raw JSON mid-escape
    formatted = format_transcription_for_llm(raw)
    transcript_text = formatted[:limit]
    assert "\\u" not in transcript_text, f"unexpected escapes at small limit: {transcript_text!r}"


def main():
    print("=== Issue #260: Title truncation Unicode escape regression ===\n")
    run("full transcript decodes Cyrillic", test_full_transcript_decodes_cyrillic)
    run("truncating AFTER formatting preserves decoding", test_truncating_after_formatting_preserves_decoding)
    run("truncating BEFORE formatting breaks decoding (documents bug)", test_truncating_before_formatting_breaks_decoding)
    run("small limit produces clean text after fix", test_short_limit_does_not_leak_escapes_after_fix)

    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
