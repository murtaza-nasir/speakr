"""
Simple job queue for background processing tasks.
Ensures jobs run sequentially or with limited concurrency to prevent
overwhelming external services like ASR endpoints.
"""

import threading
import queue
import logging
from datetime import datetime
from typing import Callable, Any, Dict, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class JobStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    """Represents a background processing job."""
    id: str
    func: Callable
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    recording_id: Optional[int] = None  # For tracking which recording this job is for


class JobQueue:
    """
    A simple job queue that processes jobs with limited concurrency.

    Features:
    - Configurable max concurrent workers
    - Job status tracking
    - Prevents duplicate jobs for the same recording
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, max_workers: int = 2):
        """Singleton pattern to ensure only one queue exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._max_workers_init = max_workers
        return cls._instance

    def __init__(self, max_workers: int = 2):
        """Initialize the job queue."""
        if self._initialized:
            return

        self._queue = queue.Queue()
        self._jobs: Dict[str, Job] = {}
        self._recording_jobs: Dict[int, str] = {}  # Maps recording_id to job_id
        self._max_workers = getattr(self, '_max_workers_init', max_workers)
        self._workers: list = []
        self._running = False
        self._jobs_lock = threading.Lock()
        self._initialized = True

        logger.info(f"JobQueue initialized with max_workers={self._max_workers}")

    def start(self):
        """Start the worker threads."""
        if self._running:
            return

        self._running = True
        for i in range(self._max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"JobQueueWorker-{i}",
                daemon=True
            )
            worker.start()
            self._workers.append(worker)

        logger.info(f"Started {self._max_workers} job queue workers")

    def stop(self):
        """Stop the worker threads."""
        self._running = False
        # Put None items to unblock workers
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=5)
        self._workers.clear()
        logger.info("Job queue workers stopped")

    def _worker_loop(self):
        """Main worker loop that processes jobs from the queue."""
        while self._running:
            try:
                job = self._queue.get(timeout=0.1)  # Check every 100ms for faster pickup
                if job is None:
                    continue

                self._process_job(job)
                self._queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Worker error: {e}")

    def _process_job(self, job: Job):
        """Process a single job."""
        wait_time = (datetime.utcnow() - job.created_at).total_seconds()
        logger.info(f"Starting job {job.id} for recording {job.recording_id} (waited {wait_time:.2f}s in queue)")

        with self._jobs_lock:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.utcnow()

        try:
            job.func(*job.args, **job.kwargs)

            with self._jobs_lock:
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.utcnow()

            logger.info(f"Job {job.id} completed successfully")

        except Exception as e:
            logger.error(f"Job {job.id} failed: {e}")

            with self._jobs_lock:
                job.status = JobStatus.FAILED
                job.completed_at = datetime.utcnow()
                job.error = str(e)

        finally:
            # Clean up recording job mapping
            with self._jobs_lock:
                if job.recording_id and job.recording_id in self._recording_jobs:
                    del self._recording_jobs[job.recording_id]

    def enqueue(
        self,
        func: Callable,
        args: tuple = (),
        kwargs: dict = None,
        job_id: str = None,
        recording_id: int = None
    ) -> Job:
        """
        Add a job to the queue.

        Args:
            func: The function to call
            args: Positional arguments for the function
            kwargs: Keyword arguments for the function
            job_id: Optional custom job ID
            recording_id: Optional recording ID to prevent duplicate jobs

        Returns:
            The created Job object
        """
        # Auto-start workers if not running
        if not self._running:
            self.start()

        if kwargs is None:
            kwargs = {}

        # Generate job ID if not provided
        if job_id is None:
            job_id = f"job_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}"

        # Check for existing job for this recording
        with self._jobs_lock:
            if recording_id and recording_id in self._recording_jobs:
                existing_job_id = self._recording_jobs[recording_id]
                existing_job = self._jobs.get(existing_job_id)
                if existing_job and existing_job.status in [JobStatus.QUEUED, JobStatus.RUNNING]:
                    logger.warning(f"Job already exists for recording {recording_id}: {existing_job_id}")
                    return existing_job

        # Create the job
        job = Job(
            id=job_id,
            func=func,
            args=args,
            kwargs=kwargs,
            recording_id=recording_id
        )

        with self._jobs_lock:
            self._jobs[job_id] = job
            if recording_id:
                self._recording_jobs[recording_id] = job_id

        # Add to queue
        self._queue.put(job)

        logger.info(f"Enqueued job {job_id} for recording {recording_id}, queue size: {self._queue.qsize()}")

        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID."""
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def get_job_for_recording(self, recording_id: int) -> Optional[Job]:
        """Get the active job for a recording."""
        with self._jobs_lock:
            job_id = self._recording_jobs.get(recording_id)
            if job_id:
                return self._jobs.get(job_id)
        return None

    def get_queue_status(self) -> Dict[str, Any]:
        """Get the current queue status."""
        with self._jobs_lock:
            queued = sum(1 for j in self._jobs.values() if j.status == JobStatus.QUEUED)
            running = sum(1 for j in self._jobs.values() if j.status == JobStatus.RUNNING)

        return {
            "queue_size": self._queue.qsize(),
            "queued_jobs": queued,
            "running_jobs": running,
            "max_workers": self._max_workers,
            "is_running": self._running
        }

    def get_position_in_queue(self, recording_id: int) -> Optional[int]:
        """Get the position of a recording's job in the queue (1-indexed)."""
        with self._jobs_lock:
            job_id = self._recording_jobs.get(recording_id)
            if not job_id:
                return None

            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.QUEUED:
                return None

            # Count queued jobs created before this one
            position = 1
            for j in self._jobs.values():
                if j.status == JobStatus.QUEUED and j.created_at < job.created_at:
                    position += 1

            return position

    def cleanup_old_jobs(self, max_age_hours: int = 24):
        """Remove completed/failed jobs older than max_age_hours."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)

        with self._jobs_lock:
            to_remove = [
                job_id for job_id, job in self._jobs.items()
                if job.status in [JobStatus.COMPLETED, JobStatus.FAILED]
                and job.completed_at and job.completed_at < cutoff
            ]

            for job_id in to_remove:
                del self._jobs[job_id]

            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} old jobs")


# Global job queue instance
job_queue = JobQueue(max_workers=2)
