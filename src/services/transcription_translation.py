"""LLM-based transcription translation service.

Mirrors ``src/services/speaker_identification.py``: a pure function over the
stored transcript text plus the LLM, with a job wrapper that persists the
result on a recording without touching the original transcription.
"""

import json
import re
import time


# Each chunk has to come back from the model as one complete JSON object, so
# its size is bounded by the model's output limit. transcript_length_limit
# caps summary input instead, and its -1 ("no limit") would send every segment
# as a separate call. 6000 characters is about 1500-2500 output tokens.
TRANSLATION_CHUNK_CHARS = 6000
# Smaller models lose their place in long numbered lists: from about 30
# segments on, a sentence is skipped and every later translation moves up
# one key, so text would be stored under the wrong speaker and time.
TRANSLATION_CHUNK_SEGMENTS = 25


def _chunk_limit():
    return TRANSLATION_CHUNK_CHARS


def _report(progress, done, total):
    """Pass chunk progress to the caller's callback, if there is one.

    Called once before the first chunk and once after each chunk, so a
    chunk split by _translate_payload_split still counts as one part.
    """
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:
        pass


def _resolve_target_language(user_id, target_language):
    if target_language:
        return target_language
    if user_id is not None:
        try:
            from src.models import User
            from src.utils.language import output_language_name_for_code

            user = User.query.get(user_id)
            if user is not None:
                if getattr(user, 'output_language', None):
                    return user.output_language
                ui = getattr(user, 'ui_language', None)
                name = output_language_name_for_code(ui)
                if name:
                    return name
                if ui:
                    return ui
        except Exception:
            pass
    return target_language


def _prefix_cache_parts():
    from src.tasks.processing import (
        PREFIX_CACHE_OPTIMIZED_PROMPTS,
        _SHARED_LLM_SYSTEM_MSG,
        _shared_user_prefix,
    )

    return bool(PREFIX_CACHE_OPTIMIZED_PROMPTS), _SHARED_LLM_SYSTEM_MSG, _shared_user_prefix


def _clean_llm_response(text):
    from src.tasks.processing import clean_llm_response

    return clean_llm_response(text)


def _safe_json_loads(text, fallback=None):
    from src.utils import safe_json_loads

    return safe_json_loads(text, fallback)


def _call_llm(messages, user_id, prefix_opt):
    from src.services.llm import call_translation_completion

    flag, _system, _prefix = prefix_opt
    completion = call_translation_completion(
        messages=messages,
        temperature=0.2,
        response_format={'type': 'json_object'},
        user_id=user_id,
        operation_type='translation',
    )
    content = completion.choices[0].message.content
    return _clean_llm_response(content)


def _llm_translate_payload(payload, user_id, target_language, prefix_opt):
    """Send a numbered JSON object of sentences/text and return the parsed dict."""
    import json as _json

    source_json = _json.dumps(payload, ensure_ascii=False)
    flag, shared_system, shared_prefix = prefix_opt

    if flag and shared_system and shared_prefix:
        try:
            from src.tasks.processing import _user_content

            user_content = _user_content(
                shared_prefix(source_json)
                + f'\n\nTranslate each value in the JSON object above into {target_language}. '
                f'Return a JSON object with exactly the same keys and translated values.\n',
                source_json,
            )
        except Exception:
            user_content = (
                shared_prefix(source_json)
                + f'\n\nTranslate each value in the JSON object above into {target_language}. '
                f'Return a JSON object with exactly the same keys and translated values.\n'
            )
        messages = [
            {'role': 'system', 'content': shared_system},
            {'role': 'user', 'content': user_content},
        ]
    else:
        messages = [
            {
                'role': 'system',
                'content': (
                    'You are a translation engine. Translate the values of the JSON object '
                    'into the requested language. Return only a valid JSON object with the '
                    'same keys.'
                ),
            },
            {
                'role': 'user',
                'content': (
                    f'Translate each value in the following JSON object into {target_language}. '
                    f'Return a JSON object with exactly the same keys.\n\n{source_json}'
                ),
            },
        ]

    content = _call_llm(messages, user_id, prefix_opt)
    parsed = _safe_json_loads(content, None)
    if not isinstance(parsed, dict):
        raise ValueError('Malformed translation response: expected a JSON object')

    expected_keys = set(payload.keys())
    if len(parsed) != len(expected_keys) or not expected_keys.issubset(parsed.keys()):
        raise ValueError('Malformed translation response: missing or mismatched keys')

    return parsed


