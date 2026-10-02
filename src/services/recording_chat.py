"""The prompt for chatting with one recording: one builder for the web app
and API v1 (#412 audit C1, mailr spec G9).

The notes in the prompt are the ones the chatting user can see: the owner's
for the owner, a recipient's own personal notes otherwise.

with_sources: the transcript is sent with segment markers ([S12 12:34 Dana])
and the model is asked to cite [S12 "exact words"]; parse_sources turns those
into numbered sources and checks each quote against its segment.
"""

import re

_CITE = re.compile(r'\[S(\d+)(?:\s+"([^"\]]*)")?\]')


def _clock(seconds):
    if seconds is None:
        return '--:--'
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def _marked_transcript(segments):
    return '\n'.join(f"[S{seg['index']} {_clock(seg['start_time'])} {seg['speaker'] or 'Unknown'}] {seg['text']}"
                     for seg in segments)


def build_chat_messages(recording, user, message, history=None, with_sources=False):
    """(messages, segments). segments is the canonical list when with_sources
    is on and the transcript has segments, else None."""
    from src.models import SystemSetting
    from src.services.transcript_segments import canonical_segments
    from src.tasks.processing import _resolve_timestamp_template_format, format_transcription_for_llm

    segments = canonical_segments(recording) if with_sources else None
    use_markers = bool(segments)
    include_timestamps = bool(getattr(user, 'chat_include_timestamps', False)) and not use_markers
    if use_markers:
        transcript = _marked_transcript(segments)
    else:
        transcript = format_transcription_for_llm(
            recording.transcription,
            include_timestamps=include_timestamps,
            template_format=_resolve_timestamp_template_format(
                user, user.chat_timestamp_template_id) if include_timestamps else None,
        )
    limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if limit != -1:
        transcript = transcript[:limit]

    language = f"Please provide all your responses in {user.output_language}." if user.output_language else ""
    name = user.name or "User"
    title = user.job_title or "a professional"
    company = user.company or "their organization"

    citation = ""
    if use_markers:
        citation = (
            "\nEach transcript line starts with a marker such as [S12 12:34 Dana]: segment 12, its time and "
            "speaker. Support every statement about the recording with a citation of the segment and a short "
            "exact quote from it, written as [S12 \"exact words\"]. Quote words that appear in that segment; "
            "never invent a segment or a quote.\n")
    elif include_timestamps:
        citation = (
            "\nWhen you reference a specific moment in the recording, cite its "
            "timestamp in square brackets exactly as it appears in the transcript "
            "(for example [00:07:35]). The interface renders these as clickable "
            "links that start playback at that moment, so cite them wherever they "
            "support your answer. Never invent a timestamp that is not in the "
            "transcript.\n")

    notes = recording.get_user_notes(user)
    system_prompt = f"""You are a professional meeting and audio transcription analyst assisting {name}, who is a(n) {title} at {company}. {language} Analyze the following meeting information and respond to the specific request.
{citation}
Following are the meeting participants and their roles:
{recording.participants or "No specific participants information provided."}

Following is the meeting transcript:
<<start transcript>>
{transcript or "No transcript available."}
<<end transcript>>

Additional context and notes about the meeting:
{notes or "none"}
"""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": message})
    return messages, (segments if use_markers else None)


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().lower()


def parse_sources(reply, segments):
    """Replace [S12 "quote"] citations with [n]; returns (reply, sources)."""
    by_index = {seg['index']: seg for seg in segments or []}
    sources, numbers = [], {}

    def _replace(match):
        index, quote = int(match.group(1)), match.group(2)
        key = (index, _norm(quote))
        if key not in numbers:
            seg = by_index.get(index)
            numbers[key] = len(sources) + 1
            sources.append({
                'n': numbers[key],
                'segment_index': index if seg else None,
                'start_time': seg['start_time'] if seg else None,
                'end_time': seg['end_time'] if seg else None,
                'speaker': seg['speaker'] if seg else None,
                'quote': quote,
                'verified': bool(seg and quote and _norm(quote) in _norm(seg['text'])),
            })
        return f'[{numbers[key]}]'

    return _CITE.sub(_replace, reply or ''), sources
