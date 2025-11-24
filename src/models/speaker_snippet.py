"""
SpeakerSnippet database model.

This module defines the SpeakerSnippet model for storing example quotes/snippets
from recordings that feature specific speakers. These snippets provide context
when viewing speaker profiles and help users verify speaker identifications.
"""

from datetime import datetime
from src.database import db


class SpeakerSnippet(db.Model):
    """Model for storing representative speech snippets from speakers."""

    __tablename__ = 'speaker_snippet'

    id = db.Column(db.Integer, primary_key=True)
    speaker_id = db.Column(db.Integer, db.ForeignKey('speaker.id', ondelete='CASCADE'), nullable=False)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='CASCADE'), nullable=False)
    segment_index = db.Column(db.Integer, nullable=False)  # Index in the transcript
    text_snippet = db.Column(db.String(200), nullable=False)  # The actual quote
    timestamp = db.Column(db.Float, nullable=True)  # Seconds into the recording
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    speaker = db.relationship('Speaker', backref=db.backref('snippets', lazy=True, cascade='all, delete-orphan'))
    recording = db.relationship('Recording', backref=db.backref('speaker_snippets', lazy=True))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'speaker_id': self.speaker_id,
            'recording_id': self.recording_id,
            'text': self.text_snippet,
            'timestamp': self.timestamp,
            'recording_title': self.recording.title if self.recording else 'Unknown',
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    def __repr__(self):
        return f"SpeakerSnippet(speaker_id={self.speaker_id}, recording_id={self.recording_id}, text='{self.text_snippet[:30]}...')"
