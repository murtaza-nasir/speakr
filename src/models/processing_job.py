"""
ProcessingJob database model for persistent job queue.

This model stores background processing jobs in the database to ensure
they survive application restarts and support fair scheduling across users.
"""

from datetime import datetime
from src.database import db


class ProcessingJob(db.Model):
    """Database model for tracking background processing jobs."""

    __tablename__ = 'processing_job'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False, index=True)

    # Job type: transcribe, summarize, reprocess_transcription, reprocess_summary
    job_type = db.Column(db.String(50), nullable=False)

    # Status: queued, processing, completed, failed
    status = db.Column(db.String(20), default='queued', nullable=False, index=True)

    # JSON blob for job-specific parameters (language, min_speakers, custom_prompt, etc.)
    params = db.Column(db.Text, nullable=True)

    # Error tracking
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0, nullable=False)

    # Track if this is a new upload (vs reprocessing) - for cleanup on failure
    is_new_upload = db.Column(db.Boolean, default=False, nullable=False)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    user = db.relationship('User', backref=db.backref('processing_jobs', lazy='dynamic'))
    recording = db.relationship('Recording', backref=db.backref('processing_jobs', lazy='dynamic'))

    def __repr__(self):
        return f'<ProcessingJob {self.id} type={self.job_type} status={self.status}>'

    def to_dict(self):
        """Convert job to dictionary for API responses."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'recording_id': self.recording_id,
            'job_type': self.job_type,
            'status': self.status,
            'retry_count': self.retry_count,
            'is_new_upload': self.is_new_upload,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message
        }
