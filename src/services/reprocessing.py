"""Queue a transcription or summary again: the one way every route does it.

Before #412 the web routes, API v1 (single and batch) and the bulk action each
had their own copy: some cleared the old transcript, summary and events and
some did not, and the API skipped the status, audio and transcription checks.
Every reprocess path now calls these functions, so the checks, the clearing
and the resolved settings are the same on all of them. The routes keep their
own access checks and response shapes.
"""

from src.database import db
from src.models import Event

IN_PROGRESS = ('QUEUED', 'PROCESSING', 'SUMMARIZING')


class ReprocessError(Exception):
    """A reprocess request that cannot be queued; status is the HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def queue_transcription_reprocess(recording, user, overrides=None):
    """Clear the transcript, summary and events and queue a new transcription.

    overrides: per-request transcription settings (language, speaker counts,
    hotwords, initial prompt, model); the rest comes from tag, folder, account
    and admin defaults (resolve_transcription_params). The title is generated
    again unless the user typed it (title_source). Returns the job id.
    """
    from src.services.job_queue import job_queue
    from src.services.storage import get_storage_service
    from src.services.transcription_defaults import resolve_transcription_params

    if recording.audio_deleted_at or not recording.audio_path \
            or not get_storage_service().exists(recording.audio_path):
        raise ReprocessError('Audio file not found for reprocessing', 404)
    if recording.status in IN_PROGRESS:
        raise ReprocessError('Recording is already being processed', 400)

    recording.transcription = None
    recording.summary = None
    recording.status = 'QUEUED'
    Event.query.filter_by(recording_id=recording.id).delete()
    db.session.commit()

    params = resolve_transcription_params(recording, overrides or {})
    params['user_id'] = user.id
    return job_queue.enqueue(user_id=user.id, recording_id=recording.id,
                             job_type='reprocess_transcription', params=params)


def queue_summary_reprocess(recording, user, custom_prompt=None, prompt_mode='replace',
                            prompt_variables=None, job_type='reprocess_summary'):
    """Clear the summary and events and queue a new summary. Returns the job id.

    custom_prompt replaces the resolved prompt, or is appended to it when
    prompt_mode is 'append'. prompt_variables, when given, replace the
    recording's template variables (sanitised as at upload).
    """
    from src.services import llm
    from src.services.job_queue import job_queue
    from src.utils.error_formatting import is_transcription_error
    from src.utils.prompt_variables import sanitize_variable_values

    if not recording.transcription or len(recording.transcription.strip()) < 10:
        raise ReprocessError('No valid transcription available for summary generation', 400)
    if is_transcription_error(recording.transcription):
        raise ReprocessError('Cannot generate summary: transcription failed. Please reprocess the transcription first.', 400)
    if recording.status in ('PROCESSING', 'SUMMARIZING'):
        raise ReprocessError('Recording is already being processed', 400)
    if llm.client is None:
        raise ReprocessError('Summary service is not available (LLM client not configured)', 503)

    custom_prompt = (custom_prompt or '').strip() or None
    mode = (prompt_mode or 'replace').strip().lower()
    if mode not in ('replace', 'append'):
        mode = 'replace'
    if prompt_variables is not None:
        recording.prompt_variables = sanitize_variable_values(prompt_variables)

    recording.summary = None
    Event.query.filter_by(recording_id=recording.id).delete()
    db.session.commit()

    params = {
        'custom_prompt': custom_prompt,
        'custom_prompt_append': bool(custom_prompt) and mode == 'append',
        'user_id': user.id,
    }
    return job_queue.enqueue(user_id=user.id, recording_id=recording.id, job_type=job_type, params=params)
