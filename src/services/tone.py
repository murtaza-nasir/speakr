"""Voice tone for a recording: validation of the record an external scorer stores.

Speakr does not compute tone. An external tool scores the audio and PUTs the result to
/api/v1/recordings/<id>/tone; this module only checks the shape and size of that record
so a bad payload can never break the transcript view. Recordings without tone data
behave exactly as before.
"""

MAX_BYTES = 512 * 1024
ALLOWED_KEYS = {"schema_version", "scored_at", "audio_seconds", "model", "windows", "call", "job", "source"}


class ToneError(ValueError):
    """A payload that cannot be stored; the message is safe to show the caller."""


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise ToneError(f"{name} must be a finite number")
    return float(value)


def validate_tone(payload):
    """Returns a cleaned copy of `payload` or raises ToneError."""
    import json

    if not isinstance(payload, dict):
        raise ToneError("tone must be a JSON object")
    if len(json.dumps(payload)) > MAX_BYTES:
        raise ToneError("tone record is too large")
    unknown = set(payload) - ALLOWED_KEYS
    if unknown:
        raise ToneError("unknown field(s): " + ", ".join(sorted(unknown)))
    if payload.get("schema_version") != 1:
        raise ToneError("schema_version must be 1")
    windows = payload.get("windows")
    if not isinstance(windows, list):
        raise ToneError("windows must be a list")
    last_start = -1.0
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            raise ToneError(f"windows[{index}] must be an object")
        start, end = _number(window.get("start"), f"windows[{index}].start"), _number(window.get("end"), f"windows[{index}].end")
        if end <= start or start < 0:
            raise ToneError(f"windows[{index}] has an invalid time range")
        if start < last_start:
            raise ToneError("windows must be in time order")
        last_start = start
        if not isinstance(window.get("speaker", ""), str):
            raise ToneError(f"windows[{index}].speaker must be text")
        scores = window.get("scores")
        if not isinstance(scores, dict):
            raise ToneError(f"windows[{index}].scores must be an object")
        for name, value in scores.items():
            _number(value, f"windows[{index}].scores.{name}")
        standout = window.get("standout")
        if standout is not None:
            if not isinstance(standout, dict) or not isinstance(standout.get("label"), str) or not isinstance(standout.get("emoji"), str):
                raise ToneError(f"windows[{index}].standout needs a label and an emoji")
            if len(standout["emoji"]) > 8 or len(standout["label"]) > 40:
                raise ToneError(f"windows[{index}].standout is too long")
    if payload.get("call") is not None and not isinstance(payload["call"], dict):
        raise ToneError("call must be an object")
    return payload


def window_for_time(tone, seconds):
    """The window containing `seconds`, or None. Lines look their window up by time, so a
    split or merged line still finds it."""
    if not tone or seconds is None:
        return None
    for window in tone.get("windows") or []:
        if window["start"] <= seconds < window["end"]:
            return window
    return None
