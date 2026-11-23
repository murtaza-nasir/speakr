"""Cancel of a waiting or running processing job, and the state it leaves.

A waiting job is removed at once. A running job is marked
(``ProcessingJob.cancel_requested_at``); its worker stops at the next safe
point (``job_control.check_cancel``), and ``finish_canceled_job`` removes the
row. The mark lives in the database because the request that cancels may be
served by a process that runs no workers.

The recording after a cancel (``settle_recording``):

- A transcript is stored: the recording is COMPLETED. This covers a cancel
  during the title of a transcription or of the summary job that follows
  it (the transcript of that run is complete and is kept; the steps after
  COMPLETED run, the LLM steps do not), and a canceled summary of a
  recording that has a transcript.
- No transcript, and the job was a reprocess: the transcript, summary,
  events and status the reprocess cleared are put back
  (``ProcessingJob.restore_state``), so the recording is as it was.
- No transcript and nothing to put back (a first transcription): the
  recording is FAILED with ``CANCELED_MESSAGE``; Reprocess starts it again.
  Nothing of the canceled run is stored.

A cancel sends no webhook and no email: it is neither a completion nor a
failure, and the person who canceled knows.
"""

import json
import logging
from datetime import datetime
from types import SimpleNamespace

from src.database import db

logger = logging.getLogger(__name__)

CANCELED_MESSAGE = "Processing was canceled."

# What a canceled first transcription shows in place of a transcript, in the
# stored-error form the recording view already reads (error_formatting).
_CANCELED_CARD = {
    't': 'Processing canceled',
    'm': 'The transcription of this recording was canceled before it finished.',
    'g': 'Use Reprocess to start the transcription again.',
    'i': 'fa-ban',
    'y': 'canceled',
    'k': True,
}

# Jobs of the same recording that have nothing to work on once its
# transcription is canceled.
_TRANSCRIPTION_SIDE = ('convert', 'transcribe', 'reprocess_transcription')
_DEPENDENT_TYPES = ('summarize', 'reprocess_summary', 'translate_transcription')

# The stored prompt of the summary is cleared and restored with it (#584).
_SUMMARY_PROMPT_FIELDS = ('summary_prompt_used', 'summary_prompt_source',
                          'summary_prompt_source_name', 'summary_prompt_user_id')
_SNAPSHOT_FIELDS = {
    'transcription': ('transcription', 'summary', 'status', 'error_message',
                      'transcription_requested_language', 'resolved_hotwords', 'resolved_initial_prompt')
                     + _SUMMARY_PROMPT_FIELDS,
    'summary': ('summary', 'status', 'error_message') + _SUMMARY_PROMPT_FIELDS,
}


class NotCancelable(Exception):
    """A job of a type that is never canceled (stitch, merge)."""


# --- the state a reprocess clears ---------------------------------------------

def snapshot_for_reprocess(recording, kind):
    """What a reprocess is about to clear, as the job's restore_state.

    kind: 'transcription' (transcript, summary, events, status and the
    resolved hints) or 'summary' (summary, events, status). Call before the
    clearing.
    """
    from src.models import Event

    state = {'v': 1, 'kind': kind}
    for field in _SNAPSHOT_FIELDS[kind]:
        state[field] = getattr(recording, field, None)
    events = []
    for event in Event.query.filter_by(recording_id=recording.id).all():
        row = {}
        for column in Event.__table__.columns:
            if column.name in ('id', 'recording_id'):
                continue
            value = getattr(event, column.name)
            row[column.name] = value.isoformat() if isinstance(value, datetime) else value
        events.append(row)
    state['events'] = events
    return state


def _load_snapshot(raw):
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get('kind') in _SNAPSHOT_FIELDS else None


def _restore_snapshot(recording, state):
    from src.models import Event

    for field in _SNAPSHOT_FIELDS[state['kind']]:
        if field == 'status':
            continue
        setattr(recording, field, state.get(field))
    status = state.get('status')
    if status not in ('COMPLETED', 'FAILED'):
        status = 'COMPLETED' if _has_transcript(recording) else 'FAILED'
    recording.status = status

    Event.query.filter_by(recording_id=recording.id).delete()
    for row in state.get('events') or []:
        values = {}
        for column in Event.__table__.columns:
            if column.name in ('id', 'recording_id') or column.name not in row:
                continue
            value = row[column.name]
            if value is not None and isinstance(column.type, db.DateTime):
                value = datetime.fromisoformat(value)
            values[column.name] = value
        db.session.add(Event(recording_id=recording.id, **values))


