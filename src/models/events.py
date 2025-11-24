"""
Event model for calendar events extracted from transcripts.

This module defines the Event model for storing calendar events
that are extracted from transcriptions.
"""

import json
from datetime import datetime
from src.database import db


class Event(db.Model):
    """Calendar events extracted from transcripts."""

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    start_datetime = db.Column(db.DateTime, nullable=False)
    end_datetime = db.Column(db.DateTime, nullable=True)
    location = db.Column(db.String(500), nullable=True)
    attendees = db.Column(db.Text, nullable=True)  # JSON list of attendees
    reminder_minutes = db.Column(db.Integer, nullable=True, default=15)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    recording = db.relationship('Recording', backref=db.backref('events', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'recording_id': self.recording_id,
            'title': self.title,
            'description': self.description,
            'start_datetime': self.start_datetime.isoformat() if self.start_datetime else None,
            'end_datetime': self.end_datetime.isoformat() if self.end_datetime else None,
            'location': self.location,
            'attendees': json.loads(self.attendees) if self.attendees else [],
            'reminder_minutes': self.reminder_minutes,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
