"""
Render the stored simplified-JSON transcript into text using a transcript-template
format string. Used to inject timestamps into the transcript sent to the
summarizer / chat (#304). Mirrors the placeholder/filter handling in
file_exporter.format_transcription_with_template so the formats line up.

Segment schema (see TranscriptionResponse.to_storage_format):
    {"speaker": str, "sentence": str, "start_time": float|None, "end_time": float|None}

Supported placeholders: {{index}} {{speaker}} {{text}} {{start_time}} {{end_time}}
Supported filters: |upper, and |srt on start_time/end_time.
"""

import json
import re
from datetime import timedelta

# Built-in default used when timestamps are requested but no template is chosen.
DEFAULT_TIMESTAMP_FORMAT = "[{{start_time}}] {{speaker}}: {{text}}"


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


def render_transcription(transcription_text, template_format):
    """Render the transcript JSON with `template_format`.

    Returns the rendered text, or None if the input isn't our segment-list JSON
    (caller should then fall back to its plain-text handling).
    """
    try:
        data = json.loads(transcription_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list):
        return None

    fmt = template_format or DEFAULT_TIMESTAMP_FORMAT
    lines = []
    for index, segment in enumerate(data, 1):
        line = fmt
        replacements = {
            '{{index}}': str(index),
            '{{speaker}}': segment.get('speaker', 'Unknown'),
            '{{text}}': segment.get('sentence', ''),
            '{{start_time}}': _fmt_time(segment.get('start_time')),
            '{{end_time}}': _fmt_time(segment.get('end_time')),
        }
        # SRT-time filters first (more specific than the |upper sweep).
        line = line.replace('{{start_time|srt}}', _fmt_srt_time(segment.get('start_time')))
        line = line.replace('{{end_time|srt}}', _fmt_srt_time(segment.get('end_time')))
        line = re.sub(
            r'\{\{(.*?)\|upper\}\}',
            lambda m: replacements.get('{{' + m.group(1) + '}}', '').upper(),
            line,
        )
        for key, value in replacements.items():
            line = line.replace(key, value)
        lines.append(line)
    return '\n'.join(lines)
