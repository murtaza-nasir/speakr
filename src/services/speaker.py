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


def update_voice_profiles(recording, label_to_name, user):
    """Fold this recording's per-label voice embeddings into the named profiles
    and refresh the recording's speaker snippets.

    Both save routes call this. update_transcript used to skip it, so saving
    names alongside any line edit left voice profiles and snippets untouched.
    Returns (embeddings_updated, snippets_created). Never raises.
    """
    from src.services.speaker_embedding_matcher import update_speaker_embedding
    from src.services.speaker_snippets import create_speaker_snippets
    import json

    if not recording.speaker_embeddings or not label_to_name:
        return 0, 0

    embeddings_updated = 0
    snippets_created = 0
    try:
        embeddings_data = (json.loads(recording.speaker_embeddings)
                           if isinstance(recording.speaker_embeddings, str)
                           else recording.speaker_embeddings)
        for label, embedding in (embeddings_data or {}).items():
            name = label_to_name.get(label)
            if not name or not embedding or len(embedding) != 256:
                continue
            speaker = find_user_speaker(user.id, name)
            if not speaker:
                continue
            similarity = update_speaker_embedding(speaker, embedding, recording.id)
            embeddings_updated += 1
            if similarity is not None:
                current_app.logger.info(f"Updated voice profile for '{name}' (similarity: {similarity*100:.1f}%)")
            else:
                current_app.logger.info(f"Created initial voice profile for '{name}'")

        snippets_created = create_speaker_snippets(
            recording.id, {label: {'name': name} for label, name in label_to_name.items()})
        if snippets_created > 0:
            current_app.logger.info(f"Created {snippets_created} speaker snippets")
    except Exception as e:
        current_app.logger.error(f"Error updating speaker embeddings: {e}", exc_info=True)

    return embeddings_updated, snippets_created


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