def _translate_payload_split(payload, user_id, target_language, prefix_opt):
    """Translate a payload, halving it when the model drops or shifts keys.

    After a missing key, every later translation may sit one key too early,
    so the whole response is discarded and each half is sent again.
    """
    try:
        return _llm_translate_payload(payload, user_id, target_language, prefix_opt)
    except ValueError as e:
        if 'mismatched keys' not in str(e) or len(payload) < 2:
            raise
    keys = list(payload)
    half = len(keys) // 2
    result = {}
    for part in (keys[:half], keys[half:]):
        result.update(
            _translate_payload_split({k: payload[k] for k in part}, user_id, target_language, prefix_opt)
        )
    return result


def translate_json_segments(text, user_id, target_language, progress=None):
    """Translate a JSON list of transcript segments, preserving structure."""
    data = json.loads(text)
    if not isinstance(data, list):
        return translate_plain_text(text, user_id, target_language, progress=progress)

    # Detect whether this is actually a segment list.
    is_segments = all(isinstance(item, dict) and ('sentence' in item or 'text' in item) for item in data)
    if not is_segments:
        # Could be a JSON array of strings; treat as plain text.
        return translate_plain_text(text, user_id, target_language, progress=progress)

    sentences = [item.get('sentence', item.get('text', '')) for item in data]

    limit = _chunk_limit()
    # Chunk sentences without truncating.
    chunks = []
    current = []
    current_len = 0
    for sentence in sentences:
        piece_len = len(sentence or '') + 10
        if current and (current_len + piece_len > limit or len(current) >= TRANSLATION_CHUNK_SEGMENTS):
            chunks.append(current)
            current = []
            current_len = 0
        current.append(sentence)
        current_len += piece_len
    if current:
        chunks.append(current)

    prefix_opt = _prefix_cache_parts()
    translated_all = []
    _report(progress, 0, len(chunks))
    for chunk_number, chunk in enumerate(chunks, 1):
        non_empty = [i for i, s in enumerate(chunk) if (s or '').strip()]
        payload = {str(i + 1): (chunk[i] if chunk[i] is not None else '') for i in non_empty}
        parsed = _translate_payload_split(payload, user_id, target_language, prefix_opt) if payload else {}
        for i in range(len(chunk)):
            if i in non_empty:
                value = parsed.get(str(i + 1)) if isinstance(parsed, dict) else None
                if not isinstance(value, str):
                    raise ValueError('Malformed translation response: missing translation')
                translated_all.append(value)
            else:
                translated_all.append(chunk[i] if chunk[i] is not None else '')
        _report(progress, chunk_number, len(chunks))

    new_data = []
    for idx, item in enumerate(data):
        new_item = dict(item)
        if 'sentence' in item:
            new_item['sentence'] = translated_all[idx]
        elif 'text' in item:
            new_item['text'] = translated_all[idx]
        else:
            new_item['sentence'] = translated_all[idx]
        # Preserve speaker, start_time, end_time unchanged (already present).
        new_data.append(new_item)

    return json.dumps(new_data, ensure_ascii=False)


