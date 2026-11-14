"""Saved provider tasks for submit-and-poll transcription services.

AssemblyAI and Tencent accept a job, return a task ID and are then polled, and
they charge for each job. The task is saved on the running ProcessingJob as
soon as the service accepts it, so a retry of that job (after a polling
timeout, an error or a restart) polls the same task instead of submitting and
paying again. A new job, such as a reprocess the user starts, has no saved task
and always submits. A saved task is used only for the same service and the
same audio bytes.

The WhisperX ASR service's asynchronous jobs (asr_endpoint) are saved the
same way, so a restart of Speakr resumes polling the remote job instead of
running the audio through the GPU again. A remote job saved on a processing
job is cancelled when that job or its recording is deleted
(cancel_remote_tasks).
"""

import hashlib
import json
import logging
from collections import namedtuple

from src.database import db
from src.models import ProcessingJob

logger = logging.getLogger(__name__)

TRANSCRIPTION_JOB_TYPES = ('transcribe', 'reprocess_transcription')

# saved: the task saved for this service and audio, or None.
# save: stores a task (None forgets it).
# key: a stable name for this processing job and audio (see
#      TranscriptionRequest.task_key).
# report_progress: stores (done, total) on the running job.
ProviderTaskContext = namedtuple('ProviderTaskContext', 'saved save key report_progress')

_NO_CONTEXT = ProviderTaskContext(None, None, None, None)


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as audio:
        for block in iter(lambda: audio.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def provider_task_context(recording_id, provider, audio_path):
    """Return the ProviderTaskContext of the recording's running job.

    Every field is None when no transcription job is running for the
    recording, as in incognito mode.
    """
    job = (ProcessingJob.query
           .filter(ProcessingJob.recording_id == recording_id,
                   ProcessingJob.status == 'processing',
                   ProcessingJob.job_type.in_(TRANSCRIPTION_JOB_TYPES))
           .order_by(ProcessingJob.id.desc())
           .first())
    if job is None:
        return _NO_CONTEXT
    audio_sha256 = _file_sha256(audio_path)
    try:
        stored = json.loads(job.provider_task) if job.provider_task else None
    except ValueError:
        stored = None
    saved = None
    if (isinstance(stored, dict) and stored.get('provider') == provider
            and stored.get('audio_sha256') == audio_sha256):
        saved = stored.get('task')
    job_id = job.id

    def save(task):
        value = None if task is None else json.dumps(
            {'provider': provider, 'audio_sha256': audio_sha256, 'task': task})
        # Written in its own transaction, at once: the worker may be stopped
        # while the service is still working on the task.
        table = ProcessingJob.__table__
        with db.engine.begin() as connection:
            connection.execute(table.update().where(table.c.id == job_id).values(provider_task=value))

    from src.services.job_queue import job_progress_writer
    key = f"speakr-{recording_id}-{job_id}-{audio_sha256[:16]}"
    return ProviderTaskContext(saved, save, key, job_progress_writer(job_id))


def provider_task_for(recording_id, provider, audio_path):
    """Return (saved task, save function) for the recording's running job.

    Both are None when no transcription job is running for the recording, as
    in incognito mode.
    """
    context = provider_task_context(recording_id, provider, audio_path)
    return context.saved, context.save


def cancel_remote_tasks(jobs):
    """Cancel the WhisperX ASR jobs saved on these processing jobs.

    Called before processing jobs or their recording are deleted, so the
    service stops the run and drops the transcript. Best effort: a service
    that cannot be reached keeps the job until its result expires.
    """
    for job in jobs:
        try:
            stored = json.loads(job.provider_task) if job.provider_task else None
        except (TypeError, ValueError):
            continue
        if not isinstance(stored, dict) or stored.get('provider') != 'asr_endpoint':
            continue
        task = stored.get('task')
        if not isinstance(task, dict) or not task.get('job_id') or not task.get('base_url'):
            continue
        try:
            from src.services.transcription.connectors.asr_endpoint import cancel_remote_job
            cancel_remote_job(task['base_url'], task['job_id'])
        except Exception as e:
            logger.warning(f"Could not cancel ASR job {task.get('job_id')} of processing job {job.id}: {e}")
