"""Links from transcript segments to saved speakers (speaker_id).

Each named segment of a JSON transcript stores speaker_id next to the
speaker name: the id of one of the recording owner's saved speakers. The name
stays as the display copy, so everything that reads names keeps working; a
rename or merge follows the id, and the API reports the speaker's current
name. A segment whose name matches no saved speaker, and a diarization label
(SPEAKER_00), has no id.

The id is found from, in order: the id the segment already has (when it
still belongs to the owner and its name matches), the voice sample saved for
that diarization label of this recording, and a saved speaker of the same
name (ignoring case). Voice samples are optional: without voice embeddings
the name match links every named speaker.
"""

import json
import re

from src.database import db

LABEL = re.compile(r'^SPEAKER_\d+$', re.IGNORECASE)


def owner_speakers(user_id):
    """({lowercased name: Speaker}, {id: Speaker}) for one user."""
    from src.models import Speaker
    by_name, by_id = {}, {}
    for speaker in Speaker.query.filter_by(user_id=user_id).order_by(Speaker.id):
        by_id[speaker.id] = speaker
        by_name.setdefault((speaker.name or '').strip().lower(), speaker)
    return by_name, by_id


def _sample_speakers(recording):
    """{diarization label: speaker id} from the voice samples of the recording."""
    from src.models import SpeakerVoiceSample
    result = {}
    for label, speaker_id in (db.session.query(SpeakerVoiceSample.label, SpeakerVoiceSample.speaker_id)
                              .filter(SpeakerVoiceSample.recording_id == recording.id,
                                      SpeakerVoiceSample.label.isnot(None))):
        result.setdefault(label, speaker_id)
    return result


def _labels_by_name(recording):
    """{lowercased name: [labels]} from the recording's label map."""
    result = {}
    for label, name in (recording.speaker_label_map or {}).items():
        if isinstance(name, str):
            result.setdefault(name.strip().lower(), []).append(label)
    return result


def resolve_id(name, current_id, by_name, by_id, samples, labels_by_name):
    """The speaker id for one segment name, or None."""
    if not isinstance(name, str) or not name.strip() or LABEL.match(name.strip()):
        return None
    key = name.strip().lower()
    if current_id in by_id and (by_id[current_id].name or '').strip().lower() == key:
        return current_id
    for label in labels_by_name.get(key, []):
        sid = samples.get(label)
        if sid in by_id and (by_id[sid].name or '').strip().lower() == key:
            return sid
    speaker = by_name.get(key)
    return speaker.id if speaker else None


def link_list(segments, recording, speakers=None):
    """Set speaker_id on a list of segment dicts in place (a save that is about
    to store them). Returns True when anything changed."""
    if recording.user_id is None or not isinstance(segments, list):
        return False
    by_name, by_id = speakers or owner_speakers(recording.user_id)
    samples = _sample_speakers(recording)
    labels_by_name = _labels_by_name(recording)
    changed = False
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        sid = resolve_id(seg.get('speaker'), seg.get('speaker_id'), by_name, by_id, samples, labels_by_name)
        if sid is None:
            if 'speaker_id' in seg:
                del seg['speaker_id']
                changed = True
        elif seg.get('speaker_id') != sid:
            seg['speaker_id'] = sid
            changed = True
    return changed


def link_segments(recording, speakers=None):
    """Set speaker_id on every segment of the recording's JSON transcript.

    Returns the new transcription text when anything changed, else None. Does
    not assign it: the caller decides how to write it.
    """
    if not recording.transcription or recording.user_id is None:
        return None
    try:
        segments = json.loads(recording.transcription)
    except (TypeError, ValueError):
        return None
    if not isinstance(segments, list):
        return None
    return json.dumps(segments) if link_list(segments, recording, speakers) else None


def link_recording(recording):
    """link_segments and assign the result (an ORM change; no commit)."""
    text = link_segments(recording)
    if text is not None:
        recording.transcription = text


def backfill_all(engine, logger=None, batch=200):
    """Link every stored JSON transcript, once (run_once 0003).

    Writes with plain UPDATE statements: adding the ids is not a change of
    the recording a client should be told about, so it does not move
    updated_at or send webhooks.
    """
    from sqlalchemy import text
    from src.models import Recording
    speakers_cache = {}
    linked = 0
    last_id = 0
    while True:
        rows = (Recording.query.filter(Recording.id > last_id, Recording.transcription.isnot(None))
                .order_by(Recording.id).limit(batch).all())
        if not rows:
            break
        updates = []
        for rec in rows:
            last_id = rec.id
            if rec.user_id not in speakers_cache:
                speakers_cache[rec.user_id] = owner_speakers(rec.user_id)
            new_text = link_segments(rec, speakers_cache[rec.user_id])
            if new_text is not None:
                updates.append({'id': rec.id, 't': new_text})
        db.session.rollback()          # nothing above is meant to be flushed
        if updates:
            with engine.begin() as conn:
                conn.execute(text('UPDATE recording SET transcription = :t WHERE id = :id'), updates)
            linked += len(updates)
    if logger:
        logger.info(f"Linked transcript speakers to saved speakers in {linked} recording(s)")
    return linked