def translate_plain_text(text, user_id, target_language, progress=None):
    """Translate plain text, splitting at paragraph boundaries."""
    limit = _chunk_limit()
    # Split while preserving separators.
    tokens = re.split(r'(\n\s*\n)', text)
    blocks = []
    i = 0
    while i < len(tokens):
        para = tokens[i]
        sep = tokens[i + 1] if (i + 1) < len(tokens) else ''
        blocks.append((para, sep))
        if (i + 1) < len(tokens):
            i += 2
        else:
            i += 1

    # Chunk blocks without truncating.
    chunks = []
    current = []
    current_len = 0
    for para, sep in blocks:
        piece = para + sep
        piece_len = len(piece)
        if current and current_len + piece_len > limit:
            chunks.append(current)
            current = []
            current_len = 0
        current.append((para, sep))
        current_len += piece_len
    if current:
        chunks.append(current)

    prefix_opt = _prefix_cache_parts()
    translated_chunks = []
    _report(progress, 0, len(chunks))
    for chunk_number, chunk in enumerate(chunks, 1):
        chunk_text = ''.join(para + sep for para, sep in chunk)
        if not chunk_text.strip():
            translated_chunks.append(chunk_text)
            _report(progress, chunk_number, len(chunks))
            continue
        # Translate each paragraph independently so the paragraph separators
        # are preserved verbatim instead of depending on the model keeping them.
        paras = [para for para, _ in chunk]
        seps = [sep for _, sep in chunk]
        # Only send non-empty paragraphs; empty ones are kept verbatim so a
        # model that omits an empty key cannot fail an otherwise valid chunk.
        non_empty = [i for i, p in enumerate(paras) if (p or '').strip()]
        payload = {str(i): paras[i] for i in non_empty}
        parsed = _translate_payload_split(payload, user_id, target_language, prefix_opt) if payload else {}
        translated_paras = []
        for i in range(len(paras)):
            if i in non_empty:
                value = parsed.get(str(i)) if isinstance(parsed, dict) else None
                if not isinstance(value, str):
                    raise ValueError('Malformed translation response: missing translation')
                translated_paras.append(value)
            else:
                translated_paras.append(paras[i] if paras[i] is not None else '')
        translated_chunk = ''.join(translated_paras[i] + seps[i] for i in range(len(paras)))
        translated_chunks.append(translated_chunk)
        _report(progress, chunk_number, len(chunks))

    return ''.join(translated_chunks)


