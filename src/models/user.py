"""
User and Speaker database models.

This module defines the User model for authentication and user profiles,
and the Speaker model for tracking speaker profiles used in diarization.
"""

from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import func
from src.database import db


class User(db.Model, UserMixin):
    """User model for authentication and profile management."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=True)
    sso_provider = db.Column(db.String(100), nullable=True)
    sso_subject = db.Column(db.String(255), unique=True, nullable=True)
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

    # Default naming template for title generation
    default_naming_template_id = db.Column(db.Integer, db.ForeignKey('naming_template.id', ondelete='SET NULL'), nullable=True)
    default_naming_template = db.relationship('NamingTemplate', foreign_keys=[default_naming_template_id])

    # Token budget (None = unlimited)
    monthly_token_budget = db.Column(db.Integer, nullable=True)

    # Transcription budget in seconds (None = unlimited)
    monthly_transcription_budget = db.Column(db.Integer, nullable=True)

    # Email verification fields
    email_verified = db.Column(db.Boolean, default=False)
    email_verification_token = db.Column(db.String(200), nullable=True, index=True)
    email_verification_sent_at = db.Column(db.DateTime, nullable=True)

    # Password reset fields
    password_reset_token = db.Column(db.String(200), nullable=True, index=True)
    password_reset_sent_at = db.Column(db.DateTime, nullable=True)

    # Auto speaker labelling settings
    auto_speaker_labelling = db.Column(db.Boolean, default=False)  # Enable auto-labelling when voice confidence exceeds threshold
    auto_speaker_labelling_threshold = db.Column(db.String(10), nullable=True, default='medium')  # 'low', 'medium', 'high'

    # Auto summarization setting (user can disable if admin hasn't globally disabled)
    auto_summarization = db.Column(db.Boolean, default=True)

    # Transcription hints (hotwords and initial prompt for improving ASR accuracy)
    transcription_hotwords = db.Column(db.Text, nullable=True)
    transcription_initial_prompt = db.Column(db.Text, nullable=True)

    # Parse meeting date from the uploaded filename (#342). When enabled,
    # a date extracted from the filename takes precedence over file
    # metadata / lastModified when setting meeting_date on upload.
    parse_filename_dates = db.Column(db.Boolean, default=False)
    filename_date_pattern = db.Column(db.String(50), nullable=True, default='auto')  # preset key or 'custom'
    filename_date_regex = db.Column(db.String(500), nullable=True)  # named-group regex for 'custom'

    # Filename template for auto-exported markdown files (#348). Supports
    # {{id}}, {{title}}, {{filename}}, {{date}}, {{datetime}}, {{time}},
    # {{year}}, {{month}}, {{day}}. Null = default "recording_{{id}}"
    # (exact legacy behavior).
    export_filename_template = db.Column(db.String(500), nullable=True)

    # Include timestamps in the transcript sent to the summarizer / chat (#304).
    # Independent per feature; the *_template_id selects a transcript template to
    # format the timestamps (null = a built-in default timestamp format).
    summary_include_timestamps = db.Column(db.Boolean, default=False)
    summary_timestamp_template_id = db.Column(db.Integer, db.ForeignKey('transcript_template.id', ondelete='SET NULL'), nullable=True)
    chat_include_timestamps = db.Column(db.Boolean, default=False)
    chat_timestamp_template_id = db.Column(db.Integer, db.ForeignKey('transcript_template.id', ondelete='SET NULL'), nullable=True)

    # UI/display preferences
    show_timestamps_simple_view = db.Column(db.Boolean, default=False)
    editor_autosave = db.Column(db.Boolean, default=False)
    # Audio player placement in the recording-detail view. 'bottom'
    # (default) keeps the player anchored under the content columns;
    # 'top' renders it above the columns so it sits closer to the
    # title bar. User-configurable from the Display tab in settings.
    audio_player_position = db.Column(db.String(10), default='bottom')

    def __repr__(self):
        return f"User('{self.username}', '{self.email}')"

    @staticmethod
    def normalize_email(value):
        """Canonical email form for storage and lookup: trimmed + lowercased.

        Email addresses are case-insensitive in practice (the domain always,
        the local part at every real provider), so normalizing on write keeps
        stored data clean and normalizing input before a lookup keeps matching
        consistent. Also strips the stray whitespace mobile autofill appends.
        """
        return value.strip().lower() if value else value

    @classmethod
    def find_by_email(cls, email):
        """Case-insensitive lookup by email.

        Uses ``func.lower`` on the column so it matches regardless of the
        stored case — a user whose address was stored mixed-case (before
        normalization existed, or via an SSO provider) still resolves, with
        no migration required.
        """
        if not email:
            return None
        return cls.query.filter(
            func.lower(cls.email) == cls.normalize_email(email)
        ).first()


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
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'use_count': self.use_count,
            'embedding_count': self.embedding_count,
            'confidence_score': self.confidence_score
        }
