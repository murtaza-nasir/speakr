"""Status codes in error text are matched only as separate tokens.

A WhisperX out-of-memory traceback was shown as "Authentication Error",
because "401" occurred in a Ray actor id. The same applied to "429" inside
any longer number.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.error_formatting import format_error_for_user

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
