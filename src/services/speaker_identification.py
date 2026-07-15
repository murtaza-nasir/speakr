"""
Shared speaker identification service.

Provides LLM-based speaker identification from transcript context,
used by both the web UI (recordings.py) and REST API (api_v1.py).
"""

import os
import re
import json
from flask import current_app


def identify_speakers_from_transcript(transcription_data, user_id, candidate_names=None):
    """
    Identify speakers in a transcription using an LLM.

    Args:
        transcription_data: List of transcript segments (already parsed JSON).
        user_id: Current user's ID (for token tracking).
        candidate_names: Optional saved speaker names. When provided, the LLM
            may only return these exact names or an empty string.

    Returns:
        dict mapping original speaker labels to identified names.
        Values are empty string "" for unidentified speakers.

    Raises:
        ValueError: If LLM API key is not configured.
        Exception: On LLM call failure.
    """
    from src.services.llm import call_llm_completion
    from src.utils import safe_json_loads
    from src.models import SystemSetting

    # Extract unique speakers in order of appearance
    seen_speakers = set()
    unique_speakers = []
    for segment in transcription_data:
        speaker = segment.get('speaker')
        if speaker and speaker not in seen_speakers:
            seen_speakers.add(speaker)
            unique_speakers.append(speaker)

    if not unique_speakers:
        return {}

    # Normalize all labels to SPEAKER_XX format for the LLM
    speaker_to_label = {}
    for idx, speaker in enumerate(unique_speakers):
        speaker_to_label[speaker] = f'SPEAKER_{str(idx).zfill(2)}'

    # Create temporary transcript with normalized labels
    formatted_lines = []
    for segment in transcription_data:
        original_speaker = segment.get('speaker')
        label = speaker_to_label.get(original_speaker, 'Unknown Speaker')
        sentence = segment.get('sentence', '')
        formatted_lines.append(f"[{label}]: {sentence}")
    formatted_transcription = "\n".join(formatted_lines)

    speaker_labels = list(speaker_to_label.values())

    current_app.logger.info(f"[Auto-Identify] Formatted transcript (first 500 chars): {formatted_transcription[:500]}")
    current_app.logger.info(f"[Auto-Identify] Speaker labels: {speaker_labels}")

    # Apply configurable transcript length limit
    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if transcript_limit == -1:
        transcript_text = formatted_transcription
    else:
        transcript_text = formatted_transcription[:transcript_limit]

    candidates = [str(name).strip() for name in (candidate_names or []) if str(name).strip()]
    candidate_instruction = ""
    if candidates:
        candidate_instruction = (
            "\nKnown speaker profiles: " + ", ".join(candidates) +
            "\nUse only these exact names. If the transcript does not clearly support "
            "a match, return an empty string for that speaker.\n"
        )

    prompt = f"""Analyze the following conversation transcript and identify the names of the speakers based on the context and content of their dialogue.

The speakers that need to be identified are: {', '.join(speaker_labels)}
{candidate_instruction}

Look for clues in the conversation such as:
- Names mentioned by other speakers when addressing someone
- Self-introductions or references to their own name
- Context clues about roles, relationships, or positions
- Any direct mentions of names in the dialogue

Here is the complete conversation transcript:

{transcript_text}

Based on the conversation above, identify the most likely real names for the speakers. Pay close attention to how speakers address each other and any names that are mentioned in the dialogue.

Respond with a single JSON object where keys are the speaker labels (e.g., "SPEAKER_01") and values are the identified full names. If a name cannot be determined from the conversation context, use an empty string "".

Example format:
{{
  "SPEAKER_01": "Jane Smith",
  "SPEAKER_03": "Bob Johnson",
  "SPEAKER_05": ""
}}

JSON Response:
"""

    current_app.logger.info("[Auto-Identify] Calling LLM")

    use_schema = os.environ.get('AUTO_IDENTIFY_RESPONSE_SCHEMA', '').strip() in ('1', 'true', 'yes')
    system_msg = (
        "You are an expert in analyzing conversation transcripts to identify speakers "
        "based on contextual clues in the dialogue. Analyze the conversation carefully "
        "to find names mentioned when speakers address each other or introduce themselves. "
        "Your response must be a single, valid JSON object containing only the requested "
        "speaker identifications."
    )

    response_content = None
    if use_schema:
        # Build JSON schema response format with constrained keys
        schema_properties = {label: {"type": "string"} for label in speaker_labels}
        schema_response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "speaker_identification",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": schema_properties,
                    "required": speaker_labels,
                    "additionalProperties": False
                }
            }
        }
        schema_prompt = prompt + f"\n\nIMPORTANT: Your JSON response must contain exactly these keys: {', '.join(speaker_labels)}"
        try:
            current_app.logger.info("[Auto-Identify] Trying json_schema response format")
            completion = call_llm_completion(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": schema_prompt}
                ],
                temperature=0.2,
                response_format=schema_response_format,
                user_id=user_id,
                operation_type='speaker_identification'
            )
            response_content = completion.choices[0].message.content
            current_app.logger.info(f"[Auto-Identify] LLM Raw Response (schema mode): {response_content}")
        except Exception as schema_err:
            current_app.logger.warning(f"[Auto-Identify] json_schema mode failed, falling back to json_object: {schema_err}")
            response_content = None

    if response_content is None:
        completion = call_llm_completion(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
            user_id=user_id,
            operation_type='speaker_identification'
        )
        response_content = completion.choices[0].message.content
        current_app.logger.info(f"[Auto-Identify] LLM Raw Response: {response_content}")

    identified_map = safe_json_loads(response_content, {})
    current_app.logger.info(f"[Auto-Identify] Parsed identified_map: {identified_map}")

    # --- Sanitize identified_map ---
    identified_map = _sanitize_identified_map(
        identified_map, speaker_labels, candidate_names=candidates
    )
    current_app.logger.info(f"[Auto-Identify] Sanitized identified_map: {identified_map}")

    # Map back to original speaker labels
    final_speaker_map = {}
    for original_speaker, temp_label in speaker_to_label.items():
        if temp_label in identified_map:
            final_speaker_map[original_speaker] = identified_map[temp_label]

    if candidates:
        allowed = {name.casefold(): name for name in candidates}
        final_speaker_map = {
            label: allowed.get(str(name).casefold(), "")
            for label, name in final_speaker_map.items()
        }

    current_app.logger.info(f"[Auto-Identify] Final speaker_map: {final_speaker_map}")
    return final_speaker_map


