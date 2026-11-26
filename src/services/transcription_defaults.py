"""Single source of truth for the transcription job parameters that every
ingestion path enqueues.

Historically each path (upload, reprocess, merge, recording-session stitch,
share-target, auto-process) built its own ``job_params`` dict. Several only
passed a thin subset (e.g. account-level defaults), so recordings created by
those paths silently skipped tag/folder defaults, speaker-count hints, and the
admin-curated transcription model. That in turn changed downstream behavior such
as diarization and automatic speaker labelling.

This module centralizes the precedence chain so all paths resolve settings
identically. Precedence, highest first:

    explicit per-request override
      > first tag's defaults
      > folder defaults
      > environment defaults (ASR_MIN_SPEAKERS / ASR_MAX_SPEAKERS)
      > owner (account) defaults
      > admin-curated model default

Every caller passes the ``recording`` (which carries tags/folder/owner) plus an
optional ``overrides`` dict of already-parsed per-request values, and gets back
the complete params dict used by the transcribe job.
"""

# Sentinel so callers can pass tag_id=None explicitly (meaning "no tag prompt")
# and have it respected, versus omitting the key to get the first-tag default.
_UNSET = object()


def _int_or_none(value):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def resolve_transcription_model(value):
    """The model selection a transcription job carries.

    An explicit value must be one a user may pick: a visible profile id, the
    environment id, or a legacy model name equal to the model of one of those
    (tag and folder defaults saved before the profiles existed). With no
    profile configured there is no allowlist and any value is kept, as
    before. A value outside the list is dropped with a warning, so a user
    cannot reach a hidden profile or an unlisted model by naming it (#266).
    With no value, the explicitly marked default profile is used; otherwise
    ``None`` means the environment.
    """
    candidate = (value or '').strip() or None
    if candidate:
        try:
            from src.services import asr_config
            selectable = asr_config.is_selectable(candidate)
        except Exception:  # noqa: BLE001
            selectable = True
        if not selectable:
            from flask import current_app
            current_app.logger.warning(
                f"Ignoring transcription_model={candidate!r}: not a visible transcription model")
            return None
        return candidate
    try:
        from src.services import asr_config
        return asr_config.default_profile_id()
    except Exception:  # noqa: BLE001
        return None


