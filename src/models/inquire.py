"""
InquireSession model for semantic search sessions.

This module defines the InquireSession model for tracking
inquire mode sessions and their filtering criteria.
"""

import json
from datetime import datetime
from src.database import db


class InquireSession(db.Model):
    """Tracks inquire mode sessions and their filtering criteria."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_name = db.Column(db.String(200), nullable=True)  # Optional user-defined name

    # Filter criteria (JSON stored as text)
    filter_tags = db.Column(db.Text, nullable=True)  # JSON array of tag IDs
    filter_speakers = db.Column(db.Text, nullable=True)  # JSON array of speaker names
    filter_date_from = db.Column(db.Date, nullable=True)
    filter_date_to = db.Column(db.Date, nullable=True)
    filter_recording_ids = db.Column(db.Text, nullable=True)  # JSON array of specific recording IDs

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('inquire_sessions', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'session_name': self.session_name,
            'filter_tags': json.loads(self.filter_tags) if self.filter_tags else [],
            'filter_speakers': json.loads(self.filter_speakers) if self.filter_speakers else [],
            'filter_date_from': self.filter_date_from.isoformat() if self.filter_date_from else None,
            'filter_date_to': self.filter_date_to.isoformat() if self.filter_date_to else None,
            'filter_recording_ids': json.loads(self.filter_recording_ids) if self.filter_recording_ids else [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used': self.last_used.isoformat() if self.last_used else None
        }
