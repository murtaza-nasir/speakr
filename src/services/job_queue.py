"""
Fair database-backed job queue for background processing tasks.

This queue ensures:
- Jobs persist across application restarts
- Fair round-robin scheduling between users
- Separate queues for transcription (slow) and summary (fast) jobs
- Limited concurrency to prevent overwhelming external services
- Automatic recovery of orphaned jobs
"""

import os
import json
import threading
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Configuration
TRANSCRIPTION_WORKERS = int(os.environ.get('JOB_QUEUE_WORKERS', '2'))
SUMMARY_WORKERS = int(os.environ.get('SUMMARY_QUEUE_WORKERS', '2'))
MAX_RETRIES = int(os.environ.get('JOB_MAX_RETRIES', '3'))
POLL_INTERVAL = 1.0  # seconds between checking for new jobs

# Job type categories
TRANSCRIPTION_JOBS = ['transcribe', 'reprocess_transcription']
SUMMARY_JOBS = ['summarize', 'reprocess_summary']


class FairJobQueue:
    """
    A database-backed job queue with fair scheduling across users.

    Uses separate queues for transcription and summary jobs to prevent
    slow transcription jobs from blocking fast summary jobs.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern to ensure only one queue exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the job queue."""
        if self._initialized:
            return

        self._transcription_workers = []
        self._summary_workers = []
        self._running = False
        self._app = None
        # Separate round-robin tracking for each queue
        self._last_user_id_transcription = None
        self._last_user_id_summary = None
        # Lock for claiming jobs (SQLite doesn't support row-level locking)
        self._claim_lock = threading.Lock()
        self._initialized = True

        logger.info(f"FairJobQueue initialized: {TRANSCRIPTION_WORKERS} transcription workers, {SUMMARY_WORKERS} summary workers")

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
        """Start the worker threads for both queues."""
        if self._running:
            return

        self._running = True

        # Start transcription workers
        for i in range(TRANSCRIPTION_WORKERS):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(TRANSCRIPTION_JOBS, 'transcription'),
                name=f"TranscriptionWorker-{i}",
                daemon=True
            )
            worker.start()
            self._transcription_workers.append(worker)

        # Start summary workers
        for i in range(SUMMARY_WORKERS):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(SUMMARY_JOBS, 'summary'),
                name=f"SummaryWorker-{i}",
                daemon=True
            )
            worker.start()
            self._summary_workers.append(worker)

        logger.info(f"Started {TRANSCRIPTION_WORKERS} transcription workers and {SUMMARY_WORKERS} summary workers")

    def stop(self):
        """Stop the worker threads gracefully."""
        self._running = False
        for worker in self._transcription_workers + self._summary_workers:
            worker.join(timeout=5)
        self._transcription_workers.clear()
        self._summary_workers.clear()
        logger.info("Job queue workers stopped")

    def _worker_loop(self, job_types: List[str], queue_name: str):
        """Main worker loop that processes jobs of specific types."""
        while self._running:
            try:
                job = self._claim_next_job(job_types, queue_name)
                if job:
                    self._process_job(job)
                else:
                    # No jobs available, sleep briefly
                    time.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"{queue_name.capitalize()} worker error: {e}", exc_info=True)
                time.sleep(POLL_INTERVAL)

    def _claim_next_job(self, job_types: List[str], queue_name: str):
        """
        Claim the next job of specified types using fair round-robin scheduling.

        Args:
            job_types: List of job types this worker handles
            queue_name: Name of the queue ('transcription' or 'summary')

        Returns the claimed job or None if no jobs available.
        """
        # Use lock to prevent race conditions (SQLite doesn't support row-level locking)
        with self._claim_lock:
            with self._app_context():
                from src.database import db
                from src.models import ProcessingJob

                try:
                    # Get list of users with queued jobs of our types
                    users_with_jobs = db.session.query(
                        ProcessingJob.user_id
                    ).filter(
                        ProcessingJob.status == 'queued',
                        ProcessingJob.job_type.in_(job_types)
                    ).group_by(
                        ProcessingJob.user_id
                    ).order_by(
                        db.func.min(ProcessingJob.created_at)
                    ).all()

                    if not users_with_jobs:
                        return None

                    user_ids = [u[0] for u in users_with_jobs]

                    # Get last user ID for this queue type
                    last_user_id = (self._last_user_id_transcription
                                   if queue_name == 'transcription'
                                   else self._last_user_id_summary)

                    # Round-robin: pick next user after last processed
                    next_user_id = None
                    if last_user_id is not None and last_user_id in user_ids:
                        idx = user_ids.index(last_user_id)
                        next_user_id = user_ids[(idx + 1) % len(user_ids)]
                    else:
                        next_user_id = user_ids[0]

                    # Get oldest queued job of our types for this user
                    candidate_job = ProcessingJob.query.filter(
                        ProcessingJob.user_id == next_user_id,
                        ProcessingJob.status == 'queued',
                        ProcessingJob.job_type.in_(job_types)
                    ).order_by(
                        ProcessingJob.created_at
                    ).first()

                    if candidate_job:
                        # Atomically claim the job - only succeeds if status is still 'queued'
                        # This prevents race conditions when multiple workers try to claim the same job
                        from sqlalchemy import update
                        claim_time = datetime.utcnow()
                        result = db.session.execute(
                            update(ProcessingJob)
                            .where(
                                ProcessingJob.id == candidate_job.id,
                                ProcessingJob.status == 'queued'  # Critical: only claim if still queued
                            )
                            .values(status='processing', started_at=claim_time)
                        )

                        if result.rowcount == 0:
                            # Job was already claimed by another worker - this is expected with multiple workers
                            logger.debug(f"[{queue_name.upper()}] Job {candidate_job.id} already claimed by another worker")
                            db.session.rollback()
                            return None

                        # Also update Recording.status to reflect active processing
                        from src.models import Recording
                        recording = db.session.get(Recording, candidate_job.recording_id)
                        if recording and recording.status == 'QUEUED':
                            recording.status = 'PROCESSING'

                        db.session.commit()

                        # Refresh the job object to get updated values
                        db.session.refresh(candidate_job)

                        # Update last user ID for this queue
                        if queue_name == 'transcription':
                            self._last_user_id_transcription = next_user_id
                        else:
                            self._last_user_id_summary = next_user_id

                        wait_time = (claim_time - candidate_job.created_at).total_seconds()
                        logger.info(f"[{queue_name.upper()}] Claimed job {candidate_job.id} (type={candidate_job.job_type}) for user {candidate_job.user_id}, recording {candidate_job.recording_id} (waited {wait_time:.1f}s)")
                        return candidate_job

                    return None

                except Exception as e:
                    logger.error(f"Error claiming {queue_name} job: {e}", exc_info=True)
                    db.session.rollback()
                    return None

    def _process_job(self, job):
        """Process a single job by dispatching to the appropriate task function."""
        job_id = job.id
        job_type = job.job_type
        recording_id = job.recording_id
        params_str = job.params
        is_new_upload = job.is_new_upload

        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob, Recording
            from flask import current_app

            try:
                # Parse job parameters
                params = json.loads(params_str) if params_str else {}

                # Re-fetch the job in this session context to ensure it's attached
                job = db.session.get(ProcessingJob, job_id)
                if not job:
                    logger.error(f"Job {job_id} not found when trying to process")
                    return

                # Get recording
                recording = db.session.get(Recording, recording_id)
                if not recording:
                    raise ValueError(f"Recording {recording_id} not found")

                # Dispatch based on job type
                if job_type == 'transcribe':
                    self._run_transcription(job, recording, params)
                elif job_type == 'summarize':
                    self._run_summarization(job, recording, params)
                elif job_type == 'reprocess_transcription':
                    self._run_reprocess_transcription(job, recording, params)
                elif job_type == 'reprocess_summary':
                    self._run_reprocess_summary(job, recording, params)
                else:
                    raise ValueError(f"Unknown job type: {job_type}")

                # Mark as completed - re-fetch to ensure we have latest state
                job = db.session.get(ProcessingJob, job_id)
                if job:
                    job.status = 'completed'
                    job.completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.info(f"Job {job_id} completed successfully")

            except Exception as e:
                logger.error(f"Job {job_id} failed: {e}", exc_info=True)

                # Re-fetch job to update it
                job = db.session.get(ProcessingJob, job_id)
                if job:
                    job.error_message = str(e)
                    job.retry_count += 1

                    if job.retry_count < MAX_RETRIES:
                        # Re-queue for retry
                        job.status = 'queued'
                        job.started_at = None
                        logger.info(f"Job {job_id} re-queued for retry ({job.retry_count}/{MAX_RETRIES})")
                    else:
                        job.status = 'failed'
                        job.completed_at = datetime.utcnow()
                        recording = db.session.get(Recording, recording_id)

                        if is_new_upload and recording:
                            # For failed new uploads, delete the recording and file
                            logger.info(f"Deleting failed new upload: recording {recording_id}")
                            try:
                                # Delete the audio file
                                if recording.audio_path and os.path.exists(recording.audio_path):
                                    os.remove(recording.audio_path)
                                    logger.info(f"Deleted audio file: {recording.audio_path}")
                                # Delete ALL processing jobs for this recording (required due to NOT NULL constraint)
                                ProcessingJob.query.filter_by(recording_id=recording_id).delete()
                                # Delete the recording from database
                                db.session.delete(recording)
                            except Exception as delete_err:
                                logger.error(f"Error deleting failed upload recording: {delete_err}")
                        elif recording:
                            # For reprocessing failures, just mark as failed
                            recording.status = 'FAILED'
                            recording.error_message = str(e)

                        logger.error(f"Job {job_id} failed permanently after {MAX_RETRIES} retries")

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
            datetime.utcnow(),
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
            datetime.utcnow(),
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
        params: Dict[str, Any] = None,
        is_new_upload: bool = False
    ) -> int:
        """
        Add a job to the database queue.

        Args:
            user_id: ID of the user who owns this job
            recording_id: ID of the recording to process
            job_type: Type of job (transcribe, summarize, reprocess_transcription, reprocess_summary)
            params: Optional parameters for the job
            is_new_upload: True if this is a new file upload (for cleanup on failure)

        Returns:
            The created job ID
        """
        with self._app_context():
            from src.database import db
            from src.models import ProcessingJob, Recording

            # Check for existing active job of the SAME TYPE for this recording
            # Allow different job types to coexist (e.g., transcribe and summarize)
            existing = ProcessingJob.query.filter(
                ProcessingJob.recording_id == recording_id,
                ProcessingJob.job_type == job_type,
                ProcessingJob.status.in_(['queued', 'processing'])
            ).first()

            if existing:
                logger.warning(f"Job of type {job_type} already exists for recording {recording_id}: {existing.id}")
                return existing.id

            # Create new job
            job = ProcessingJob(
                user_id=user_id,
                recording_id=recording_id,
                job_type=job_type,
                params=json.dumps(params) if params else None,
                is_new_upload=is_new_upload
            )
            db.session.add(job)

            # Update recording status based on job type
            recording = db.session.get(Recording, recording_id)
            if recording:
                if job_type in SUMMARY_JOBS:
                    recording.status = 'SUMMARIZING'
                else:
                    recording.status = 'QUEUED'

            db.session.commit()

            # Auto-start workers if not running
            if not self._running:
                self.start()

            queue_name = 'summary' if job_type in SUMMARY_JOBS else 'transcription'
            logger.info(f"Enqueued {queue_name} job {job.id} (type={job_type}) for user {user_id}, recording {recording_id}")
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
                queue_name = 'summary' if job.job_type in SUMMARY_JOBS else 'transcription'
                logger.info(f"Recovered orphaned {queue_name} job {job.id} for recording {job.recording_id}")

            if orphaned:
                db.session.commit()
                logger.info(f"Recovered {len(orphaned)} orphaned jobs")

    def get_queue_status(self) -> Dict[str, Any]:
        """Get the current queue status for both queues."""
        with self._app_context():
            from src.models import ProcessingJob

            transcription_queued = ProcessingJob.query.filter(
                ProcessingJob.status == 'queued',
                ProcessingJob.job_type.in_(TRANSCRIPTION_JOBS)
            ).count()
            transcription_processing = ProcessingJob.query.filter(
                ProcessingJob.status == 'processing',
                ProcessingJob.job_type.in_(TRANSCRIPTION_JOBS)
            ).count()

            summary_queued = ProcessingJob.query.filter(
                ProcessingJob.status == 'queued',
                ProcessingJob.job_type.in_(SUMMARY_JOBS)
            ).count()
            summary_processing = ProcessingJob.query.filter(
                ProcessingJob.status == 'processing',
                ProcessingJob.job_type.in_(SUMMARY_JOBS)
            ).count()

            return {
                "transcription_queue": {
                    "queued": transcription_queued,
                    "processing": transcription_processing,
                    "workers": TRANSCRIPTION_WORKERS
                },
                "summary_queue": {
                    "queued": summary_queued,
                    "processing": summary_processing,
                    "workers": SUMMARY_WORKERS
                },
                "is_running": self._running
            }

    def get_position_in_queue(self, recording_id: int) -> Optional[int]:
        """Get the position of a recording's job in its respective queue (1-indexed)."""
        with self._app_context():
            from src.models import ProcessingJob

            job = ProcessingJob.query.filter(
                ProcessingJob.recording_id == recording_id,
                ProcessingJob.status == 'queued'
            ).first()

            if not job:
                return None

            # Determine which queue this job is in
            job_types = SUMMARY_JOBS if job.job_type in SUMMARY_JOBS else TRANSCRIPTION_JOBS

            # Count jobs of the same type created before this one
            position = ProcessingJob.query.filter(
                ProcessingJob.status == 'queued',
                ProcessingJob.job_type.in_(job_types),
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
