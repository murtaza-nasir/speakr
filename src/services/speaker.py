"""
Speaker identification and management services.
"""

import os
import re
from datetime import datetime
from flask import current_app
from flask_login import current_user

from src.database import db
from src.models import Speaker, SystemSetting
from src.services.llm import call_llm_completion
from src.utils import safe_json_loads

# NOTE: format_transcription_for_llm is referenced but not defined - needs to be implemented
def format_transcription_for_llm(transcription):
    """
    Format transcription for LLM processing.

    TODO: This function needs proper implementation.
    If transcription is JSON, extract and format the text.
    Otherwise return as-is.
    """
    if isinstance(transcription, str):
        try:
            import json
            data = json.loads(transcription)
            # If it's JSON diarized format, extract text
            if isinstance(data, list):
                return '\n'.join([f"[{seg.get('speaker', 'UNKNOWN')}] {seg.get('text', '')}"
                                  for seg in data if 'text' in seg])
        except:
            pass
    return str(transcription)

# Import TEXT_MODEL_API_KEY from llm service
from src.services.llm import TEXT_MODEL_API_KEY


def update_speaker_usage(speaker_names):
    """Helper function to update speaker usage statistics."""
    if not speaker_names or not current_user.is_authenticated:
        return
    
    try:
        for name in speaker_names:
            name = name.strip()
            if not name:
                continue
                
            speaker = Speaker.query.filter_by(user_id=current_user.id, name=name).first()
            if speaker:
                speaker.use_count += 1
                speaker.last_used = datetime.utcnow()
            else:
                # Create new speaker
                speaker = Speaker(
                    name=name,
                    user_id=current_user.id,
                    use_count=1,
                    created_at=datetime.utcnow(),
                    last_used=datetime.utcnow()
                )
                db.session.add(speaker)
        
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error updating speaker usage: {e}")
        db.session.rollback()



def identify_speakers_from_text(transcription):
    """
    Uses an LLM to identify speakers from a transcription.
    """
    if not TEXT_MODEL_API_KEY:
        raise ValueError("TEXT_MODEL_API_KEY not configured.")

    # The transcription passed here could be JSON, so we format it.
    formatted_transcription = format_transcription_for_llm(transcription)

    # Extract existing speaker labels (e.g., SPEAKER_00, SPEAKER_01) in order of appearance
    all_labels = re.findall(r'\[(SPEAKER_\d+)\]', formatted_transcription)
    seen = set()
    speaker_labels = [x for x in all_labels if not (x in seen or seen.add(x))]
    
    if not speaker_labels:
        return {}

    # Get configurable transcript length limit
    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if transcript_limit == -1:
        # No limit
        transcript_text = formatted_transcription
    else:
        transcript_text = formatted_transcription[:transcript_limit]

    prompt = f"""Analyze the following transcription and identify the names of the speakers. The speakers are labeled as {', '.join(speaker_labels)}. Based on the context of the conversation, determine the most likely name for each speaker label.

Transcription:
---
{transcript_text}
---

Respond with a single JSON object where keys are the speaker labels (e.g., "SPEAKER_00") and values are the identified full names. If a name cannot be determined, use the value "Unknown".

Example:
{{
  "SPEAKER_00": "John Doe",
  "SPEAKER_01": "Jane Smith",
  "SPEAKER_02": "Unknown"
}}

JSON Response:
"""

    try:
        completion = call_llm_completion(
            messages=[
                {"role": "system", "content": "You are an expert in analyzing conversation transcripts to identify speakers. Your response must be a single, valid JSON object."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        response_content = completion.choices[0].message.content
        speaker_map = safe_json_loads(response_content, {})

        # Post-process the map to replace "Unknown" with an empty string
        for speaker_label, identified_name in speaker_map.items():
            if identified_name.strip().lower() == "unknown":
                speaker_map[speaker_label] = ""

        return speaker_map
    except Exception as e:
        current_app.logger.error(f"Error calling LLM for speaker identification: {e}")
        raise


def identify_unidentified_speakers_from_text(transcription, unidentified_speakers):
    """
    Uses an LLM to identify only the unidentified speakers from a transcription.
    """
    if not TEXT_MODEL_API_KEY:
        raise ValueError("TEXT_MODEL_API_KEY not configured.")

    # The transcription passed here could be JSON, so we format it.
    formatted_transcription = format_transcription_for_llm(transcription)

    if not unidentified_speakers:
        return {}

    # Get configurable transcript length limit
    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if transcript_limit == -1:
        # No limit
        transcript_text = formatted_transcription
    else:
        transcript_text = formatted_transcription[:transcript_limit]

    prompt = f"""Analyze the following conversation transcript and identify the names of the UNIDENTIFIED speakers based on the context and content of their dialogue.

The speakers that need to be identified are: {', '.join(unidentified_speakers)}

Look for clues in the conversation such as:
- Names mentioned by other speakers when addressing someone
- Self-introductions or references to their own name
- Context clues about roles, relationships, or positions
- Any direct mentions of names in the dialogue

Here is the complete conversation transcript:

{transcript_text}

Based on the conversation above, identify the most likely real names for the unidentified speakers. Pay close attention to how speakers address each other and any names that are mentioned in the dialogue.

Respond with a single JSON object where keys are the speaker labels (e.g., "SPEAKER_01") and values are the identified full names. If a name cannot be determined from the conversation context, use an empty string "".

Example format:
{{
  "SPEAKER_01": "Jane Smith",
  "SPEAKER_03": "Bob Johnson",
  "SPEAKER_05": ""
}}

JSON Response:
"""

    try:
        current_app.logger.info(f"[Auto-Identify] Calling LLM to identify speakers: {unidentified_speakers}")
        current_app.logger.info(f"[Auto-Identify] Transcript excerpt (first 500 chars): {transcript_text[:500]}")

        completion = call_llm_completion(
            messages=[
                {"role": "system", "content": "You are an expert in analyzing conversation transcripts to identify speakers based on contextual clues in the dialogue. Analyze the conversation carefully to find names mentioned when speakers address each other or introduce themselves. Your response must be a single, valid JSON object containing only the requested speaker identifications."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        response_content = completion.choices[0].message.content
        current_app.logger.info(f"[Auto-Identify] LLM Raw Response: {response_content}")

        speaker_map = safe_json_loads(response_content, {})
        current_app.logger.info(f"[Auto-Identify] Parsed speaker_map: {speaker_map}")

        # Post-process the map to replace "Unknown" with an empty string
        for speaker_label, identified_name in speaker_map.items():
            if identified_name and identified_name.strip().lower() in ["unknown", "n/a", "not available", "unclear"]:
                speaker_map[speaker_label] = ""

        current_app.logger.info(f"[Auto-Identify] Final speaker_map after post-processing: {speaker_map}")
        return speaker_map
    except Exception as e:
        current_app.logger.error(f"Error calling LLM for speaker identification: {e}", exc_info=True)
        raise

