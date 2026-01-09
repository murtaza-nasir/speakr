"""
Transcription usage tracking model for monitoring audio transcription consumption.
"""

from datetime import datetime, date
from src.database import db


class TranscriptionUsage(db.Model):
    """Daily transcription usage aggregates per user per connector type."""
    __tablename__ = 'transcription_usage'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    connector_type = db.Column(db.String(50), nullable=False)  # 'openai_whisper', 'openai_transcribe', 'asr_endpoint'

    # Audio duration tracking (in seconds for precision)
    audio_duration_seconds = db.Column(db.Integer, default=0)

    # Cost tracking ($0 for self-hosted ASR)
    estimated_cost = db.Column(db.Float, default=0.0)

    # Request count for this day/connector
    request_count = db.Column(db.Integer, default=0)

    # Model info (e.g., 'whisper-1', 'gpt-4o-transcribe', 'asr-endpoint')
    model_name = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('transcription_usage', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'date', 'connector_type', name='uq_user_date_connector'),
        db.Index('idx_transcription_user_date', 'user_id', 'date'),
    )

    def __repr__(self):
        return f'<TranscriptionUsage {self.user_id} {self.date} {self.connector_type}: {self.audio_duration_seconds}s>'