def _language_key(value):
    """Normalise a language name or code to a comparable key, or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        from src.utils.language import normalize_language_code

        code = normalize_language_code(text)
        if code:
            return code
    except Exception:
        pass
    return text.lower()


def _same_language(source_language, target_language):
    """True when both values describe the same language.

    Only an explicitly requested source language counts. None means the
    transcription ran with auto-detect, so the source is unknown and the text
    must go to the LLM.
    """
    if not source_language or not target_language:
        return False
    return _language_key(source_language) == _language_key(target_language)


def translate_transcription_text(text, user_id=None, target_language=None, source_language=None,
                                 progress=None):
    """Pure function: translate stored transcript text, return translated text.

    When the transcription was created with an explicitly requested language
    and the target language is the same one, the text is copied verbatim: the
    LLM call would only burn tokens to return the input unchanged. Recordings
    transcribed with auto-detect always go through the LLM, because their
    source language is not known for certain.
    """
    if text is None:
        return ''
    target_language = _resolve_target_language(user_id, target_language)
    if not target_language:
        target_language = 'English'

    if _same_language(source_language, target_language):
        return text if isinstance(text, str) else ''

    stripped = text.strip() if isinstance(text, str) else ''
    if stripped.startswith('['):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            # Do not catch a malformed-model ValueError here: translating the
            # raw JSON string as plain text would persist a mangled result
            # instead of letting the job retry the chunk.
            return translate_json_segments(text, user_id, target_language, progress=progress)
    return translate_plain_text(text, user_id, target_language, progress=progress)


def translate_transcription(*args, **kwargs):
    """Job wrapper / pure function dispatcher.

    Accepts either a recording object (and persists the result) or a text
    string (and returns the translated text).
    """
    text = None
    user_id = kwargs.get('user_id', kwargs.get('userId'))
    target_language = kwargs.get('target_language', kwargs.get('targetLanguage', kwargs.get('language')))
    source_language = kwargs.get('source_language', kwargs.get('sourceLanguage'))
    recording = None
    params = kwargs.get('params')
    # Optional callable(done, total), called once per translated chunk.
    progress = kwargs.get('progress')

    if args:
        if isinstance(args[0], str):
            text = args[0]
            if len(args) > 1 and user_id is None:
                user_id = args[1]
            if len(args) > 2 and target_language is None:
                target_language = args[2]
        else:
            recording = args[0]
            if len(args) > 1 and isinstance(args[1], dict):
                params = args[1]
            elif len(args) > 1 and user_id is None:
                user_id = args[1]
            if len(args) > 2 and target_language is None:
                target_language = args[2]

    if isinstance(params, dict):
        if user_id is None:
            user_id = params.get('user_id', params.get('userId'))
        if target_language is None:
            target_language = params.get('target_language', params.get('targetLanguage', params.get('language')))
        if source_language is None:
            source_language = params.get('source_language', params.get('sourceLanguage'))

    if recording is not None:
        text = getattr(recording, 'transcription', None)
        if user_id is None:
            user_id = getattr(recording, 'user_id', None)
        if target_language is None:
            target_language = _resolve_target_language(user_id, None)
        if source_language is None:
            # NULL means auto-detect, which must still go through the LLM.
            source_language = getattr(recording, 'transcription_requested_language', None)

        if not text:
            raise ValueError('No transcription to translate')

        translation_start_time = time.time()
        copied_verbatim = _same_language(source_language, target_language or 'English')
        translated = translate_transcription_text(
            text, user_id, target_language, source_language=source_language, progress=progress)

        recording.transcription_translation = translated
        recording.transcription_translation_language = target_language
        # NULL means the LLM was skipped because source and target matched:
        # there is no translation time to report. A numeric value, including a
        # real translation rounded down to 0, means the model ran.
        if copied_verbatim:
            recording.translation_duration_seconds = None
        else:
            recording.translation_duration_seconds = max(
                0, int(round(time.time() - translation_start_time)))
        from src.database import db

        db.session.commit()
        return translated

    if text is None:
        text = kwargs.get('text', kwargs.get('transcription', kwargs.get('transcription_text')))
    if target_language is None:
        target_language = _resolve_target_language(user_id, None)

    return translate_transcription_text(
        text, user_id, target_language, source_language=source_language, progress=progress)


def _segment_list(text):
    """The transcript as a list of segment dicts, or None for plain text."""
    if not text or not isinstance(text, str) or not text.strip().startswith('['):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return data
    return None


def _segment_text(segment):
    # Segments keep their text under 'sentence'; some imports use 'text'.
    return segment.get('sentence', segment.get('text'))


def reconcile_translation(recording, previous_transcription):
    """Bring a stored translation in line with a changed transcript.

    When only speakers or timestamps changed (a speaker rename, merge or
    assignment), they are copied into the translation segment by segment. Any
    other change, including clearing the transcript, removes the translation,
    because its text no longer matches the original.
    """
    if not recording.transcription_translation:
        return
    old = _segment_list(previous_transcription)
    new = _segment_list(recording.transcription)
    translated = _segment_list(recording.transcription_translation)
    if (old is not None and new is not None and translated is not None
            and len(old) == len(new) == len(translated)
            and all(_segment_text(o) == _segment_text(n) for o, n in zip(old, new))):
        synced = []
        for segment, source in zip(translated, new):
            segment = dict(segment)
            for key in ('speaker', 'start_time', 'end_time'):
                if key in source:
                    segment[key] = source[key]
                else:
                    segment.pop(key, None)
            synced.append(segment)
        synced_text = json.dumps(synced, ensure_ascii=False)
        if synced_text != recording.transcription_translation:
            recording.transcription_translation = synced_text
        return
    recording.transcription_translation = None
    recording.transcription_translation_language = None
    recording.translation_duration_seconds = None
