"""
Speaker identification and management services.
"""

import re
from datetime import datetime
from flask import current_app
from flask_login import current_user
from sqlalchemy import func

from src.database import db
from src.models import Speaker
from src.utils.speaker_labels import is_generic_speaker_label, is_real_speaker_name


# Kept for importers; the rule itself is src/utils/speaker_labels.py.
DEFAULT_LABEL = re.compile(r'^SPEAKER_\d+$', re.IGNORECASE)


def find_user_speaker(user_id, name):
    """The user's saved speaker with this name, ignoring case.

    "john" and "John" are the same person. An exact lookup created a second
    profile for each spelling, splitting the use counts and voice embeddings
    between them (#395). When older data already holds several spellings, the
    most-used one wins. A diarization label (SPEAKER_00) is never a saved
    speaker, so it finds nothing.
    """
    if not name or is_generic_speaker_label(name):
        return None
    return (Speaker.query
            .filter(Speaker.user_id == user_id, func.lower(Speaker.name) == name.strip().lower())
            .order_by(Speaker.use_count.desc(), Speaker.id.asc())
            .first())


def _resolve_name(info, user):
    name = (info or {}).get('name', '') or ''
    name = name.strip()
    if (info or {}).get('isMe') and not name:
        name = (user.name if user else None) or 'Me'
    if not name:
        return ''
    existing = find_user_speaker(user.id, name) if user else None
    return existing.name if existing else name


def apply_speaker_map(segments, speaker_map, user):
    """Rename the segments of a JSON transcript in place.

    speaker_map is {label: {'name': str, 'isMe': bool}}. A name that matches a
    saved speaker ignoring case takes the saved spelling, so the transcript,
    the speaker list and the voice profile all agree.

    A diarization label given as a name ("Speaker 1", SPEAKER_03) renames the
    turns like any other text and counts as not named: it is left out of both
    return values, so no saved speaker, voice sample or snippet follows.

    Returns (names_used, label_to_name). Both hold real names only.
    """
    label_to_name = {}
    spelling = {}   # lowercased name -> first spelling in this save
    for label, info in (speaker_map or {}).items():
        name = _resolve_name(info, user)
        if name:
            label_to_name[label] = spelling.setdefault(name.lower(), name)

    names_used = []
    for segment in segments:
        name = label_to_name.get(segment.get('speaker'))
        if name:
            segment['speaker'] = name
            if name not in names_used and not is_generic_speaker_label(name):
                names_used.append(name)

    return names_used, {k: v for k, v in label_to_name.items() if not is_generic_speaker_label(v)}


def labels_typed_as_names(speaker_map):
    """{speaker value: label} for the entries of a speaker map whose name is a
    diarization label ("Speaker 1", SPEAKER_03): turns a person un-named."""
    out = {}
    for key, info in (speaker_map or {}).items():
        name = ((info or {}).get('name') or '').strip() if isinstance(info, dict) else ''
        if name and is_generic_speaker_label(name):
            out[key] = name
    return out


def participants_from_segments(segments):
    """Comma-separated real speaker names, leaving out diarization labels."""
    names = {
        str(seg.get('speaker')).strip() for seg in segments
        if isinstance(seg, dict) and is_real_speaker_name(str(seg.get('speaker') or ''))
    }
    return ', '.join(sorted(names))


def update_voice_profiles(recording, label_to_name, user, seconds_by_key=None, shown_as=None):
    """Train voice profiles from the names given in one save, and refresh the
    recording's speaker snippets.

    label_to_name maps the speaker values the transcript showed before the
    save to the names now assigned; seconds_by_key is the speech per value,
    measured before the rename (None when the transcript has no timing).
    shown_as is labels_typed_as_names() of the same save: speaker values
    that were given a diarization label in place of a name.
    Both save routes and API v1 call this. Returns
    (samples_stored, snippets_created). Never raises.

    Commits: the caller's changes first, so they stand on their own, then the
    training, which is rolled back as a whole if it fails partway.
    (A savepoint would hold SQLite's write lock through the training and
    collide with background writers.)
    """
    from src.services.speaker_snippets import create_speaker_snippets
    from src.services.voice_profiles import apply_names_to_profiles, record_label_names

    stored = 0
    snippets_created = 0
    # Runs even when nothing was named: a save that only merged speakers
    # away must still drop the merged labels' samples.
    if recording.speaker_embeddings:
        db.session.commit()
        try:
            stats = apply_names_to_profiles(recording, label_to_name or {}, seconds_by_key, user,
                                            shown_as=shown_as)
            db.session.commit()
            stored = stats['stored']
            if stats['rejected'] or stats['skipped_short']:
                current_app.logger.info(
                    f"Recording {recording.id}: {stats['rejected']} voice samples kept out as unlike "
                    f"the named person, {stats['skipped_short']} skipped for too little speech")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating voice profiles: {e}", exc_info=True)
            try:
                record_label_names(recording, dict(label_to_name or {}, **(shown_as or {})))
                db.session.commit()
            except Exception as e2:
                db.session.rollback()
                current_app.logger.error(f"Error saving the speaker label map: {e2}", exc_info=True)
    try:
        if not label_to_name:
            return stored, 0
        snippets_created = create_speaker_snippets(
            recording.id, {label: {'name': name} for label, name in label_to_name.items()})
        if snippets_created > 0:
            current_app.logger.info(f"Created {snippets_created} speaker snippets")
    except Exception as e:
        current_app.logger.error(f"Error creating speaker snippets: {e}", exc_info=True)

    return stored, snippets_created


def update_speaker_usage(speaker_names):
    """Helper function to update speaker usage statistics."""
    if not speaker_names or not current_user.is_authenticated:
        return

    try:
        for name in speaker_names:
            name = name.strip()
            # A diarization label is not a person: no saved speaker for it.
            if not is_real_speaker_name(name):
                continue

            speaker = find_user_speaker(current_user.id, name)
            if speaker:
                speaker.use_count += 1
                speaker.last_used = datetime.utcnow()
            else:
                # Create new speaker
                speaker = Speaker(
                    name=name,
                    user_id=current_user.id,
                    use_count=1,
                    created_at=datetime.utcnow(),
                    last_used=datetime.utcnow()
                )
                db.session.add(speaker)

        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error updating speaker usage: {e}")
        db.session.rollback()
