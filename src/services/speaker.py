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


DEFAULT_LABEL = re.compile(r'^SPEAKER_\d+$', re.IGNORECASE)


def find_user_speaker(user_id, name):
    """The user's saved speaker with this name, ignoring case.

    "john" and "John" are the same person. An exact lookup created a second
    profile for each spelling, splitting the use counts and voice embeddings
    between them (#395). When older data already holds several spellings, the
    most-used one wins.
    """
    if not name:
        return None
    found = (Speaker.query
            .filter(Speaker.user_id == user_id, func.lower(Speaker.name) == name.strip().lower())
            .order_by(Speaker.use_count.desc(), Speaker.id.asc())
            .first())
    if found:
        return found
    # SQLite lower() folds ASCII only. Preserve saved spelling for Cyrillic
    # and other Unicode names just as for English names.
    return next((speaker for speaker in Speaker.query.filter_by(user_id=user_id)
                 .order_by(Speaker.use_count.desc(), Speaker.id.asc())
                 if speaker.name.strip().casefold() == name.strip().casefold()), None)


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

    Returns (names_used, label_to_name). label_to_name only holds real names,
    never another SPEAKER_XX label.
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
            if name not in names_used:
                names_used.append(name)

    return names_used, {k: v for k, v in label_to_name.items() if not DEFAULT_LABEL.match(v)}


def participants_from_segments(segments):
    """Comma-separated real speaker names, leaving out SPEAKER_XX labels."""
    names = {
        str(seg.get('speaker')).strip() for seg in segments
        if seg.get('speaker') and str(seg.get('speaker')).strip()
        and not DEFAULT_LABEL.match(str(seg.get('speaker')).strip())
    }
    return ', '.join(sorted(names))


def update_voice_profiles(recording, label_to_name, user, seconds_by_key=None):
    """Train voice profiles from the names given in one save, and refresh the
    recording's speaker snippets.

    label_to_name maps the speaker values the transcript showed before the
    save to the names now assigned; seconds_by_key is the speech per value,
    measured before the rename (None when the transcript has no timing).
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
            stats = apply_names_to_profiles(recording, label_to_name or {}, seconds_by_key, user)
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
                record_label_names(recording, label_to_name or {})
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
            if not name:
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
