"""
Speaker identification and management services.
"""

from datetime import datetime
from flask import current_app
from flask_login import current_user

from src.database import db
from src.models import Speaker


def update_speaker_usage(speaker_names):
    """Helper function to update speaker usage statistics."""
    if not speaker_names or not current_user.is_authenticated:
        return

    try:
        for name in speaker_names:
            name = name.strip()
            if not name:
                continue

            speaker = Speaker.query.filter_by(user_id=current_user.id, name=name).first()
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
