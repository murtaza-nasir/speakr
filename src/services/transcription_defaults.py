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

import json as _json

from flask import current_app

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
    """Validate a candidate transcription model against the admin-curated
    allowlist and fall back to the admin-saved default.

    Resolution:
      1. If a non-empty value is passed and an allowlist exists (admin-saved
         visible list or TRANSCRIPTION_MODELS_AVAILABLE), the value is accepted
         only when it is in that list; otherwise it is dropped with a warning.
         With no allowlist configured, any value is accepted.
      2. If nothing was passed, fall back to the admin-saved default model
         (system_setting key ``transcription_default_model``) when set.
    Returns the validated model id or None.
    """
    from src.config.app_config import TRANSCRIPTION_MODELS_AVAILABLE
    from src.models import SystemSetting

    candidate = (value or '').strip() or None
    visible = []
    try:
        raw = SystemSetting.get_setting('transcription_models_visible_json', None)
        if raw:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                visible = [
                    (item['value'] if isinstance(item, dict) else item)
                    for item in parsed if item
                ]
    except Exception:
        visible = []

    if candidate:
        in_db_list = bool(visible) and candidate in visible
        in_env_list = bool(TRANSCRIPTION_MODELS_AVAILABLE) and candidate in TRANSCRIPTION_MODELS_AVAILABLE
        if visible or TRANSCRIPTION_MODELS_AVAILABLE:
            if not (in_db_list or in_env_list):
                current_app.logger.warning(
                    f"Ignoring transcription_model={candidate!r} — not in admin-curated list or TRANSCRIPTION_MODELS_AVAILABLE"
                )
                candidate = None
        return candidate

    default = SystemSetting.get_setting('transcription_default_model', None)
    return (default or '').strip() or None


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
    hotwords = _override_str('hotwords')
    initial_prompt = _override_str('initial_prompt')
    transcription_model = _override_str('transcription_model')
    # language behaves like every other field: a real override wins; an empty or
    # blank value falls through the tag -> folder -> owner chain.
    language = _override_str('language')

    # Tag defaults — first tag (by order) that supplies each value.
    first_tag = tags[0] if tags else None
    for tag in tags:
        if not language and tag.default_language:
            language = tag.default_language
        if min_speakers is None and tag.default_min_speakers:
            min_speakers = tag.default_min_speakers
        if max_speakers is None and tag.default_max_speakers:
            max_speakers = tag.default_max_speakers
        if not hotwords and tag.default_hotwords:
            hotwords = tag.default_hotwords
        if not initial_prompt and tag.default_initial_prompt:
            initial_prompt = tag.default_initial_prompt
        if not transcription_model and tag.default_transcription_model:
            transcription_model = tag.default_transcription_model

    # Folder defaults.
    if folder:
        if not language and folder.default_language:
            language = folder.default_language
        if min_speakers is None and folder.default_min_speakers:
            min_speakers = folder.default_min_speakers
        if max_speakers is None and folder.default_max_speakers:
            max_speakers = folder.default_max_speakers
        if not hotwords and folder.default_hotwords:
            hotwords = folder.default_hotwords
        if not initial_prompt and folder.default_initial_prompt:
            initial_prompt = folder.default_initial_prompt
        if not transcription_model and folder.default_transcription_model:
            transcription_model = folder.default_transcription_model

    # Environment defaults.
    if min_speakers is None and ASR_MIN_SPEAKERS:
        min_speakers = _int_or_none(ASR_MIN_SPEAKERS)
    if max_speakers is None and ASR_MAX_SPEAKERS:
        max_speakers = _int_or_none(ASR_MAX_SPEAKERS)

    # Owner (account-level) defaults.
    if owner:
        if not language and owner.transcription_language:
            language = owner.transcription_language
        if not hotwords and owner.transcription_hotwords:
            hotwords = owner.transcription_hotwords
        if not initial_prompt and owner.transcription_initial_prompt:
            initial_prompt = owner.transcription_initial_prompt

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
