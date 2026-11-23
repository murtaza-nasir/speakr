"""Progress and cancel signals of the job a worker thread is running.

The job queue binds one ``JobControl`` to its worker thread for the length of
a job (``running``). Code deep inside the job reaches it with ``current()``
and needs no extra argument: the transcription step, the chunk loop, the
WhisperX poll and the summary step all report and check through it. Outside
a job (a web request, incognito mode, a test that calls a task directly)
``current()`` is a control that stores nothing and never cancels, so those
paths behave as they did.

Every read and write here uses a short connection of its own. The job's
session is left alone: no transaction is opened on it while an outside call
runs (PostgreSQL's idle_in_transaction_session_timeout, #405).

Progress is display only. A real number is stored only where one exists
(chunks finished of a total, or a fraction a service reports as measured);
there is no estimate anywhere.
"""

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger(__name__)

# Seconds between two progress writes of one job. A value that arrives
# earlier is kept and written with the next report after the interval.
PROGRESS_WRITE_INTERVAL = 3.0

# Units of a stored progress value (ProcessingJob.progress_kind).
KIND_CHUNKS = 'chunks'
KIND_FRACTION = 'fraction'

# Replaced in tests.
_now = time.monotonic


class JobCanceled(BaseException):
    """The running job was canceled by its owner.

    A BaseException, like KeyboardInterrupt: the processing code catches
    ``Exception`` in many places to retry a chunk, try another format or
    store an error, and a cancel has to pass through all of them. The job
    queue catches it in ``_process_job``.
    """

    def __init__(self, job_id=None):
        super().__init__(f"job {job_id} canceled")
        self.job_id = job_id


class JobControl:
    """Stores the progress of one running job and reads its cancel mark."""

    def __init__(self, job_id, created_at=None):
        self.job_id = job_id
        # With the creation time, a job whose id was given to a new job
        # after a delete (SQLite, #675) neither writes onto that job nor
        # reads its cancel mark.
        self.created_at = created_at
        self._last_write = None
        self._written = None
        self._pending = None
        self._detail = None

    # --- progress ---------------------------------------------------------

    def report(self, done, total, kind=KIND_CHUNKS):
        """Store ``done`` of ``total`` in the unit ``kind``, at most once per interval.

        ``report(None, None, None)`` clears the number, for a job that has
        none to show any more.
        """
        if done is None or total is None:
            self._pending = (None, None, None, self._detail)
        else:
            self._pending = (int(done), int(total), kind, self._detail)
        self._write_if_due()

    def detail(self, stage_detail):
        """Store what the service says it is doing (throttled with the progress)."""
        if stage_detail == self._detail:
            return
        self._detail = stage_detail
        base = self._pending or self._written or (None, None, None, None)
        self._pending = base[:3] + (stage_detail,)
        self._write_if_due()

    def _write_if_due(self):
        if self._pending is None or self._pending == self._written:
            self._pending = None
            return
        now = _now()
        if self._last_write is not None and now - self._last_write < PROGRESS_WRITE_INTERVAL:
            return
        done, total, kind, stage_detail = self._pending
        if self._update(progress_done=done, progress_total=total, progress_kind=kind,
                        stage_detail=stage_detail):
            self._written = self._pending
        self._pending = None
        self._last_write = now

    def stage(self, code):
        """Store the step of the pipeline the job has reached.

        Written at once: a job changes its step a handful of times. The
        number of the step before does not carry over.
        """
        self._pending = None
        self._detail = None
        self._written = (None, None, None, None)
        self._update(stage=code, progress_done=None, progress_total=None, progress_kind=None,
                     stage_detail=None)

    def _update(self, **values):
        if self.job_id is None:
            return False
        try:
            from sqlalchemy import update
            from src.database import db
            from src.models import ProcessingJob

            with db.engine.begin() as conn:
                conn.execute(
                    update(ProcessingJob)
                    .where(ProcessingJob.id == self.job_id, ProcessingJob.status == 'processing',
                           *self._same_job(ProcessingJob))
                    .values(**values)
                )
            return True
        except Exception as e:
            # Display only: a failed write never fails the job.
            logger.warning(f"Could not store progress for job {self.job_id}: {type(e).__name__}")
            return False

    def _same_job(self, model):
        return (model.created_at == self.created_at,) if self.created_at is not None else ()

    # --- cancel -----------------------------------------------------------

    def cancel_requested(self):
        """Whether the owner asked for this job to be canceled. One short read."""
        if self.job_id is None:
            return False
        try:
            from sqlalchemy import select
            from src.database import db
            from src.models import ProcessingJob

            with db.engine.connect() as conn:
                row = conn.execute(
                    select(ProcessingJob.cancel_requested_at).where(
                        ProcessingJob.id == self.job_id, *self._same_job(ProcessingJob))
                ).first()
            return bool(row and row[0] is not None)
        except Exception as e:
            logger.warning(f"Could not read the cancel mark of job {self.job_id}: {type(e).__name__}")
            return False

    def check_cancel(self):
        """Raise JobCanceled at a safe point when a cancel was requested."""
        if self.cancel_requested():
            raise JobCanceled(self.job_id)


class _NoJob(JobControl):
    """The control outside a job: nothing is stored and nothing is canceled."""

    def __init__(self):
        super().__init__(None)

    def report(self, done, total, kind=KIND_CHUNKS):
        return None

    def detail(self, stage_detail):
        return None

    def stage(self, code):
        return None

    def cancel_requested(self):
        return False


_NO_JOB = _NoJob()
_local = threading.local()


def current():
    """The control of the job this thread is running, or one that does nothing."""
    return getattr(_local, 'control', None) or _NO_JOB


@contextmanager
def running(control):
    """Bind ``control`` to this thread for the length of a job."""
    previous = getattr(_local, 'control', None)
    _local.control = control
    try:
        yield control
    finally:
        _local.control = previous


def request_cancel(job_id):
    """Mark a running job for cancel. True when the job was running.

    One UPDATE that matches only a job in status processing, so a job that
    finished a moment earlier is left as it ended. A second request keeps
    the time of the first.
    """
    from sqlalchemy import update
    from src.database import db
    from src.models import ProcessingJob

    result = db.session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.status == 'processing')
        .values(cancel_requested_at=db.func.coalesce(ProcessingJob.cancel_requested_at, datetime.utcnow()))
    )
    return result.rowcount > 0
