#!/usr/bin/env python3
"""
#304: optional timestamps in the transcript handed to the summarizer / chat.

Covers the pure rendering layer (no app/DB needed):
  - render_transcription with the default + custom template formats
  - format_transcription_for_llm: timestamps on/off and plain-text passthrough

Run:
  docker run --rm -v $PWD:/app:ro -e UPLOAD_FOLDER=/tmp/up \
    -e ASR_BASE_URL=http://x:9999 speakr-test:cov \
    sh -c "cd /app && python -m pytest tests/test_transcript_timestamps.py -q"
"""

import json

from src.utils.transcript_render import render_transcription, DEFAULT_TIMESTAMP_FORMAT
from src.tasks.processing import format_transcription_for_llm

SEGMENTS = json.dumps([
    {"speaker": "Alice", "sentence": "Hello there", "start_time": 0.0, "end_time": 2.0},
    {"speaker": "Bob", "sentence": "Hi", "start_time": 65.0, "end_time": 67.0},
])


def test_render_default_format_has_hms_timestamps():
    out = render_transcription(SEGMENTS, DEFAULT_TIMESTAMP_FORMAT)
    assert out == "[00:00:00] Alice: Hello there\n[00:01:05] Bob: Hi"


def test_render_custom_template_and_srt_filter():
    out = render_transcription(SEGMENTS, "{{start_time|srt}} {{speaker|upper}}: {{text}}")
    assert out.splitlines()[0] == "00:00:00,000 ALICE: Hello there"
    assert out.splitlines()[1] == "00:01:05,000 BOB: Hi"


def test_render_returns_none_for_plain_text():
    # Not our JSON segment list -> caller falls back to plain handling.
    assert render_transcription("just a plain transcript", DEFAULT_TIMESTAMP_FORMAT) is None


def test_format_for_llm_timestamps_off_is_plain():
    out = format_transcription_for_llm(SEGMENTS, include_timestamps=False)
    assert out == "[Alice]: Hello there\n[Bob]: Hi"


def test_format_for_llm_timestamps_on_default():
    out = format_transcription_for_llm(SEGMENTS, include_timestamps=True)
    assert out == "[00:00:00] Alice: Hello there\n[00:01:05] Bob: Hi"


def test_format_for_llm_timestamps_on_custom_template():
    out = format_transcription_for_llm(
        SEGMENTS, include_timestamps=True, template_format="{{start_time}} {{speaker}}")
    assert out == "00:00:00 Alice\n00:01:05 Bob"


def test_format_for_llm_plain_text_passthrough_even_with_timestamps():
    # A non-JSON transcript can't be timestamped; it must pass through unchanged.
    assert format_transcription_for_llm("plain text", include_timestamps=True) == "plain text"
