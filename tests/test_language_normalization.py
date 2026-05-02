#!/usr/bin/env python3
"""Tests for src.utils.language.normalize_language_code.

Run with: docker exec speakr-dev python /app/tests/test_language_normalization.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.language import normalize_language_code, SUPPORTED_CODES


PASSED = 0
FAILED = 0


def expect(value, expected):
    global PASSED, FAILED
    got = normalize_language_code(value)
    if got == expected:
        print(f"  ✓ normalize({value!r}) → {got!r}")
        PASSED += 1
    else:
        print(f"  ✗ normalize({value!r}) → {got!r} (expected {expected!r})")
        FAILED += 1


def main():
    print("=== normalize_language_code ===\n")

    # Empty / auto-detect
    expect(None, None)
    expect("", None)
    expect("   ", None)
    expect("auto", None)
    expect("Auto-Detect", None)

    # Already valid ISO codes
    expect("en", "en")
    expect("FR", "fr")
    expect("zh", "zh")
    expect("yue", "yue")  # 3-letter cantonese code

    # Display names — English
    expect("English", "en")
    expect("french", "fr")
    expect("German", "de")
    expect("Chinese", "zh")
    expect("Cantonese", "yue")

    # Native names — the actual issue #256 case
    expect("Français", "fr")
    expect("français", "fr")
    expect("Francais", "fr")
    expect("Deutsch", "de")
    expect("Español", "es")
    expect("Português", "pt")
    expect("русский", "ru")
    expect("日本語", "ja")
    expect("中文", "zh")

    # Locale codes
    expect("en-US", "en")
    expect("fr-FR", "fr")
    expect("zh_CN", "zh")
    expect("pt-BR", "pt")

    # Invalid → None (don't crash the ASR call, let auto-detect kick in)
    expect("Klingon", None)
    expect("xyz", None)
    expect("123", None)
    expect("???", None)

    # Sanity check the supported set
    assert "fr" in SUPPORTED_CODES
    assert "français" not in SUPPORTED_CODES, "supported set must hold codes only"

    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
