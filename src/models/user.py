"""
User and Speaker database models.

This module defines the User model for authentication and user profiles,
and the Speaker model for tracking speaker profiles used in diarization.
"""

from datetime import datetime
from flask_login import UserMixin
from src.database import db


class User(db.Model, UserMixin):
    """User model for authentication and profile management."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    can_share_publicly = db.Column(db.Boolean, default=True)  # Permission to create public share links
    recordings = db.relationship('Recording', backref='owner', lazy=True)
    transcription_language = db.Column(db.String(10), nullable=True)  # For ISO 639-1 codes
    output_language = db.Column(db.String(50), nullable=True)  # For full language names like "Spanish"
    ui_language = db.Column(db.String(10), nullable=True, default='en')  # For UI language preference (en, es, fr, zh)
    summary_prompt = db.Column(db.Text, nullable=True)
    extract_events = db.Column(db.Boolean, default=False)  # Enable event extraction from transcripts
    name = db.Column(db.String(100), nullable=True)
    job_title = db.Column(db.String(100), nullable=True)
    company = db.Column(db.String(100), nullable=True)
    diarize = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"User('{self.username}', '{self.email}')"


class Speaker(db.Model):
    """Speaker model for tracking voice profiles used in diarization."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, default=datetime.utcnow)
    use_count = db.Column(db.Integer, default=1)

    # Voice embedding fields (256 dimensions from WhisperX)
    average_embedding = db.Column(db.LargeBinary, nullable=True)  # Binary numpy array (256 × 4 bytes = 1024 bytes)
    embeddings_history = db.Column(db.JSON, nullable=True)  # List of metadata: [{recording_id, timestamp, similarity}, ...]
    embedding_count = db.Column(db.Integer, default=0)  # Number of embeddings collected
    confidence_score = db.Column(db.Float, nullable=True)  # 0-1 score based on embedding consistency

    # Relationship to user
    user = db.relationship('User', backref=db.backref('speakers', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'created_at': self.created_at,
            'last_used': self.last_used,
            'use_count': self.use_count,
            'embedding_count': self.embedding_count,
            'confidence_score': self.confidence_score
        }