def apply_contextual_auto_labels(recording, user):
    """Apply opt-in LLM matches constrained to the user's saved speakers."""
    if not user.auto_speaker_labelling or not recording.transcription:
        return {}

    from src.models import Speaker
    from src.services.speaker_embedding_matcher import apply_speaker_names_to_transcription
    from src.services.speaker_snippets import create_speaker_snippets

    candidates = [
        speaker.name
        for speaker in Speaker.query.filter_by(user_id=user.id)
        .order_by(Speaker.name.asc())
        .all()
        if speaker.name
    ]
    if not candidates:
        current_app.logger.info(
            "[Auto-Identify] User %s has no saved speaker profiles", user.id
        )
        return {}

    try:
        transcription_data = json.loads(recording.transcription)
    except (json.JSONDecodeError, TypeError):
        current_app.logger.warning(
            "[Auto-Identify] Recording %s has unsupported transcription data",
            recording.id,
        )
        return {}
    if not isinstance(transcription_data, list):
        return {}

    try:
        speaker_map = identify_speakers_from_transcript(
            transcription_data,
            user.id,
            candidate_names=candidates,
        )
    except Exception as error:
        current_app.logger.warning(
            "[Auto-Identify] Contextual identification failed for recording %s: %s",
            recording.id,
            error,
        )
        return {}
    speaker_map = {label: name for label, name in speaker_map.items() if name}
    if not speaker_map:
        current_app.logger.info(
            "[Auto-Identify] No saved speaker matched recording %s", recording.id
        )
        return {}
    if not apply_speaker_names_to_transcription(recording, speaker_map):
        return {}

    snippet_map = {
        label: {"name": name, "isMe": False}
        for label, name in speaker_map.items()
    }
    create_speaker_snippets(recording.id, snippet_map)
    current_app.logger.info(
        "[Auto-Identify] Applied saved-speaker matches to recording %s: %s",
        recording.id,
        speaker_map,
    )
    return speaker_map


def _sanitize_identified_map(identified_map, speaker_labels, candidate_names=None):
    """
    Clean up LLM output: handle inverted maps, strip commentary,
    clear placeholders, etc.
    """
    speaker_label_re = re.compile(r'^SPEAKER_\d{2}$')

    # Detect inverted map ({name: "SPEAKER_XX"}) and flip it
    if identified_map and all(
        speaker_label_re.match(str(v)) for v in identified_map.values() if v
    ) and not any(speaker_label_re.match(str(k)) for k in identified_map.keys()):
        current_app.logger.warning("[Auto-Identify] Detected inverted map, flipping keys/values")
        identified_map = {v: k for k, v in identified_map.items() if v}

    allowed = {
        str(name).strip().casefold(): str(name).strip()
        for name in (candidate_names or [])
        if str(name).strip()
    }
    sanitized = {}
    for speaker_label, identified_name in identified_map.items():
        # Skip entries whose key isn't a valid SPEAKER_XX label
        if not speaker_label_re.match(str(speaker_label)):
            continue
        if not identified_name or not isinstance(identified_name, str):
            sanitized[speaker_label] = ""
            continue

        name = identified_name.strip()
        if allowed:
            sanitized[speaker_label] = allowed.get(name.casefold(), "")
            continue

        # Clear generic placeholders
        if name.lower() in ["unknown", "n/a", "not available", "unclear", "unidentified", ""]:
            sanitized[speaker_label] = ""
            continue

        # Clear label-to-label entries (e.g. "SPEAKER_01": "SPEAKER_02")
        if speaker_label_re.match(name):
            sanitized[speaker_label] = ""
            continue

        # Strip parenthetical content: "John (the host)" -> "John"
        name = re.sub(r'\s*\([^)]*\)', '', name).strip()

        # Take first name segment before comma, semicolon, or slash
        name = re.split(r'[,;/]', name)[0].strip()

        # Collapse whitespace
        name = re.sub(r'\s+', ' ', name)

        # Final check: if result still matches SPEAKER_XX, clear it
        if speaker_label_re.match(name) or not name:
            sanitized[speaker_label] = ""
            continue

        sanitized[speaker_label] = name

    return sanitized