# --- the recording after a cancel -----------------------------------------------

def _has_transcript(recording):
    from src.tasks.processing import _has_transcript_content
    from src.utils.error_formatting import is_transcription_error

    text = recording.transcription
    return bool(_has_transcript_content(text) and not is_transcription_error(text))


def settle_recording(recording, job, ran):
    """Put the recording in its state after the cancel of ``job``.

    ran: the job was running (its worker calls this); False for a job that
    was removed while it waited. Returns the outcome for the log: unchanged,
    kept, restored or canceled. The caller commits.
    """
    from src.services.job_queue import is_background

    if is_background(job.job_type):
        return 'unchanged'
    state = _load_snapshot(job.restore_state)

    if job.job_type not in _TRANSCRIPTION_SIDE and state is not None:
        # A canceled summary: the summary and events it cleared come back.
        _restore_snapshot(recording, state)
        return 'restored'

    if _has_transcript(recording):
        if recording.status != 'COMPLETED':
            if (ran and job.job_type in _TRANSCRIPTION_SIDE) or _is_auto_summary(job):
                # The transcript of this run is complete: the steps that
                # follow COMPLETED run (shares, export, Inquire chunks), the
                # LLM steps do not. The summary after a transcript is the
                # last step of that run, waiting or running (D#663).
                from src.tasks.processing import finish_processing
                finish_processing(recording.id, extract_events=False)
            else:
                recording.status = 'COMPLETED'
        return 'kept'

    if state is not None:
        _restore_snapshot(recording, state)
        return 'restored'

    recording.status = 'FAILED'
    recording.error_message = CANCELED_MESSAGE
    recording.transcription = 'ERROR_JSON:' + json.dumps(_CANCELED_CARD, ensure_ascii=False)
    return 'canceled'


def _view(job):
    """The fields of a job that outlive its row."""
    return SimpleNamespace(id=job.id, job_type=job.job_type, recording_id=job.recording_id,
                           user_id=job.user_id, restore_state=job.restore_state,
                           provider_task=job.provider_task, params=job.params)


def _is_auto_summary(job):
    from src.services.job_queue import is_auto_summary
    try:
        params = json.loads(job.params) if getattr(job, 'params', None) else {}
    except (TypeError, ValueError):
        params = {}
    return is_auto_summary(job.job_type, params)


def _cancel_dependents(parent):
    """Cancel the summary and translation jobs of a recording whose transcription was canceled."""
    from sqlalchemy import delete
    from src.models import ProcessingJob
    from src.services import job_control

    if parent.job_type not in _TRANSCRIPTION_SIDE:
        return
    others = ProcessingJob.query.filter(
        ProcessingJob.recording_id == parent.recording_id,
        ProcessingJob.id != parent.id,
        ProcessingJob.job_type.in_(_DEPENDENT_TYPES),
        ProcessingJob.status.in_(['queued', 'processing']),
    ).all()
    for other in others:
        other_id = other.id
        db.session.expunge(other)
        removed = db.session.execute(
            delete(ProcessingJob).where(ProcessingJob.id == other_id, ProcessingJob.status == 'queued')
        ).rowcount
        if not removed:
            job_control.request_cancel(other_id)
        logger.info(f"Job {other_id} canceled with job {parent.id} of recording {parent.recording_id}")


def _cancel_remote(view):
    if not view.provider_task:
        return
    try:
        from src.services.transcription.provider_tasks import cancel_remote_tasks
        cancel_remote_tasks([view])
    except Exception as e:
        logger.warning(f"Could not cancel the ASR job of processing job {view.id}: {type(e).__name__}")


# --- the request side -----------------------------------------------------------

def _audit_cancel(view, recording):
    """recording.job.canceled, in the transaction of the cancel. The kind is
    the job type, a name from the code."""
    from src.services import audit
    audit.record_in_tx('recording.job.canceled', recording_id=view.recording_id,
                       recording_owner_id=recording.user_id if recording is not None else None,
                       details={'kind': view.job_type})


