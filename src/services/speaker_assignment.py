"""Apply speaker names to a recording: one way for the web app and API v1.

Before #412 API v1 /speakers/assign was a copy of the web update_speakers that
skipped rebuilding the Inquire chunks, and neither route rewrote the auto-export
when no summary followed, so exports kept the old speaker labels. Both routes
now call apply_speaker_names; they keep their own access checks, input formats
and response shapes.
"""

import json
import re

from flask import current_app

from src.database import db


def apply_speaker_names(recording, user, speaker_map, regenerate_summary=False):
    """Rename speakers in the transcript and run what follows a rename.

    speaker_map: {label: {'name': str, 'isMe': bool}}. Updates participants,
    speaker usage and voice profiles, rebuilds the Inquire chunks, then either
    queues a summary or rewrites the auto-export. Returns a dict with
    summary_queued, embeddings_updated and snippets_created.
    """
    from src.services.speaker import (apply_speaker_map, participants_from_segments,
                                      update_speaker_usage, update_voice_profiles)
    from src.services.voice_profiles import speech_seconds_by_label

    transcription_text = recording.transcription or ''
    try:
        transcription_data = json.loads(transcription_text)
        is_json = isinstance(transcription_data, list)
    except (json.JSONDecodeError, TypeError):
        is_json = False

    label_to_name, seconds_by_key, names_used = {}, None, []
    if is_json:
        seconds_by_key = speech_seconds_by_label(transcription_data)
        names_used, label_to_name = apply_speaker_map(transcription_data, speaker_map, user)
        recording.transcription = json.dumps(transcription_data)
        recording.participants = participants_from_segments(transcription_data)
    else:
        for label, info in speaker_map.items():
            name = (info.get('name') or '').strip()
            if info.get('isMe') and not name:
                name = user.name or 'Me'
            if name:
                transcription_text = re.sub(r'\[\s*' + re.escape(label) + r'\s*\]', f'[{name}]',
                                            transcription_text, flags=re.IGNORECASE)
                if name not in names_used:
                    names_used.append(name)
        recording.transcription = transcription_text
        if names_used:
            recording.participants = ', '.join(names_used)

    if names_used:
        update_speaker_usage(names_used)
    embeddings_updated, snippets_created = update_voice_profiles(recording, label_to_name, user, seconds_by_key) or (0, 0)
    db.session.commit()

    # The transcript text changed: rebuild the Inquire chunks (background).
    from src.api import recordings as recordings_api
    recordings_api.reindex_recording_chunks_async(recording.id)

    summary_queued = False
    if regenerate_summary:
        from src.services.job_queue import job_queue
        job_queue.enqueue(user_id=user.id, recording_id=recording.id, job_type='summarize',
                          params={'user_id': user.id})
        summary_queued = True
    else:
        # Without a new summary, rewrite the export now so it shows the names.
        from src.file_exporter import ENABLE_AUTO_EXPORT, export_recording
        if ENABLE_AUTO_EXPORT:
            try:
                export_recording(recording.id)
            except Exception as e:
                current_app.logger.warning(f"Export after speaker update failed for {recording.id}: {e}")

    return {'summary_queued': summary_queued, 'embeddings_updated': embeddings_updated,
            'snippets_created': snippets_created}
