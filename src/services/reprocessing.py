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
    """A reprocess request that cannot be queued; status is the HTTP status.

    code is a server.apiError key (#477); message is its text in the
    interface language, looked up when read (during the request).
    """

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status

    @property
    def message(self):
        from src.utils.localization import translate
        return translate(f'server.apiError.{self.code}')


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
        raise ReprocessError('reprocess_audio_not_found', 404)
    if recording.status in IN_PROGRESS:
        raise ReprocessError('recording_already_processing', 400)

    # Settings first: a lookup that raises after the clearing below has
    # committed leaves a QUEUED recording with no job and no transcript,
    # which nothing can reprocess ("already being processed") or reset.
    params = resolve_transcription_params(recording, overrides or {})
    params['user_id'] = user.id

    recording.transcription = None
    recording.summary = None
    recording.status = 'QUEUED'
    Event.query.filter_by(recording_id=recording.id).delete()
    db.session.commit()

    try:
        return job_queue.enqueue(user_id=user.id, recording_id=recording.id,
                                 job_type='reprocess_transcription', params=params)
    except Exception as e:
        # Same trap from the other side: the results are gone, so leave the
        # recording FAILED, which the reprocess and reset routes accept.
        db.session.rollback()
        recording = db.session.get(type(recording), recording.id)
        recording.status = 'FAILED'
        from src.tasks.processing import _sanitize_error_message
        recording.error_message = _sanitize_error_message(f'Could not queue reprocessing: {e}')
        db.session.commit()
        raise ReprocessError('reprocess_queue_failed', 503) from e


def summary_prompt_params(custom_prompt=None, prompt_mode='replace', additional_instructions=None):
    """The custom_prompt and custom_prompt_append job parameters of a request.

    custom_prompt replaces the resolved prompt, or is appended to it when
    prompt_mode is 'append'. additional_instructions (#584) are added for
    this run after the prompt that runs, in the words the append mode uses,
    so a replaced prompt can carry them too. Neither given: (None, False),
    and the summary task resolves the prompt as for an automatic summary.
    """
    custom_prompt = custom_prompt.strip() if isinstance(custom_prompt, str) and custom_prompt.strip() else None
    extra = (additional_instructions.strip()
             if isinstance(additional_instructions, str) and additional_instructions.strip() else None)
    mode = (prompt_mode or 'replace').strip().lower() if isinstance(prompt_mode, str) else 'replace'
    if mode not in ('replace', 'append'):
        mode = 'replace'
    if extra:
        if not custom_prompt:
            return extra, True
        if mode == 'append':
            return f"{custom_prompt}\n\n{extra}", True
        # The same wording generate_summary_only_task uses for an appended prompt.
        return f"{custom_prompt}\n\nAdditional context for this recording:\n{extra}", False
    return custom_prompt, bool(custom_prompt) and mode == 'append'


def caller_summary_prompt(recording, user, custom_prompt, append):
    """The prompt of the person who asks for a summary of another user's recording (#584).

    The summary task resolves the owner's prompt. When the caller is not the
    owner and sends no prompt of their own, or sends one to append, their own
    prompt (the tags they can see, then their default) is resolved here and
    passed as the prompt that runs. A prompt the caller sends to replace the
    resolved one is used unchanged.
    """
    if user is None or recording.user_id is None or recording.user_id == user.id:
        return custom_prompt, append
    if custom_prompt and not append:
        return custom_prompt, append
    from src.tasks.processing import resolve_summary_instructions
    base, _source = resolve_summary_instructions(recording, user)
    if not custom_prompt:
        return base, False
    # The same wording generate_summary_only_task uses for an appended prompt.
    return f"{base}\n\nAdditional context for this recording:\n{custom_prompt}", False


def queue_summary_reprocess(recording, user, custom_prompt=None, prompt_mode='replace',
                            prompt_variables=None, job_type='reprocess_summary',
                            additional_instructions=None):
    """Clear the summary and events and queue a new summary. Returns the job id.

    custom_prompt, prompt_mode and additional_instructions are read by
    summary_prompt_params. prompt_variables, when given, replace the
    recording's template variables (sanitised as at upload).
    """
    from src.services import llm
    from src.services.job_queue import job_queue
    from src.utils.error_formatting import is_transcription_error
    from src.utils.prompt_variables import sanitize_variable_values

    if not recording.transcription or len(recording.transcription.strip()) < 10:
        raise ReprocessError('no_transcription_for_summary', 400)
    if is_transcription_error(recording.transcription):
        raise ReprocessError('summary_blocked_transcription_failed', 400)
    if recording.status in ('PROCESSING', 'SUMMARIZING'):
        raise ReprocessError('recording_already_processing', 400)
    if llm.client is None:
        raise ReprocessError('summary_llm_not_configured', 503)

    custom_prompt, append = summary_prompt_params(custom_prompt, prompt_mode, additional_instructions)
    custom_prompt, append = caller_summary_prompt(recording, user, custom_prompt, append)
    if prompt_variables is not None:
        recording.prompt_variables = sanitize_variable_values(prompt_variables)

    recording.summary = None
    Event.query.filter_by(recording_id=recording.id).delete()
    db.session.commit()

    params = {
        'custom_prompt': custom_prompt,
        'custom_prompt_append': append,
        'user_id': user.id,
    }
    return job_queue.enqueue(user_id=user.id, recording_id=recording.id, job_type=job_type, params=params)