def cancel_job(job_id):
    """Cancel a waiting or running job. Returns one of:

    'removed'           the job was waiting; its row is gone and the recording is settled
    'cancel_requested'  the job is running; its worker stops at the next safe point
    'finished'          the job ended (completed or failed) before the cancel
    'gone'              no such job (a second cancel of a removed job)

    Raises NotCancelable for a stitch or merge job. Commits.
    """
    from sqlalchemy import delete
    from src.models import ProcessingJob, Recording
    from src.services import job_control

    for _ in range(3):
        # Workers write the job and the recording from other sessions:
        # nothing read earlier in this one is trusted.
        db.session.expire_all()
        job = db.session.get(ProcessingJob, job_id)
        if job is None:
            return 'gone'
        if job.status in ('completed', 'failed'):
            return 'finished'
        if job.job_type in ProcessingJob.NOT_CANCELABLE:
            raise NotCancelable(job.job_type)
        view = _view(job)
        db.session.expunge(job)

        # Each statement matches one status, so a worker that claims or ends
        # the job at this moment wins or loses as a whole.
        removed = db.session.execute(
            delete(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == 'queued')
        ).rowcount
        if removed:
            recording = db.session.get(Recording, view.recording_id)
            outcome = settle_recording(recording, view, ran=False) if recording else 'unchanged'
            _cancel_dependents(view)
            _audit_cancel(view, recording)
            db.session.commit()
            # A job put back in the queue by a retry or a restart may hold
            # an ASR service job of its earlier attempt.
            _cancel_remote(view)
            logger.info(f"Job {job_id} canceled while it waited; recording {view.recording_id}: {outcome}")
            return 'removed'

        if job_control.request_cancel(job_id):
            _cancel_dependents(view)
            _audit_cancel(view, db.session.get(Recording, view.recording_id))
            db.session.commit()
            # The service stops working on the audio now, not at the
            # worker's next poll. The mark is committed first: the worker
            # checks it before each poll and before a new submit, so a job
            # that is gone on the service is never submitted again.
            _cancel_remote(view)
            logger.info(f"Cancel requested for running job {job_id} of recording {view.recording_id}")
            return 'cancel_requested'

        # Neither waiting nor running any more: look again.
        db.session.rollback()
    return 'finished'


# --- the worker side ------------------------------------------------------------

def _another_job(job, recording_id, created_at):
    """The row has the id of the canceled job but is a later job (#675)."""
    return job.recording_id != recording_id or (created_at is not None and job.created_at != created_at)


def finish_canceled_job(job_id, recording_id, loaded_created_at=None, job_created_at=None):
    """Remove a running job that stopped for a cancel and settle its recording.

    Called by the job queue in the worker thread, after the job's code has
    raised JobCanceled (or failed with the cancel mark set). No webhook and
    no email is sent.
    """
    from src.models import ProcessingJob, Recording

    try:
        db.session.rollback()
    except Exception:
        pass
    # The job's code wrote the recording from its own session.
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job_id)
    if job is None or _another_job(job, recording_id, job_created_at):
        return
    view = _view(job)
    # The WhisperX connector removes its job at the poll that sees the
    # cancel; this covers a job it could not remove and one saved by an
    # earlier attempt.
    try:
        from src.services.transcription.provider_tasks import release_remote_task
        release_remote_task(job)
    except Exception as e:
        logger.warning(f"Could not remove the ASR job of canceled job {job_id}: {type(e).__name__}")

    outcome = 'unchanged'
    recording = db.session.get(Recording, recording_id)
    replaced = (recording is not None and loaded_created_at is not None
                and recording.created_at != loaded_created_at)
    if recording is not None and not replaced:
        try:
            outcome = settle_recording(recording, view, ran=True)
        except Exception as e:
            db.session.rollback()
            logger.error(f"Could not settle recording {recording_id} after the cancel of job {job_id}: "
                         f"{type(e).__name__}")
            recording = db.session.get(Recording, recording_id)
            if recording is not None and recording.status in ('QUEUED', 'PROCESSING', 'SUMMARIZING'):
                recording.status = 'COMPLETED' if _has_transcript(recording) else 'FAILED'
                if recording.status == 'FAILED':
                    recording.error_message = CANCELED_MESSAGE
        _cancel_dependents(view)

    job = db.session.get(ProcessingJob, job_id)
    if job is not None and not _another_job(job, recording_id, job_created_at):
        db.session.delete(job)
    db.session.commit()
    logger.info(f"Job {job_id} canceled while it ran; recording {recording_id}: {outcome}")
