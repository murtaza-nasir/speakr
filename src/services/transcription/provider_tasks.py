"""Saved provider tasks for submit-and-poll transcription services.

AssemblyAI and Tencent accept a job, return a task ID and are then polled, and
they charge for each job. The task is saved on the running ProcessingJob as
soon as the service accepts it, so a retry of that job (after a polling
timeout, an error or a restart) polls the same task instead of submitting and
paying again. A new job, such as a reprocess the user starts, has no saved task
and always submits. A saved task is used only for the same service and the
same audio bytes.
"""

import hashlib
import json

from src.database import db
from src.models import ProcessingJob

TRANSCRIPTION_JOB_TYPES = ('transcribe', 'reprocess_transcription')


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as audio:
        for block in iter(lambda: audio.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def provider_task_for(recording_id, provider, audio_path):
    """Return (saved task, save function) for the recording's running job.

    Both are None when no transcription job is running for the recording, as
    in incognito mode.
    """
    job = (ProcessingJob.query
           .filter(ProcessingJob.recording_id == recording_id,
                   ProcessingJob.status == 'processing',
                   ProcessingJob.job_type.in_(TRANSCRIPTION_JOB_TYPES))
           .order_by(ProcessingJob.id.desc())
           .first())
    if job is None:
        return None, None
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

    return saved, save
