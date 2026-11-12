"""Status codes in error text are matched only as separate tokens.

A WhisperX out-of-memory traceback was shown as "Authentication Error",
because "401" occurred in a Ray actor id. The same applied to "429" inside
any longer number.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.error_formatting import ERROR_PATTERNS, format_error_for_user

OOM = ("ASR request failed with status 500: worker error: Traceback (most recent call last): "
       "ray.exceptions.RayTaskError: ActorID 8f2c401ab93e7d10 ... RuntimeError: CUDA failed with error out of memory")


def test_an_id_containing_401_is_not_an_authentication_error():
    assert format_error_for_user(OOM)["type"] != "auth"


@pytest.mark.parametrize("text,kind", [
    ("Error code: 401 - {'error': {'message': 'Unauthorized'}}", "auth"),
    ("HTTP 429 Too Many Requests", "rate_limit"),
    ("Error code: 429 - please slow down", "rate_limit"),
])
def test_real_status_codes_are_still_recognised(text, kind):
    assert format_error_for_user(text)["type"] == kind


def test_a_longer_number_is_not_a_status_code():
    assert format_error_for_user("used 1429 tokens, then the request failed")["type"] != "rate_limit"


def test_ffmpeg_wording_for_an_unreadable_file_is_a_format_error():
    # #482: FFmpeg says neither "corrupt" nor "unsupported format".
    text = ("Compression of x.mp3 to mp3 failed: ffmpeg version n8.1.3 ... "
            "Error opening input files: Invalid data found when processing input")
    assert format_error_for_user(text)["type"] == "format"


@pytest.mark.parametrize("text", [
    'OpenASR request failed (status 400): {"error":{"code":"no_speech"}}',
    'No speech detected in audio file',
    'FunASR task failed: ASR_RESPONSE_HAVE_NO_WORDS',
])
def test_explicit_no_speech_errors_have_dedicated_guidance(text):
    result = format_error_for_user(text)
    assert result["type"] == "no_speech"
    assert result["is_known"] is True
    assert result["icon"] == "fa-microphone-slash"


def test_an_empty_transcript_is_not_assumed_to_contain_no_speech():
    text = ("The transcription service returned no text. The recording may contain no "
            "speech, or the service failed without reporting an error.")
    assert format_error_for_user(text)["type"] != "no_speech"


@pytest.mark.parametrize("text", [
    'ASR request failed: no_speech',
    'FunASR task failed: ASR_RESPONSE_HAVE_NO_WORDS',
])
def test_the_no_speech_codes_match_as_whole_words(text):
    assert format_error_for_user(text)["type"] == "no_speech"


def test_whisper_no_speech_prob_field_is_not_a_no_speech_error():
    # Whisper's verbose JSON carries no_speech_prob on every segment, and it can
    # appear in an error body quoted inside the error text.
    text = ('Whisper request failed (status 400): {"segments":[{"id":0,'
            '"no_speech_prob":0.91,"text":""}],"error":"invalid request"}')
    assert format_error_for_user(text)["type"] != "no_speech"


def _js_no_speech_patterns():
    """The no-speech pattern list of parseUnformattedError, as Python regexes."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "static", "js", "app.modular.js")
    source = open(path, encoding="utf-8").read()
    block = source[:source.index("errors.noSpeechTitle")]
    line = block[block.rindex("patterns: ["):]
    line = line[len("patterns: ["):line.index("],")]
    patterns = []
    for item in re.findall(r"/((?:[^/\\]|\\.)+)/|'([^']*)'", line):
        regex, literal = item
        patterns.append(regex if regex else re.escape(literal))
    return patterns


def test_the_frontend_no_speech_patterns_match_the_backend_list():
    entry = next(p for p in ERROR_PATTERNS if p["type"] == "no_speech")
    assert _js_no_speech_patterns() == entry["patterns"]
