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
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='CASCADE'), nullable=False, index=True)

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

    # JSON: the task a submit-and-poll transcription service accepted for this
    # job, so a retry polls it instead of submitting (and paying) again. See
    # src/services/transcription/provider_tasks.py.
    provider_task = db.Column(db.Text, nullable=True)

    # Parts finished / parts in total, for a job that works through its input
    # in parts (translation chunks). Written once per part and cleared when a
    # worker claims the job, so a retry starts again from 0.
    progress_done = db.Column(db.Integer, nullable=True)
    progress_total = db.Column(db.Integer, nullable=True)
    # The unit of progress_done / progress_total: 'chunks' (parts finished of
    # parts in total: audio chunks, translation chunks) or 'fraction' (a
    # measured share of the work, stored as done out of 100). NULL with a
    # total set is a row written before this column existed: chunks.
    progress_kind = db.Column(db.String(20), nullable=True)
    # The step of the pipeline the running job has reached, when its worker
    # reported one (titling, summarizing); NULL otherwise, and
    # the step then follows from the job type. See job_stage().
    stage = db.Column(db.String(30), nullable=True)
    # What the transcription service says it is doing inside that step
    # (preparing, transcribing, aligning, diarizing); NULL when it says nothing.
    stage_detail = db.Column(db.String(30), nullable=True)

    # Set when the owner cancels a running job. The worker stops at its next
    # safe point (src/services/job_control.py, src/services/job_cancel.py).
    cancel_requested_at = db.Column(db.DateTime, nullable=True)
    # JSON: what a reprocess cleared when it was queued (transcript, summary,
    # events, status), so a cancel puts the recording back as it was. Emptied
    # as soon as the job ends.
    restore_state = db.Column(db.Text, nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    user = db.relationship('User', backref=db.backref('processing_jobs', lazy='dynamic', cascade='all, delete-orphan'))
    recording = db.relationship('Recording', backref=db.backref('processing_jobs', lazy='dynamic', cascade='all, delete-orphan'))

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
            'error_message': self.error_message,
            'progress_done': self.progress_done,
            'progress_total': self.progress_total,
            'progress': self.progress(),
            'progress_kind': self.progress_kind_or_default(),
            'stage': self.job_stage(),
            'stage_detail': self.stage_detail if self.status == 'processing' else None,
            'cancel_requested': self.cancel_requested_at is not None,
        }

    # Job types a cancel is refused for: the audio of the recording does not
    # exist until the job has assembled it.
    NOT_CANCELABLE = ('stitch', 'merge')

    # The step a running job of each type is in when its worker reported none.
    _DEFAULT_STAGES = {
        'convert': 'converting',
        'stitch': 'assembling',
        'merge': 'joining',
        'transcribe': 'transcribing',
        'reprocess_transcription': 'transcribing',
        'summarize': 'summarizing',
        'reprocess_summary': 'summarizing',
        'translate_transcription': 'translating',
        'index_chunks': 'indexing',
        'trim': 'trimming',
    }

    def job_stage(self):
        """A stable code for where the job is.

        queued, completed and failed for a job in that status. For a running
        job: the step its worker reported (titling,
        summarizing), else the step of its type (converting, assembling,
        joining, transcribing, summarizing, translating, indexing, trimming).
        """
        if self.status != 'processing':
            return self.status
        return self.stage or self._DEFAULT_STAGES.get(self.job_type, 'processing')

    def progress_kind_or_default(self):
        """'chunks' or 'fraction' for a running job with a number, else None."""
        if self.status != 'processing' or not self.progress_total or self.progress_total <= 0:
            return None
        return self.progress_kind or 'chunks'

    def progress(self):
        """{'done', 'total'} or {'fraction'} for a running job with a real number, else None."""
        kind = self.progress_kind_or_default()
        if kind is None:
            return None
        done = max(0, min(self.progress_done or 0, self.progress_total))
        if kind == 'fraction':
            return {'fraction': round(done / self.progress_total, 4)}
        return {'done': done, 'total': self.progress_total}

    def can_cancel(self):
        """Whether DELETE on this job cancels it (a waiting or running job of a cancelable type)."""
        return self.status in ('queued', 'processing') and self.job_type not in self.NOT_CANCELABLE
