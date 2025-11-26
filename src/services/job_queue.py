"""
Fair database-backed job queue for background processing tasks.

This queue ensures:
- Jobs persist across application restarts
- Fair round-robin scheduling between users
- Limited concurrency to prevent overwhelming external services
- Automatic recovery of orphaned jobs
"""

import os
import json
import threading
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Configuration
MAX_WORKERS = int(os.environ.get('JOB_QUEUE_WORKERS', '2'))
MAX_RETRIES = int(os.environ.get('JOB_MAX_RETRIES', '3'))
POLL_INTERVAL = 1.0  # seconds between checking for new jobs


class FairJobQueue:
    """
    A database-backed job queue with fair scheduling across users.

    Uses round-robin scheduling to ensure all users get fair processing time,
    preventing one user from monopolizing the queue.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, num_workers: int = None):
        """Singleton pattern to ensure only one queue exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._num_workers_init = num_workers or MAX_WORKERS
        return cls._instance

    def __init__(self, num_workers: int = None):
        """Initialize the job queue."""
        if self._initialized:
            return

        self._num_workers = getattr(self, '_num_workers_init', num_workers or MAX_WORKERS)
        self._workers = []
        self._running = False
        self._app = None
        self._last_user_id = None  # For round-robin tracking
        self._initialized = True

        logger.info(f"FairJobQueue initialized with {self._num_workers} workers")

    def init_app(self, app):
        """Initialize with Flask app for context management."""
        self._app = app

    @contextmanager
    def _app_context(self):
        """Get application context for database operations."""
        if self._app:
            with self._app.app_context():
                yield
        else:
            yield

    def start(self):
        """Start the worker threads."""
        if self._running:
            return

        self._running = True
        for i in range(self._num_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"FairJobQueueWorker-{i}",
                daemon=True
            )
            worker.start()
            self._workers.append(worker)

        logger.info(f"Started {self._num_workers} fair job queue workers")

    def stop(self):
        """Stop the worker threads gracefully."""
        self._running = False
        for worker in self._workers:
            worker.join(timeout=5)
        self._workers.clear()
        logger.info("Fair job queue workers stopped")

    def _worker_loop(self):
        """Main worker loop that processes jobs from the database."""
        while self._running:
            try:
                job = self._claim_next_job()
                if job:
                    self._process_job(job)
                else:
                    # No jobs available, sleep briefly
                    time.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)
                time.sleep(POLL_INTERVAL)

    def _claim_next_job(self):
        """
        Claim the next job using fair round-robin scheduling.

        Returns the claimed job or None if no jobs available.
        """
        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob

            try:
                # Get list of users with queued jobs, ordered by oldest job first
                users_with_jobs = db.session.query(
                    ProcessingJob.user_id
                ).filter(
                    ProcessingJob.status == 'queued'
                ).group_by(
                    ProcessingJob.user_id
                ).order_by(
                    db.func.min(ProcessingJob.created_at)
                ).all()

                if not users_with_jobs:
                    return None

                user_ids = [u[0] for u in users_with_jobs]

                # Round-robin: pick next user after last processed
                next_user_id = None
                if self._last_user_id is not None and self._last_user_id in user_ids:
                    idx = user_ids.index(self._last_user_id)
                    next_user_id = user_ids[(idx + 1) % len(user_ids)]
                else:
                    next_user_id = user_ids[0]

                # Get oldest queued job for this user
                job = ProcessingJob.query.filter(
                    ProcessingJob.user_id == next_user_id,
                    ProcessingJob.status == 'queued'
                ).order_by(
                    ProcessingJob.created_at
                ).with_for_update(skip_locked=True).first()

                if job:
                    # Claim the job
                    job.status = 'processing'
                    job.started_at = datetime.utcnow()
                    db.session.commit()
                    self._last_user_id = next_user_id

                    wait_time = (datetime.utcnow() - job.created_at).total_seconds()
                    logger.info(f"Claimed job {job.id} (type={job.job_type}) for user {job.user_id}, recording {job.recording_id} (waited {wait_time:.1f}s)")
                    return job

                return None

            except Exception as e:
                logger.error(f"Error claiming job: {e}", exc_info=True)
                db.session.rollback()
                return None

    def _process_job(self, job):
        """Process a single job by dispatching to the appropriate task function."""
        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob, Recording
            from flask import current_app

            try:
                # Parse job parameters
                params = json.loads(job.params) if job.params else {}

                # Get recording
                recording = db.session.get(Recording, job.recording_id)
                if not recording:
                    raise ValueError(f"Recording {job.recording_id} not found")

                # Dispatch based on job type
                if job.job_type == 'transcribe':
                    self._run_transcription(job, recording, params)
                elif job.job_type == 'summarize':
                    self._run_summarization(job, recording, params)
                elif job.job_type == 'reprocess_transcription':
                    self._run_reprocess_transcription(job, recording, params)
                elif job.job_type == 'reprocess_summary':
                    self._run_reprocess_summary(job, recording, params)
                else:
                    raise ValueError(f"Unknown job type: {job.job_type}")

                # Mark as completed
                job.status = 'completed'
                job.completed_at = datetime.utcnow()
                db.session.commit()

                logger.info(f"Job {job.id} completed successfully")

            except Exception as e:
                logger.error(f"Job {job.id} failed: {e}", exc_info=True)

                # Update job with error
                job.error_message = str(e)
                job.retry_count += 1

                if job.retry_count < MAX_RETRIES:
                    # Re-queue for retry
                    job.status = 'queued'
                    job.started_at = None
                    logger.info(f"Job {job.id} re-queued for retry ({job.retry_count}/{MAX_RETRIES})")
                else:
                    job.status = 'failed'
                    job.completed_at = datetime.utcnow()
                    # Update recording status to FAILED
                    recording = db.session.get(Recording, job.recording_id)
                    if recording:
                        recording.status = 'FAILED'
                        recording.error_message = str(e)
                    logger.error(f"Job {job.id} failed permanently after {MAX_RETRIES} retries")

                db.session.commit()

    def _run_transcription(self, job, recording, params):
        """Run transcription task. Status updates handled by task function."""
        from src.tasks.processing import transcribe_audio_task
        from flask import current_app

        filepath = recording.audio_path
        filename_for_asr = recording.original_filename or os.path.basename(filepath)

        transcribe_audio_task(
            current_app._get_current_object().app_context(),
            recording.id,
            filepath,
            filename_for_asr,
            time.time(),
            language=params.get('language'),
            min_speakers=params.get('min_speakers'),
            max_speakers=params.get('max_speakers'),
            tag_id=params.get('tag_id')
        )

    def _run_summarization(self, job, recording, params):
        """Run summarization-only task. Status updates handled by task function."""
        from src.tasks.processing import generate_summary_only_task
        from flask import current_app

        generate_summary_only_task(
            current_app._get_current_object().app_context(),
            recording.id,
            custom_prompt_override=params.get('custom_prompt'),
            user_id=params.get('user_id')
        )

    def _run_reprocess_transcription(self, job, recording, params):
        """Run transcription reprocessing task. Status updates handled by task function."""
        from src.tasks.processing import transcribe_audio_task
        from flask import current_app

        filepath = recording.audio_path
        filename_for_asr = recording.original_filename or os.path.basename(filepath)

        transcribe_audio_task(
            current_app._get_current_object().app_context(),
            recording.id,
            filepath,
            filename_for_asr,
            time.time(),
            language=params.get('language'),
            min_speakers=params.get('min_speakers'),
            max_speakers=params.get('max_speakers'),
            tag_id=params.get('tag_id')
        )

    def _run_reprocess_summary(self, job, recording, params):
        """Run summary reprocessing task. Status updates handled by task function."""
        from src.tasks.processing import generate_summary_only_task
        from flask import current_app

        generate_summary_only_task(
            current_app._get_current_object().app_context(),
            recording.id,
            custom_prompt_override=params.get('custom_prompt'),
            user_id=params.get('user_id')
        )

    def enqueue(
        self,
        user_id: int,
        recording_id: int,
        job_type: str,
        params: Dict[str, Any] = None
    ) -> int:
        """
        Add a job to the database queue.

        Args:
            user_id: ID of the user who owns this job
            recording_id: ID of the recording to process
            job_type: Type of job (transcribe, summarize, reprocess_transcription, reprocess_summary)
            params: Optional parameters for the job

        Returns:
            The created job ID
        """
        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob, Recording

            # Check for existing active job for this recording
            existing = ProcessingJob.query.filter(
                ProcessingJob.recording_id == recording_id,
                ProcessingJob.status.in_(['queued', 'processing'])
            ).first()

            if existing:
                logger.warning(f"Job already exists for recording {recording_id}: {existing.id}")
                return existing.id

            # Create new job
            job = ProcessingJob(
                user_id=user_id,
                recording_id=recording_id,
                job_type=job_type,
                params=json.dumps(params) if params else None
            )
            db.session.add(job)

            # Update recording status to QUEUED
            recording = db.session.get(Recording, recording_id)
            if recording:
                recording.status = 'QUEUED'

            db.session.commit()

            # Auto-start workers if not running
            if not self._running:
                self.start()

            logger.info(f"Enqueued job {job.id} (type={job_type}) for user {user_id}, recording {recording_id}")
            return job.id

    def recover_orphaned_jobs(self):
        """
        Recover jobs that were processing when the app crashed.
        Call this on startup to reset orphaned jobs back to queued.
        """
        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob

            orphaned = ProcessingJob.query.filter(
                ProcessingJob.status == 'processing'
            ).all()

            for job in orphaned:
                job.status = 'queued'
                job.started_at = None
                logger.info(f"Recovered orphaned job {job.id} for recording {job.recording_id}")

            if orphaned:
                db.session.commit()
                logger.info(f"Recovered {len(orphaned)} orphaned jobs")

    def get_queue_status(self) -> Dict[str, Any]:
        """Get the current queue status."""
        with self._app_context():
            from src.models import ProcessingJob

            queued = ProcessingJob.query.filter_by(status='queued').count()
            processing = ProcessingJob.query.filter_by(status='processing').count()

            return {
                "queued_jobs": queued,
                "processing_jobs": processing,
                "num_workers": self._num_workers,
                "is_running": self._running
            }

    def get_position_in_queue(self, recording_id: int) -> Optional[int]:
        """Get the position of a recording's job in the queue (1-indexed)."""
        with self._app_context():
            from src.models import ProcessingJob

            job = ProcessingJob.query.filter(
                ProcessingJob.recording_id == recording_id,
                ProcessingJob.status == 'queued'
            ).first()

            if not job:
                return None

            # Count jobs created before this one
            position = ProcessingJob.query.filter(
                ProcessingJob.status == 'queued',
                ProcessingJob.created_at < job.created_at
            ).count() + 1

            return position

    def get_job_for_recording(self, recording_id: int):
        """Get the active job for a recording."""
        with self._app_context():
            from src.models import ProcessingJob

            return ProcessingJob.query.filter(
                ProcessingJob.recording_id == recording_id,
                ProcessingJob.status.in_(['queued', 'processing'])
            ).first()

    def cleanup_old_jobs(self, max_age_hours: int = 24):
        """Remove completed/failed jobs older than max_age_hours."""
        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob
            from datetime import timedelta

            cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)

            deleted = ProcessingJob.query.filter(
                ProcessingJob.status.in_(['completed', 'failed']),
                ProcessingJob.completed_at < cutoff
            ).delete(synchronize_session=False)

            if deleted:
                db.session.commit()
                logger.info(f"Cleaned up {deleted} old jobs")


# Global job queue instance
job_queue = FairJobQueue()
