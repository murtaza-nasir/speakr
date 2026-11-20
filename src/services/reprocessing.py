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

    # What is cleared below, for a cancel of the job (src/services/job_cancel.py).
    from src.services.job_cancel import snapshot_for_reprocess
    restore_state = snapshot_for_reprocess(recording, 'transcription')

    recording.transcription = None
    recording.summary = None
    recording.clear_summary_prompt_used()
    recording.status = 'QUEUED'
    Event.query.filter_by(recording_id=recording.id).delete()
    from src.services import audit
    audit.record_in_tx('recording.reprocessed', recording=recording, details={'kind': 'transcription'})
    db.session.commit()

    try:
        return job_queue.enqueue(user_id=user.id, recording_id=recording.id,
                                 job_type='reprocess_transcription', params=params,
                                 restore_state=restore_state)
    except Exception as e:
        # Same trap from the other side: the results are gone, so leave the
        # recording FAILED, which the reprocess and reset routes accept.
        db.session.rollback()
        recording = db.session.get(type(recording), recording.id)
        recording.status = 'FAILED'
        from src.utils.error_formatting import sanitize_error_message
        recording.error_message = sanitize_error_message(f'Could not queue reprocessing: {e}')
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


def summary_prompt_used(recording, user, custom_prompt=None, prompt_mode='replace'):
    """The prompt to store with the summary a request produces, or None.

    The editable form: the prompt of the request without the instructions
    added for one run (additional_instructions, or a prompt sent to append).
    Returns {'prompt', 'source', 'name'}. A replacing prompt is 'custom',
    unless it is the caller's resolved prompt unchanged, which keeps its
    source. Another user's request without a replacing prompt runs that
    user's resolved prompt (caller_summary_prompt), so that one is stored.
    None for the owner without a replacing prompt: the summary task resolves
    the prompt when it runs and stores what it resolved.
    """
    from src.services.summary_prompt import prompt_source, same_prompt
    from src.tasks.processing import resolve_summary_prompt_detail

    text = custom_prompt.strip() if isinstance(custom_prompt, str) and custom_prompt.strip() else None
    mode = (prompt_mode or 'replace').strip().lower() if isinstance(prompt_mode, str) else 'replace'
    replacing = bool(text) and mode != 'append'
    is_owner = user is None or recording.user_id is None or recording.user_id == user.id
    if not replacing and is_owner:
        return None
    base, source, supplier = resolve_summary_prompt_detail(recording, user)
    if replacing and not same_prompt(text, base):
        return {'prompt': text, 'source': 'custom', 'name': None}
    kind, name, _key = prompt_source(source, supplier)
    return {'prompt': text if replacing else base, 'source': kind, 'name': name}


def follow_up_summary_params(recording, user):
    """Job parameters of the summary that follows a change of the transcript
    (new speaker names, an edited transcript).

    A custom prompt that produced the current summary in a run of this user
    is run again, so that the new summary has the same form (#584). In every
    other case only the user id is passed and the summary task resolves the
    owner's prompt, as it does after an upload.
    """
    params = {'user_id': user.id}
    stored = (recording.summary_prompt_used or '').strip()
    if (stored and recording.summary_prompt_source == 'custom'
            and recording.summary_prompt_user_id == user.id
            and (recording.summary or '').strip()):
        params.update(
            custom_prompt=recording.summary_prompt_used, custom_prompt_append=False,
            prompt_used={'prompt': recording.summary_prompt_used, 'source': 'custom', 'name': None})
    return params


def queue_summary_reprocess(recording, user, custom_prompt=None, prompt_mode='replace',
                            prompt_variables=None, job_type='reprocess_summary',
                            additional_instructions=None):
    """Clear the summary and events and queue a new summary. Returns the job id.

    custom_prompt, prompt_mode and additional_instructions are read by
    summary_prompt_params. prompt_variables, when given, replace the
    recording's template variables (sanitised as at upload).
    """
    from src.services import llm_config
    from src.services.job_queue import job_queue
    from src.utils.error_formatting import is_transcription_error
    from src.utils.prompt_variables import sanitize_variable_values

    if not recording.transcription or len(recording.transcription.strip()) < 10:
        raise ReprocessError('no_transcription_for_summary', 400)
    if is_transcription_error(recording.transcription):
        raise ReprocessError('summary_blocked_transcription_failed', 400)
    if recording.status in ('PROCESSING', 'SUMMARIZING'):
        raise ReprocessError('recording_already_processing', 400)
    if not llm_config.is_configured('text'):
        raise ReprocessError('summary_llm_not_configured', 503)

    prompt_used = summary_prompt_used(recording, user, custom_prompt, prompt_mode)
    custom_prompt, append = summary_prompt_params(custom_prompt, prompt_mode, additional_instructions)
    custom_prompt, append = caller_summary_prompt(recording, user, custom_prompt, append)
    if prompt_variables is not None:
        recording.prompt_variables = sanitize_variable_values(prompt_variables)

    from src.services.job_cancel import snapshot_for_reprocess
    restore_state = snapshot_for_reprocess(recording, 'summary')

    recording.summary = None
    recording.clear_summary_prompt_used()
    Event.query.filter_by(recording_id=recording.id).delete()
    from src.services import audit
    audit.record_in_tx('recording.reprocessed', recording=recording, details={'kind': 'summary'})
    db.session.commit()

    params = {
        'custom_prompt': custom_prompt,
        'custom_prompt_append': append,
        'user_id': user.id,
        # What to store as the prompt of the new summary (summary_prompt_used).
        'prompt_used': prompt_used,
    }
    return job_queue.enqueue(user_id=user.id, recording_id=recording.id, job_type=job_type, params=params,
                             restore_state=restore_state)
