"""
Render the stored simplified-JSON transcript into text using a transcript-template
format string. Every path that formats segments through a template uses
render_segments(): the download, the export, API v1 format=text, and the
timestamps sent to the summarizer / chat (#304).

Segment schema (see TranscriptionResponse.to_storage_format):
    {"speaker": str, "sentence": str, "start_time": float|None, "end_time": float|None}

Supported placeholders: {{index}} {{speaker}} {{text}} {{start_time}} {{end_time}}
Supported filters: |upper, and |srt and |vtt on start_time/end_time.
"""

import json
import re
from datetime import timedelta

# Built-in default used when timestamps are requested but no template is chosen.
DEFAULT_TIMESTAMP_FORMAT = "[{{start_time}}] {{speaker}}: {{text}}"

# Used by the download and the export when the user has no template.
BASIC_TEMPLATE_FORMAT = "[{{speaker}}]: {{text}}"

# {{name|upper}}. The name cannot contain braces, so a match never starts at
# an earlier placeholder on the same line.
_UPPER = re.compile(r'\{\{([^{}]*?)\|upper\}\}')


def _fmt_time(seconds):
    if seconds is None:
        return "00:00:00"
    td = timedelta(seconds=seconds)
    total = td.total_seconds()
    return f"{int(total // 3600):02d}:{int((total % 3600) // 60):02d}:{int(total % 60):02d}"


def _fmt_srt_time(seconds):
    if seconds is None:
        return "00:00:00,000"
    td = timedelta(seconds=seconds)
    total = td.total_seconds()
    return (f"{int(total // 3600):02d}:{int((total % 3600) // 60):02d}:"
            f"{int(total % 60):02d},{int((total % 1) * 1000):03d}")


def _fmt_vtt_time(seconds):
    """WebVTT cue time: MM:SS.mmm below one hour, HH:MM:SS.mmm from one hour on."""
    if seconds is None:
        return "00:00.000"
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


_TIME_FILTERS = (('srt', _fmt_srt_time), ('vtt', _fmt_vtt_time))


def parse_segments(transcription_text):
    """The segment list of a JSON transcript, or None for anything else."""
    try:
        data = json.loads(transcription_text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, list) else None


def render_segments(segments, template_format, header=None):
    """Render each segment with `template_format`, one rendering per segment,
    joined by newlines. A non-empty `header` comes first, followed by a blank
    line; trailing line breaks in it are ignored."""
    lines = []
    for index, segment in enumerate(segments, 1):
        start, end = segment.get('start_time'), segment.get('end_time')
        line = template_format
        replacements = {
            '{{index}}': str(index),
            '{{speaker}}': segment.get('speaker', 'Unknown'),
            '{{text}}': segment.get('sentence', ''),
            '{{start_time}}': _fmt_time(start),
            '{{end_time}}': _fmt_time(end),
        }
        # Time filters first (more specific than the |upper sweep).
        for name, fmt in _TIME_FILTERS:
            line = line.replace('{{start_time|' + name + '}}', fmt(start))
            line = line.replace('{{end_time|' + name + '}}', fmt(end))
        line = _UPPER.sub(
            lambda m: replacements.get('{{' + m.group(1) + '}}', '').upper(),
            line,
        )
        for key, value in replacements.items():
            line = line.replace(key, value)
        lines.append(line)
    body = '\n'.join(lines)
    header = (header or '').rstrip('\r\n')
    if header.strip():
        return f"{header}\n\n{body}"
    return body


def render_transcription(transcription_text, template_format):
    """Render the transcript JSON with `template_format`.

    Returns the rendered text, or None if the input isn't our segment-list JSON
    (caller should then fall back to its plain-text handling).
    """
    segments = parse_segments(transcription_text)
    if segments is None:
        return None
    return render_segments(segments, template_format or DEFAULT_TIMESTAMP_FORMAT)