def resolve_transcription_params(recording=None, overrides=None, *, tags=None, folder=None, owner=None):
    """Resolve the complete transcribe job params for a recording.

    Args:
        recording: the Recording, when one already exists — its .tags / .folder /
            .owner supply the default chain. May be omitted by callers that
            resolve BEFORE the row exists (upload), which instead pass ``tags``,
            ``folder`` and ``owner`` explicitly.
        overrides: optional dict of already-parsed per-request values. Recognized
            keys: ``language``, ``min_speakers``, ``max_speakers``, ``hotwords``,
            ``initial_prompt``, ``transcription_model``, ``tag_id``. Any value
            that is absent or None/empty falls through the tag -> folder -> env
            -> owner chain. ``language`` follows the SAME rule as every other
            field: a real value wins, an empty/blank value is not "auto-detect"
            but simply unset and falls through the chain (auto-detect only
            results when nothing in the chain sets a language). ``tag_id`` is
            special: if the key is PRESENT (even as None) it is respected; if
            ABSENT, the first tag's id is used.
        tags / folder / owner: explicit context, used when ``recording`` is not
            passed. Ordered ``tags`` list; ``folder`` and ``owner`` objects.

    Returns:
        dict with keys language, min_speakers, max_speakers, tag_id, hotwords,
        initial_prompt, transcription_model — identical in shape across every
        ingestion path.
    """
    from src.config.app_config import ASR_MIN_SPEAKERS, ASR_MAX_SPEAKERS

    overrides = overrides or {}

    # Derive context from the recording when given; otherwise the caller (upload,
    # which resolves before the row exists) supplies tags/folder/owner directly.
    if recording is not None:
        if owner is None:
            owner = recording.owner
        if tags is None:
            # The owner's view of the tags, as for prompts and naming templates
            # (#412 S3): another user's personal tag on a shared recording does
            # not change how it is transcribed.
            tags = recording.get_visible_tags(owner) if owner else []
        if folder is None:
            folder = recording.folder
    tags = list(tags) if tags else []

    def _override_str(key):
        v = overrides.get(key)
        if isinstance(v, str):
            v = v.strip() or None
        return v

    min_speakers = _int_or_none(overrides.get('min_speakers'))
    max_speakers = _int_or_none(overrides.get('max_speakers'))
    # Which level each speaker bound came from (0 request, 1 tag, 2 folder,
    # 3 environment), to settle a minimum above a maximum below.
    min_level = 0 if min_speakers is not None else None
    max_level = 0 if max_speakers is not None else None
    hotwords = _override_str('hotwords')
    initial_prompt = _override_str('initial_prompt')
    transcription_model = _override_str('transcription_model')
    # language behaves like every other field: a real override wins; an empty or
    # blank value falls through the tag -> folder -> owner chain. The explicit
    # value "auto" forces auto-detection and skips the chain (#412 audit S9).
    language = _override_str('language')
    force_auto_language = isinstance(language, str) and language.strip().lower() == 'auto'
    if force_auto_language:
        language = None

    # Tag defaults — first tag (by order) that supplies each value.
    first_tag = tags[0] if tags else None
    for tag in tags:
        if not language and not force_auto_language and tag.default_language:
            language = tag.default_language
        if min_speakers is None and tag.default_min_speakers:
            min_speakers, min_level = tag.default_min_speakers, 1
        if max_speakers is None and tag.default_max_speakers:
            max_speakers, max_level = tag.default_max_speakers, 1
        if not hotwords and tag.default_hotwords:
            hotwords = tag.default_hotwords
        if not initial_prompt and tag.default_initial_prompt:
            initial_prompt = tag.default_initial_prompt
        if not transcription_model and tag.default_transcription_model:
            transcription_model = tag.default_transcription_model

    # Folder defaults.
    if folder:
        if not language and not force_auto_language and folder.default_language:
            language = folder.default_language
        if min_speakers is None and folder.default_min_speakers:
            min_speakers, min_level = folder.default_min_speakers, 2
        if max_speakers is None and folder.default_max_speakers:
            max_speakers, max_level = folder.default_max_speakers, 2
        if not hotwords and folder.default_hotwords:
            hotwords = folder.default_hotwords
        if not initial_prompt and folder.default_initial_prompt:
            initial_prompt = folder.default_initial_prompt
        if not transcription_model and folder.default_transcription_model:
            transcription_model = folder.default_transcription_model

    # Environment defaults.
    if min_speakers is None and ASR_MIN_SPEAKERS:
        min_speakers, min_level = _int_or_none(ASR_MIN_SPEAKERS), 3
    if max_speakers is None and ASR_MAX_SPEAKERS:
        max_speakers, max_level = _int_or_none(ASR_MAX_SPEAKERS), 3

    # A minimum above the maximum can only come from two levels (a tag minimum
    # of 6 with ASR_MAX_SPEAKERS=5, #415). The bound from the closer level
    # wins and the other follows it; within one level the request is kept.
    if min_speakers and max_speakers and min_speakers > max_speakers:
        if (min_level or 0) <= (max_level or 0):
            max_speakers = min_speakers
        else:
            min_speakers = max_speakers

    # Owner (account-level) defaults.
    if owner:
        if not language and not force_auto_language and owner.transcription_language:
            language = owner.transcription_language
        if not hotwords and owner.transcription_hotwords:
            hotwords = owner.transcription_hotwords
        if not initial_prompt and owner.transcription_initial_prompt:
            initial_prompt = owner.transcription_initial_prompt

    # Admin defaults are the last level (#412 audit S6). They used to be
    # applied only inside the transcription task, so callers that read these
    # resolved values (upload pre-fill, parity checks) missed them; the task
    # keeps its fallback for jobs queued before this change.
    if not hotwords or not initial_prompt:
        from src.models import SystemSetting
        if not hotwords:
            hotwords = (SystemSetting.get_setting('admin_default_hotwords', '') or '').strip() or None
        if not initial_prompt:
            initial_prompt = (SystemSetting.get_setting('admin_default_initial_prompt', '') or '').strip() or None

    # Admin-curated validation / default for the model.
    transcription_model = resolve_transcription_model(transcription_model)

    # tag_id: explicit override respected (including None), else first tag.
    tag_id = overrides.get('tag_id', _UNSET)
    if tag_id is _UNSET:
        tag_id = first_tag.id if first_tag else None

    return {
        'language': language,
        'min_speakers': min_speakers,
        'max_speakers': max_speakers,
        'tag_id': tag_id,
        'hotwords': hotwords,
        'initial_prompt': initial_prompt,
        'transcription_model': transcription_model,
    }
