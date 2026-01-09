"""
Background task functions for audio processing, transcription, and summarization.

These functions handle asynchronous processing tasks:
- Audio transcription (Whisper API and custom ASR endpoints)
- Title and summary generation
- Event extraction from transcripts
- Audio/video format conversion
"""

import os
import re
import json
import time
import mimetypes
import tempfile
import subprocess
import httpx
from datetime import datetime
from flask import current_app
from openai import OpenAI

from src.database import db
from src.models import Recording, Tag, Event, TranscriptChunk, SystemSetting, GroupMembership, RecordingTag, InternalShare, SharedRecordingState, User
from src.services.embeddings import process_recording_chunks
from src.services.llm import is_using_openai_api, call_llm_completion, format_api_error_message, TEXT_MODEL_NAME, client, http_client_no_proxy
from src.utils import extract_json_object, safe_json_loads
from src.utils.ffprobe import get_codec_info, is_video_file, is_lossless_audio, FFProbeError
from src.utils.ffmpeg_utils import convert_to_mp3, extract_audio_from_video as ffmpeg_extract_audio, compress_audio, FFmpegError, FFmpegNotFoundError
from src.utils.audio_conversion import convert_if_needed, ConversionResult
from src.utils.error_formatting import format_error_for_storage
from src.config.app_config import AUDIO_COMPRESS_UPLOADS, AUDIO_CODEC, AUDIO_BITRATE
from src.audio_chunking import AudioChunkingService, ChunkProcessingError, ChunkingNotSupportedError
from src.config.app_config import (
    ASR_DIARIZE, ASR_BASE_URL, ASR_RETURN_SPEAKER_EMBEDDINGS,
    transcription_api_key, transcription_base_url, chunking_service, ENABLE_CHUNKING,
    USE_NEW_TRANSCRIPTION_ARCHITECTURE
)
from src.file_exporter import export_recording, ENABLE_AUTO_EXPORT

# Configuration for internal sharing
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'


def apply_team_tag_auto_shares(recording_id):
    """
    Apply auto-shares for all group tags on a recording after processing completes.

    This function should be called after a recording status changes to COMPLETED.
    It creates InternalShare records for team members based on group tag settings.

    Args:
        recording_id: ID of the recording to apply auto-shares for
    """
    if not ENABLE_INTERNAL_SHARING:
        return

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return

    # Get all group tags on this recording with auto-share enabled
    group_tags = db.session.query(Tag).join(
        RecordingTag, RecordingTag.tag_id == Tag.id
    ).filter(
        RecordingTag.recording_id == recording_id,
        Tag.group_id.isnot(None),
        db.or_(Tag.auto_share_on_apply == True, Tag.share_with_group_lead == True)
    ).all()

    if not group_tags:
        return

    shares_created = 0

    for tag in group_tags:
        # Determine who to share with
        if tag.auto_share_on_apply:
            group_members = GroupMembership.query.filter_by(group_id=tag.group_id).all()
        elif tag.share_with_group_lead:
            group_members = GroupMembership.query.filter_by(group_id=tag.group_id, role='admin').all()
        else:
            continue

        for membership in group_members:
            # Skip the recording owner
            if membership.user_id == recording.user_id:
                continue

            # Check if already shared
            existing_share = InternalShare.query.filter_by(
                recording_id=recording_id,
                shared_with_user_id=membership.user_id
            ).first()

            if not existing_share:
                # Create internal share with correct permissions
                # Group admins get edit permission, regular members get read-only
                share = InternalShare(
                    recording_id=recording_id,
                    owner_id=recording.user_id,
                    shared_with_user_id=membership.user_id,
                    can_edit=(membership.role == 'admin'),
                    can_reshare=False,
                    source_type='group_tag',
                    source_tag_id=tag.id
                )
                db.session.add(share)

                # Create SharedRecordingState with default values for the recipient
                state = SharedRecordingState(
                    recording_id=recording_id,
                    user_id=membership.user_id,
                    is_inbox=True,  # New shares appear in inbox by default
                    is_highlighted=False  # Not favorited by default
                )
                db.session.add(state)

                shares_created += 1
                current_app.logger.info(f"Auto-shared recording {recording_id} with user {membership.user_id} (role={membership.role}) via group tag '{tag.name}'")

    if shares_created > 0:
        db.session.commit()
        current_app.logger.info(f"Created {shares_created} auto-shares for recording {recording_id} after processing completed")


def format_transcription_for_llm(transcription_text):
    """
    Formats transcription for LLM. If it's our simplified JSON, convert it to plain text.
    Otherwise, return as is.
    """
    try:
        transcription_data = json.loads(transcription_text)
        if isinstance(transcription_data, list):
            # It's our simplified JSON format
            formatted_lines = []
            for segment in transcription_data:
                speaker = segment.get('speaker', 'Unknown Speaker')
                sentence = segment.get('sentence', '')
                formatted_lines.append(f"[{speaker}]: {sentence}")
            return "\n".join(formatted_lines)
    except (json.JSONDecodeError, TypeError):
        # Not a JSON, or not the format we expect, so return as is.
        pass
    return transcription_text


def clean_llm_response(text):
    """
    Clean LLM responses by removing thinking tags and excessive whitespace.
    This handles responses from reasoning models that include <think> tags.
    """
    if not text:
        return ""

    # Remove thinking tags and their content
    # Handle both <think> and <thinking> tags with various closing formats
    cleaned = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Also handle unclosed thinking tags (in case the model doesn't close them)
    cleaned = re.sub(r'<think(?:ing)?>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Remove any remaining XML-like tags that might be related to thinking
    # but preserve markdown formatting
    cleaned = re.sub(r'<(?!/?(?:code|pre|blockquote|p|br|hr|ul|ol|li|h[1-6]|em|strong|b|i|a|img)(?:\s|>|/))[^>]+>', '', cleaned)

    # Clean up excessive whitespace while preserving intentional formatting
    # Remove leading/trailing whitespace from each line
    lines = cleaned.split('\n')
    cleaned_lines = []
    for line in lines:
        # Preserve lines that are part of code blocks or lists
        if line.strip() or (len(cleaned_lines) > 0 and cleaned_lines[-1].strip().startswith(('```', '-', '*', '1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.'))):
            cleaned_lines.append(line.rstrip())

    # Join lines and remove multiple consecutive blank lines
    cleaned = '\n'.join(cleaned_lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    # Final strip to remove leading/trailing whitespace
    return cleaned.strip()

# Configuration from environment
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
# Note: ASR_ENDPOINT and ASR_API_KEY were removed - they were dead code
# ASR configuration is now handled via the connector architecture
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'

# chunking_service, ENABLE_CHUNKING, transcription_api_key, and transcription_base_url
# are imported from src.config.app_config

# Note: OpenAI clients are created inside each transcription function as needed,
# not at module level (matching original pre-refactor behavior)


def generate_title_task(app_context, recording_id, will_auto_summarize=False):
    """Generates only a title for a recording based on transcription.

    Args:
        app_context: Flask app context
        recording_id: ID of the recording
        will_auto_summarize: If True, don't set status to COMPLETED (summary task will do it)
    """
    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            current_app.logger.error(f"Error: Recording {recording_id} not found for title generation.")
            return

        if client is None:
            current_app.logger.warning(f"Skipping title generation for {recording_id}: OpenRouter client not configured.")
            # Only mark as completed if auto-summarization won't happen next
            if not will_auto_summarize:
                recording.status = 'COMPLETED'
                recording.completed_at = datetime.utcnow()
                db.session.commit()
            return

        if not recording.transcription or len(recording.transcription.strip()) < 10:
            current_app.logger.warning(f"Transcription for recording {recording_id} is too short or empty. Skipping title generation.")
            # Only mark as completed if auto-summarization won't happen next
            if not will_auto_summarize:
                recording.status = 'COMPLETED'
                recording.completed_at = datetime.utcnow()
                db.session.commit()
            return

        # Get configurable transcript length limit and format transcription for LLM
        transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
        if transcript_limit == -1:
            raw_transcription = recording.transcription
        else:
            raw_transcription = recording.transcription[:transcript_limit]

        # Convert ASR JSON to clean text format
        transcript_text = format_transcription_for_llm(raw_transcription)


        # Get user language preference
        user_output_language = None
        if recording.owner:
            user_output_language = recording.owner.output_language

        language_directive = f"Please provide the title in {user_output_language}." if user_output_language else ""

        prompt_text = f"""Create a short title for this conversation:

{transcript_text}

Requirements:
- Maximum 8 words
- No phrases like "Discussion about" or "Meeting on"
- Just the main topic

{language_directive}

Title:"""

        system_message_content = "You are an AI assistant that generates concise titles for audio transcriptions. Respond only with the title."
        if user_output_language:
            system_message_content += f" Ensure your response is in {user_output_language}."

        try:
            completion = call_llm_completion(
                messages=[
                    {"role": "system", "content": system_message_content},
                    {"role": "user", "content": prompt_text}
                ],
                temperature=0.7,
                max_tokens=5000,
                user_id=recording.user_id,
                operation_type='title_generation'
            )

            raw_response = completion.choices[0].message.content
            reasoning = getattr(completion.choices[0].message, 'reasoning', None)

            # Use reasoning content if main content is empty (fallback for reasoning models)
            if not raw_response and reasoning:
                current_app.logger.info(f"Title generation for recording {recording_id}: Using reasoning field as fallback")
                # Try to extract a title from the reasoning field
                lines = reasoning.strip().split('\n')
                # Look for the last line that might be the title
                for line in reversed(lines):
                    line = line.strip()
                    if line and not line.startswith('I') and len(line.split()) <= 8:
                        raw_response = line
                        break

            title = clean_llm_response(raw_response) if raw_response else ""

            if title:
                recording.title = title
                current_app.logger.info(f"Title generated for recording {recording_id}: {title}")
            else:
                current_app.logger.warning(f"Empty title generated for recording {recording_id}")

        except Exception as e:
            current_app.logger.error(f"Error generating title for recording {recording_id}: {str(e)}")
            current_app.logger.error(f"Exception details:", exc_info=True)

        # Only set status to COMPLETED if auto-summarization won't happen next
        # If auto-summarization is enabled, the summary task will set COMPLETED
        if not will_auto_summarize:
            recording.status = 'COMPLETED'
            recording.completed_at = datetime.utcnow()
            db.session.commit()
            current_app.logger.info(f"Title generation complete, status set to COMPLETED for recording {recording_id}")

            # Process chunks for semantic search after completion (if inquire mode is enabled)
            if ENABLE_INQUIRE_MODE:
                try:
                    process_recording_chunks(recording_id)
                except Exception as e:
                    current_app.logger.error(f"Error processing chunks for completed recording {recording_id}: {e}")
        else:
            # Just commit the title without changing status
            db.session.commit()
            current_app.logger.info(f"Title generation complete, leaving status unchanged (auto-summarization will follow) for recording {recording_id}")


def generate_summary_only_task(app_context, recording_id, custom_prompt_override=None, user_id=None):
    """Generates only a summary for a recording (no title, no JSON response).

    Args:
        app_context: Flask app context
        recording_id: ID of the recording
        custom_prompt_override: Optional custom prompt that overrides all other prompts (for reprocessing)
        user_id: Optional user ID to filter tag visibility (defaults to recording owner)
    """
    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            current_app.logger.error(f"Error: Recording {recording_id} not found for summary generation.")
            return

        if client is None:
            current_app.logger.warning(f"Skipping summary generation for {recording_id}: OpenRouter client not configured.")
            recording.summary = "[Summary skipped: OpenRouter client not configured]"
            db.session.commit()
            return

        recording.status = 'SUMMARIZING'
        summarization_start_time = time.time()
        db.session.commit()

        current_app.logger.info(f"Requesting summary from OpenRouter for recording {recording_id} using model {TEXT_MODEL_NAME}...")

        if not recording.transcription or len(recording.transcription.strip()) < 10:
            current_app.logger.warning(f"Transcription for recording {recording_id} is too short or empty. Skipping summarization.")
            recording.summary = "[Summary skipped due to short transcription]"
            recording.status = 'COMPLETED'
            db.session.commit()
            return

        # Get user preferences and tag custom prompts
        user_summary_prompt = None
        user_output_language = None
        tag_custom_prompt = None

        # Determine which user's perspective to use for tag visibility
        # If user_id is provided (e.g., from reprocess), use that user
        # Otherwise default to the recording owner
        viewer_user = None
        if user_id:
            viewer_user = db.session.get(User, user_id)
            if viewer_user:
                current_app.logger.info(f"Using user {viewer_user.username} (ID: {user_id}) for tag visibility filtering")
            else:
                current_app.logger.warning(f"User ID {user_id} not found, falling back to recording owner")
                viewer_user = recording.owner
        else:
            viewer_user = recording.owner
            if viewer_user:
                current_app.logger.info(f"Using recording owner {viewer_user.username} for tag visibility filtering")

        # Collect custom prompts from tags visible to the viewer user
        tag_custom_prompts = []
        if viewer_user:
            visible_tags = recording.get_visible_tags(viewer_user)
            if visible_tags:
                current_app.logger.info(f"Found {len(visible_tags)} visible tags for user {viewer_user.username} on recording {recording_id}")
                # Tags are ordered by the order they were added to this recording
                for tag in visible_tags:
                    if tag.custom_prompt and tag.custom_prompt.strip():
                        tag_custom_prompts.append({
                            'name': tag.name,
                            'prompt': tag.custom_prompt.strip()
                        })
                        current_app.logger.info(f"Found custom prompt from tag '{tag.name}' for recording {recording_id}")
        else:
            current_app.logger.warning(f"No viewer user available for tag filtering on recording {recording_id}")

        # Create merged prompt if we have multiple tag prompts
        if tag_custom_prompts:
            if len(tag_custom_prompts) == 1:
                tag_custom_prompt = tag_custom_prompts[0]['prompt']
                current_app.logger.info(f"Using single custom prompt from tag '{tag_custom_prompts[0]['name']}' for recording {recording_id}")
            else:
                # Merge multiple prompts seamlessly as unified instructions
                merged_parts = []
                for tag_prompt in tag_custom_prompts:
                    merged_parts.append(tag_prompt['prompt'])
                tag_custom_prompt = "\n\n".join(merged_parts)
                tag_names = [tp['name'] for tp in tag_custom_prompts]
                current_app.logger.info(f"Combined custom prompts from {len(tag_custom_prompts)} tags in order added ({', '.join(tag_names)}) for recording {recording_id}")
        else:
            tag_custom_prompt = None

        if recording.owner:
            user_summary_prompt = recording.owner.summary_prompt
            user_output_language = recording.owner.output_language

        # Format transcription for LLM (convert JSON to clean text format like clipboard copy)
        formatted_transcription = format_transcription_for_llm(recording.transcription)

        # Get configurable transcript length limit
        transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
        if transcript_limit == -1:
            transcript_text = formatted_transcription
        else:
            transcript_text = formatted_transcription[:transcript_limit]

        language_directive = f"IMPORTANT: You MUST provide the summary in {user_output_language}. The entire response must be in {user_output_language}." if user_output_language else ""

        # Determine which summarization instructions to use
        # Priority order: custom_prompt_override > tag custom prompt > user summary prompt > admin default prompt > hardcoded fallback
        summarization_instructions = ""
        if custom_prompt_override:
            current_app.logger.info(f"Using custom prompt override for recording {recording_id} (length: {len(custom_prompt_override)})")
            summarization_instructions = custom_prompt_override
        elif tag_custom_prompt:
            current_app.logger.info(f"Using tag custom prompt for recording {recording_id}")
            summarization_instructions = tag_custom_prompt
        elif user_summary_prompt:
            current_app.logger.info(f"Using user custom prompt for recording {recording_id}")
            summarization_instructions = user_summary_prompt
        else:
            # Get admin default prompt from system settings
            admin_default_prompt = SystemSetting.get_setting('admin_default_summary_prompt', None)
            if admin_default_prompt:
                current_app.logger.info(f"Using admin default prompt for recording {recording_id}")
                summarization_instructions = admin_default_prompt
            else:
                # Fallback to hardcoded default if admin hasn't set one
                summarization_instructions = """Generate a comprehensive summary that includes the following sections:
- **Key Issues Discussed**: A bulleted list of the main topics
- **Key Decisions Made**: A bulleted list of any decisions reached
- **Action Items**: A bulleted list of tasks assigned, including who is responsible if mentioned"""
                current_app.logger.info(f"Using hardcoded default prompt for recording {recording_id}")

        # Build context information
        current_date = datetime.now().strftime("%B %d, %Y")
        context_parts = []
        context_parts.append(f"Current date: {current_date}")

        # Add selected tags information (only visible tags)
        if viewer_user:
            visible_tags = recording.get_visible_tags(viewer_user)
            if visible_tags:
                tag_names = [tag.name for tag in visible_tags]
                context_parts.append(f"Tags applied to this transcript by the user: {', '.join(tag_names)}")

        # Add user profile information if available
        if recording.owner:
            user_context_parts = []
            if recording.owner.name:
                user_context_parts.append(f"Name: {recording.owner.name}")
            if recording.owner.job_title:
                user_context_parts.append(f"Job title: {recording.owner.job_title}")
            if recording.owner.company:
                user_context_parts.append(f"Company: {recording.owner.company}")

            if user_context_parts:
                context_parts.append(f"Information about the user: {', '.join(user_context_parts)}")

        context_section = "Context:\n" + "\n".join(f"- {part}" for part in context_parts)

        # Build SYSTEM message: Initial instructions + Context + Language
        system_message_content = "You are an AI assistant that generates comprehensive summaries for meeting transcripts. Respond only with the summary in Markdown format. Do NOT use markdown code blocks (```markdown). Provide raw markdown content directly."
        system_message_content += f"\n\n{context_section}"
        if user_output_language:
            system_message_content += f"\n\nLanguage Requirement: You MUST generate the entire summary in {user_output_language}. This is mandatory."

        # Build USER message: Transcription + Summarization Instructions + Language Directive
        prompt_text = f"""Transcription:
\"\"\"
{transcript_text}
\"\"\"

Summarization Instructions:
{summarization_instructions}

{language_directive}"""

        # Debug logging: Log the complete prompt being sent to the LLM
        current_app.logger.info(f"Sending summarization prompt to LLM (length: {len(prompt_text)} chars). Set LOG_LEVEL=DEBUG to see full prompt details.")
        current_app.logger.debug(f"=== SUMMARIZATION DEBUG for recording {recording_id} ===")
        current_app.logger.debug(f"System message: {system_message_content}")
        current_app.logger.debug(f"User prompt (length: {len(prompt_text)} chars):\n{prompt_text}")
        current_app.logger.debug(f"=== END SUMMARIZATION DEBUG for recording {recording_id} ===")

        try:
            completion = call_llm_completion(
                messages=[
                    {"role": "system", "content": system_message_content},
                    {"role": "user", "content": prompt_text}
                ],
                temperature=0.5,
                max_tokens=int(os.environ.get("SUMMARY_MAX_TOKENS", "3000")),
                user_id=recording.user_id,
                operation_type='summarization'
            )

            raw_response = completion.choices[0].message.content
            current_app.logger.info(f"Raw LLM response for recording {recording_id}: '{raw_response}'")

            summary = clean_llm_response(raw_response) if raw_response else ""
            current_app.logger.info(f"Processed summary length for recording {recording_id}: {len(summary)} characters")

            if summary:
                recording.summary = summary
                db.session.commit()
                current_app.logger.info(f"Summary generated successfully for recording {recording_id}")

                # Extract events if enabled for this user BEFORE marking as completed
                if recording.owner and recording.owner.extract_events:
                    extract_events_from_transcript(recording_id, formatted_transcription, summary)

                # Mark as completed AFTER event extraction
                recording.status = 'COMPLETED'
                recording.completed_at = datetime.utcnow()
                # Calculate and save summarization duration
                summarization_end_time = time.time()
                recording.summarization_duration_seconds = int(summarization_end_time - summarization_start_time)
                db.session.commit()
                current_app.logger.info(f"Summarization completed for recording {recording_id} in {recording.summarization_duration_seconds}s.")

                # Apply auto-shares for group tags after processing completes
                apply_team_tag_auto_shares(recording_id)

                # Export to file if auto-export is enabled
                if ENABLE_AUTO_EXPORT:
                    export_recording(recording_id)
            else:
                current_app.logger.warning(f"Empty summary generated for recording {recording_id}")
                recording.summary = "[Summary not generated]"
                recording.status = 'COMPLETED'
                # Calculate and save summarization duration even for empty summary
                summarization_end_time = time.time()
                recording.summarization_duration_seconds = int(summarization_end_time - summarization_start_time)
                db.session.commit()

                # Apply auto-shares for group tags after processing completes
                apply_team_tag_auto_shares(recording_id)

                # Export to file if auto-export is enabled (even with empty summary, transcription may be useful)
                if ENABLE_AUTO_EXPORT:
                    export_recording(recording_id)

        except Exception as e:
            error_msg = format_api_error_message(str(e))
            current_app.logger.error(f"Error generating summary for recording {recording_id}: {str(e)}")
            recording.summary = error_msg
            recording.status = 'FAILED'
            db.session.commit()


def extract_events_from_transcript(recording_id, transcript_text, summary_text):
    """Extract calendar events from transcript using LLM.

    Args:
        recording_id: ID of the recording
        transcript_text: The formatted transcript text
        summary_text: The generated summary text
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not recording.owner or not recording.owner.extract_events:
            return  # Event extraction not enabled for this user

        current_app.logger.info(f"Extracting events for recording {recording_id}")

        # Get user language preference
        user_output_language = None
        if recording.owner:
            user_output_language = recording.owner.output_language

        # Build comprehensive context information
        current_date = datetime.now()
        context_parts = []

        # CRITICAL: Determine the reference date for relative date calculations
        reference_date = None
        reference_date_source = ""

        if recording.meeting_date:
            # Prefer meeting date if available
            reference_date = recording.meeting_date
            reference_date_source = "Meeting Date"
            context_parts.append(f"**MEETING DATE (use this for relative date calculations): {recording.meeting_date.strftime('%A, %B %d, %Y')}**")
        elif recording.created_at:
            # Fall back to upload date
            reference_date = recording.created_at.date()
            reference_date_source = "Upload Date (no meeting date available)"
            context_parts.append(f"**REFERENCE DATE (use this for relative date calculations): {recording.created_at.strftime('%A, %B %d, %Y')}**")

        context_parts.append(f"Today's actual date: {current_date.strftime('%A, %B %d, %Y')}")
        context_parts.append(f"Current time: {current_date.strftime('%I:%M %p')}")

        # Add additional recording context
        if recording.created_at:
            context_parts.append(f"Recording uploaded on: {recording.created_at.strftime('%B %d, %Y at %I:%M %p')}")
        if recording.meeting_date and reference_date_source == "Meeting Date":
            # Calculate days between meeting and today for context
            # Ensure both sides are date objects (meeting_date might be datetime or date)
            meeting_date_obj = recording.meeting_date.date() if isinstance(recording.meeting_date, datetime) else recording.meeting_date
            days_since = (current_date.date() - meeting_date_obj).days
            if days_since == 0:
                context_parts.append("This meeting happened today")
            elif days_since == 1:
                context_parts.append("This meeting happened yesterday")
            else:
                context_parts.append(f"This meeting happened {days_since} days ago")

        # Add user context for better understanding
        if recording.owner:
            user_context = []
            if recording.owner.name:
                user_context.append(f"User's name: {recording.owner.name}")
            if recording.owner.job_title:
                user_context.append(f"Job title: {recording.owner.job_title}")
            if recording.owner.company:
                user_context.append(f"Company: {recording.owner.company}")
            if user_context:
                context_parts.append("User information: " + ", ".join(user_context))

        # Add participants if available
        if recording.participants:
            context_parts.append(f"Participants in the meeting: {recording.participants}")

        context_section = "\n".join(context_parts)

        # Add language directive if user has a language preference
        language_directive = ""
        if user_output_language:
            language_directive = f"\n\nLANGUAGE REQUIREMENT:\n**CRITICAL**: You MUST generate ALL event titles and descriptions in {user_output_language}. This is mandatory. The entire event content (title, description, location) must be in {user_output_language}."

        # Prepare the prompt for event extraction
        event_prompt = f"""You are analyzing a meeting transcript to extract calendar events. Use the context below to correctly interpret relative dates and times.

IMPORTANT CONTEXT:
{context_section}{language_directive}

INSTRUCTIONS:
1. **CRITICAL**: Use the MEETING DATE shown above as your reference point for ALL relative date calculations
2. When people say "next Wednesday" or "tomorrow" or "next week", calculate from the MEETING DATE, not today's date
3. Example: If the meeting date is September 13, 2025 and someone says "next Wednesday", that means September 17, 2025
4. If no specific time is mentioned for an event, use 09:00:00 (9 AM) as the default start time
5. Pay attention to time zones if mentioned
6. Extract ONLY events that are explicitly discussed as future appointments, meetings, or deadlines
7. Do NOT create events for past occurrences or general discussions

STRICT QUALIFYING CRITERIA - Events MUST have:
- Explicit action words indicating a scheduled event (meeting, appointment, call, deadline, interview, presentation, review, etc.)
- A specific or calculable date/time
- A reasonable duration (typically under 8 hours, unless explicitly specified for a multi-day event, trip, conference)
- Clear purpose or agenda

DO NOT EXTRACT (explicit exclusions):
- Long-term plans or durations (study periods, job contracts, project timelines spanning weeks/months/years)
- General statements about future intentions without specific scheduling ("I'm going to study here for a year", "I'll be working on this project")
- Implied or inferred locations - only use locations explicitly stated in the conversation
- Vague commitments without concrete times ("we should meet sometime", "let's catch up soon")
- Personal life events not discussed as scheduled appointments
- Events where you need to guess or infer critical details

For each event found, extract:
- Title: A clear, concise title for the event
- Description: Brief description including context from the meeting
- Start date/time: The calculated actual date/time (in ISO format YYYY-MM-DDTHH:MM:SS, use 09:00:00 if no time specified)
- End date/time: When the event ends (if mentioned, in ISO format, default to 1 hour after start if not specified)
- Location: Where the event will take place (if mentioned)
- Attendees: List of people who should attend (if mentioned)
- Reminder minutes: How how long before to remind (default 1 day)

Transcript Summary:
{summary_text}

Transcript excerpt (for additional context):
{transcript_text[:8000]}

RESPONSE FORMAT:
Respond with a JSON object containing an "events" array. If no events are found, return a JSON object with an empty events array.

Example response:
{{
  "events": [
    {{
      "title": "Project Review Meeting",
      "description": "Quarterly review to discuss project progress and next steps as discussed in the meeting",
      "start_datetime": "2025-07-22T14:00:00",
      "end_datetime": "2025-07-22T15:30:00",
      "location": "Conference Room A",
      "attendees": ["John Smith", "Jane Doe", "Bob Johnson"],
      "reminder_minutes": 15
    }}
  ]
}}

NEGATIVE EXAMPLES - Do NOT extract events like these:

❌ "I'm going to study here for one year" → NOT an event (long-term plan, no specific appointment)
❌ "I'll be working on this project until March" → NOT an event (duration/timeline, not a meeting)
❌ "We should get coffee sometime" → NOT an event (vague, no specific time)
❌ "The semester starts in September" → NOT an event (general information, not a scheduled appointment)
❌ "I moved here from California" → NOT an event (past occurrence)

✅ "Let's meet next Tuesday at 2pm to review the proposal" → IS an event (specific time, action word, clear purpose)
✅ "The deadline for submissions is Friday at 5pm" → IS an event (specific deadline)
✅ "I have a doctor's appointment tomorrow at 10am" → IS an event (specific appointment)

CRITICAL RULES:
1. **BASE ALL DATE CALCULATIONS ON THE MEETING DATE PROVIDED IN THE CONTEXT ABOVE**
2. Only extract events that are FUTURE relative to the MEETING DATE (not today's date)
3. Convert all relative dates using the MEETING DATE as the reference point
4. Example: If the meeting date is September 13, 2025 (Friday) and someone says:
   - "next Wednesday" = September 17, 2025
   - "tomorrow" = September 14, 2025
   - "next week" = week of September 15-19, 2025
5. IMPORTANT: If no time is mentioned, always use 09:00:00 (9 AM) as the start time, NOT midnight
6. Include context from the discussion in the description
7. Do NOT invent or assume events not explicitly discussed
8. If unsure about a date/time, do not include that event"""

        # Build system message with language requirement if applicable
        system_message_content = """You are an expert at extracting calendar events from meeting transcripts. You excel at:
1. Understanding relative date references ("next Tuesday", "tomorrow", "in two weeks") and converting them to absolute dates
2. Identifying genuine future appointments, meetings, and deadlines from conversations
3. Distinguishing between actual planned events vs. general discussions
4. Extracting participant names and meeting details accurately

You must respond with valid JSON format only."""

        if user_output_language:
            system_message_content += f"\n\nLanguage Requirement: You MUST generate ALL event titles, descriptions, and locations in {user_output_language}. This is mandatory."

        completion = call_llm_completion(
            messages=[
                {"role": "system", "content": system_message_content},
                {"role": "user", "content": event_prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
            max_tokens=3000,
            user_id=recording.user_id,
            operation_type='event_extraction'
        )

        response_content = completion.choices[0].message.content
        events_data = safe_json_loads(response_content, {})

        # Handle both {"events": [...]} and direct array format
        if isinstance(events_data, dict) and 'events' in events_data:
            events_list = events_data['events']
        elif isinstance(events_data, list):
            events_list = events_data
        else:
            events_list = []

        current_app.logger.info(f"Found {len(events_list)} events for recording {recording_id}")

        # Save events to database
        for event_data in events_list:
            try:
                # Parse dates
                start_dt = None
                end_dt = None

                if 'start_datetime' in event_data:
                    try:
                        # Try ISO format first
                        start_dt = datetime.fromisoformat(event_data['start_datetime'].replace('Z', '+00:00'))
                    except:
                        # Try other common formats
                        from dateutil import parser
                        try:
                            start_dt = parser.parse(event_data['start_datetime'])
                        except:
                            current_app.logger.warning(f"Could not parse start_datetime: {event_data['start_datetime']}")
                            continue  # Skip this event if we can't parse the date

                if 'end_datetime' in event_data and event_data['end_datetime']:
                    try:
                        end_dt = datetime.fromisoformat(event_data['end_datetime'].replace('Z', '+00:00'))
                    except:
                        from dateutil import parser
                        try:
                            end_dt = parser.parse(event_data['end_datetime'])
                        except:
                            pass  # End time is optional

                # Create event record
                event = Event(
                    recording_id=recording_id,
                    title=event_data.get('title', 'Untitled Event')[:200],
                    description=event_data.get('description', ''),
                    start_datetime=start_dt,
                    end_datetime=end_dt,
                    location=event_data.get('location', '')[:500] if event_data.get('location') else None,
                    attendees=json.dumps(event_data.get('attendees', [])) if event_data.get('attendees') else None,
                    reminder_minutes=event_data.get('reminder_minutes', 15)
                )

                db.session.add(event)
                current_app.logger.info(f"Added event '{event.title}' for recording {recording_id}")

            except Exception as e:
                current_app.logger.error(f"Error saving event for recording {recording_id}: {str(e)}")
                continue

        db.session.commit()

        # Refresh the recording to ensure events relationship is loaded
        recording = db.session.get(Recording, recording_id)
        if recording:
            db.session.refresh(recording)

    except Exception as e:
        current_app.logger.error(f"Error extracting events for recording {recording_id}: {str(e)}")
        db.session.rollback()


def extract_audio_from_video(video_filepath, output_format='mp3', cleanup_original=True):
    """Extract audio from video containers using FFmpeg.

    Behavior depends on AUDIO_COMPRESS_UPLOADS setting AND codec support:
    - If compression enabled: Re-encodes to specified format (mp3/flac/opus)
    - If compression disabled AND codec is supported: Copies stream (fast, preserves quality)
    - If compression disabled AND codec is NOT supported: Re-encodes to ensure compatibility

    Args:
        video_filepath: Path to input video file
        output_format: Audio format ('mp3', 'wav', 'flac', 'copy'), default 'mp3'
        cleanup_original: If True, deletes original video after extraction

    Returns:
        tuple: (audio_filepath, mime_type)

    Raises:
        FFmpegError: If audio extraction fails
        FFmpegNotFoundError: If FFmpeg is not installed
    """
    from src.utils.audio_conversion import get_supported_codecs

    try:
        # Check if we can copy the stream (only if codec is supported)
        can_copy_stream = False
        if not AUDIO_COMPRESS_UPLOADS:
            # Probe the video to check audio codec
            try:
                codec_info = get_codec_info(video_filepath, timeout=10)
                audio_codec = codec_info.get('audio_codec')
                supported_codecs = get_supported_codecs(needs_chunking=False)

                if audio_codec and audio_codec in supported_codecs:
                    can_copy_stream = True
                    current_app.logger.info(f"Audio codec '{audio_codec}' is supported, can copy stream")
                else:
                    current_app.logger.info(f"Audio codec '{audio_codec}' not in supported codecs {supported_codecs}, will re-encode")
            except FFProbeError as e:
                current_app.logger.warning(f"Failed to probe video codec: {e}. Will re-encode to be safe.")

        if AUDIO_COMPRESS_UPLOADS:
            # Re-encode to configured codec
            current_app.logger.info(f"Extracting and compressing audio from video: {video_filepath} (codec: {AUDIO_CODEC})")
            audio_filepath, mime_type = ffmpeg_extract_audio(
                video_filepath,
                output_format=AUDIO_CODEC,
                bitrate=AUDIO_BITRATE,
                cleanup_original=cleanup_original,
                copy_stream=False
            )
        elif can_copy_stream:
            # Copy audio stream without re-encoding (fast, preserves quality)
            current_app.logger.info(f"Extracting audio from video (stream copy, no re-encoding): {video_filepath}")
            audio_filepath, mime_type = ffmpeg_extract_audio(
                video_filepath,
                output_format='copy',
                cleanup_original=cleanup_original,
                copy_stream=True
            )
        else:
            # Codec not supported - must re-encode for compatibility
            current_app.logger.info(f"Extracting and converting audio from video: {video_filepath} (codec: {AUDIO_CODEC})")
            audio_filepath, mime_type = ffmpeg_extract_audio(
                video_filepath,
                output_format=AUDIO_CODEC,
                bitrate=AUDIO_BITRATE,
                cleanup_original=cleanup_original,
                copy_stream=False
            )

        current_app.logger.info(f"Successfully extracted audio to {audio_filepath}")
        return audio_filepath, mime_type

    except FFmpegNotFoundError as e:
        current_app.logger.error(str(e))
        raise Exception("Audio conversion tool (FFmpeg) not found on server.")
    except FFmpegError as e:
        current_app.logger.error(f"FFmpeg audio extraction failed for {video_filepath}: {str(e)}")
        raise Exception(f"Audio extraction failed: {str(e)}")
    except Exception as e:
        current_app.logger.error(f"Error extracting audio from {video_filepath}: {str(e)}")
        raise


def compress_lossless_audio(filepath, codec='mp3', bitrate='128k', codec_info=None):
    """Compress lossless audio files to save storage.

    Only compresses lossless formats - already-compressed formats are skipped
    to avoid quality degradation from re-encoding.

    Args:
        filepath: Path to the audio file
        codec: Target codec - 'mp3', 'flac', or 'opus'
        bitrate: Bitrate for lossy codecs (ignored for FLAC)
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        tuple: (new_filepath, new_mime_type) or (original_filepath, None) if skipped
    """
    # Use codec detection to check if file is lossless
    try:
        if not is_lossless_audio(filepath, timeout=10, codec_info=codec_info):
            current_app.logger.debug(f"Skipping compression for {filepath} - not a lossless format")
            return filepath, None

        # Get current codec info (use provided or fetch)
        if codec_info is None:
            codec_info_result = get_codec_info(filepath, timeout=10)
        else:
            codec_info_result = codec_info
        current_codec = codec_info_result.get('audio_codec')

        # Skip if target is same as source (e.g., FLAC to FLAC when source is already FLAC)
        if current_codec == codec:
            current_app.logger.debug(f"Skipping compression for {filepath} - already in target codec")
            return filepath, None

    except FFProbeError as e:
        current_app.logger.warning(f"Failed to probe {filepath} for compression: {e}. Skipping compression.")
        return filepath, None

    # Determine output extension and MIME type
    codec_info = {
        'mp3': {'ext': '.mp3', 'mime': 'audio/mpeg'},
        'flac': {'ext': '.flac', 'mime': 'audio/flac'},
        'opus': {'ext': '.opus', 'mime': 'audio/opus'}
    }

    if codec not in codec_info:
        current_app.logger.warning(f"Unknown codec '{codec}', defaulting to mp3")
        codec = 'mp3'

    output_ext = codec_info[codec]['ext']
    output_mime = codec_info[codec]['mime']

    base_filepath = os.path.splitext(filepath)[0]
    temp_filepath = f"{base_filepath}_compressed_temp{output_ext}"
    final_filepath = f"{base_filepath}{output_ext}"

    try:
        # Get original file size for logging
        original_size = os.path.getsize(filepath)

        current_app.logger.info(f"Compressing {filepath} to {codec.upper()}...")

        # Use centralized compression utility
        final_filepath, output_mime, _ = compress_audio(
            filepath, 
            codec=codec, 
            bitrate=bitrate,
            delete_original=True,
            codec_info=None
        )

        return final_filepath, output_mime

    except FFmpegNotFoundError as e:
        current_app.logger.error(str(e))
        raise Exception("Audio conversion tool (FFmpeg) not found on server.")
    except FFmpegError as e:
        current_app.logger.error(f"FFmpeg compression failed for {filepath}: {str(e)}")
        raise Exception(f"Audio compression failed: {str(e)}")
    except Exception as e:
        current_app.logger.error(f"Error compressing audio {filepath}: {str(e)}")
        raise


def merge_diarized_chunks(chunk_results):
    """
    Merge diarized transcription chunks while preserving speaker labels AND segments.

    Since we use known_speaker_references, speaker labels (A, B, C, D) should be
    consistent across chunks. This function:
    1. Concatenates the diarized text from each chunk
    2. Merges all segments with adjusted timestamps based on chunk start_time

    Args:
        chunk_results: List of chunk results with 'transcription', 'segments', 'start_time'

    Returns:
        Tuple of (merged_text, merged_segments, all_speakers)
    """
    from src.services.transcription import TranscriptionSegment

    if not chunk_results:
        return "", [], []

    # Sort chunks by start time to ensure correct order
    sorted_chunks = sorted(chunk_results, key=lambda x: x.get('start_time', 0))

    merged_parts = []
    merged_segments = []
    all_speakers = set()

    for chunk in sorted_chunks:
        chunk_text = chunk.get('transcription', '').strip()
        if chunk_text:
            merged_parts.append(chunk_text)

        # Merge segments with adjusted timestamps
        chunk_start_offset = chunk.get('start_time', 0)
        chunk_segments = chunk.get('segments') or []

        for seg in chunk_segments:
            # Handle both TranscriptionSegment objects and dicts
            if hasattr(seg, 'speaker'):
                speaker = seg.speaker
                text = seg.text
                start_time = seg.start_time
                end_time = seg.end_time
            else:
                speaker = seg.get('speaker', 'Unknown')
                text = seg.get('text', '')
                start_time = seg.get('start_time') or seg.get('start')
                end_time = seg.get('end_time') or seg.get('end')

            # Skip empty segments
            if not text or not text.strip():
                continue

            all_speakers.add(speaker)

            # Adjust timestamps by chunk offset
            adjusted_start = (start_time or 0) + chunk_start_offset
            adjusted_end = (end_time or 0) + chunk_start_offset

            merged_segments.append(TranscriptionSegment(
                text=text,
                speaker=speaker,
                start_time=adjusted_start,
                end_time=adjusted_end
            ))

        # Track speakers from chunk metadata too
        if chunk.get('speakers'):
            for s in chunk['speakers']:
                all_speakers.add(s)

    merged_text = '\n'.join(merged_parts)
    return merged_text, merged_segments, sorted(list(all_speakers))


def transcribe_chunks_with_connector(connector, filepath, filename, mime_type, language, diarize=False):
    """
    Transcribe a large audio file using chunking with the connector architecture.

    This is used when the connector doesn't handle chunking internally (e.g., OpenAI Whisper)
    and the file exceeds the configured chunk limit.

    For diarization-enabled connectors (gpt-4o-transcribe-diarize), this function:
    1. Processes the first chunk with diarization enabled
    2. Extracts speaker audio samples from the diarized response
    3. Passes those samples as known_speaker_references to subsequent chunks
    This maintains consistent speaker labels (A, B, C, D) across all chunks.

    Args:
        connector: The transcription connector to use
        filepath: Path to the audio file
        filename: Original filename for logging
        mime_type: MIME type of the audio file
        language: Optional language code
        diarize: Whether diarization was requested (for connectors that support it)

    Returns:
        Merged transcription text (with speaker labels if diarization enabled)
    """
    import tempfile
    from src.services.transcription import TranscriptionRequest
    from src.audio_chunking import extract_speaker_samples, samples_to_data_urls

    # Get connector specs for proper chunking (respects hard limits like max_duration_seconds)
    connector_specs = connector.specifications

    # Check if connector supports diarization (property, not method - no parentheses)
    supports_diarization = connector.supports_diarization
    use_diarization = diarize and supports_diarization

    if use_diarization:
        current_app.logger.info("Diarization enabled - will use known_speaker_references for consistent speaker labels across chunks")
    elif diarize and not supports_diarization:
        current_app.logger.warning("Diarization requested but connector doesn't support it - transcribing without diarization")

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Create chunks (passes connector_specs for duration-based chunking if needed)
            current_app.logger.info(f"Creating chunks for large file: {filepath}")
            chunks = chunking_service.create_chunks(filepath, temp_dir, connector_specs)

            if not chunks:
                raise ChunkProcessingError("No chunks were created from the audio file")

            current_app.logger.info(f"Created {len(chunks)} chunks, processing each with connector...")

            # Process each chunk
            chunk_results = []
            known_speaker_names = None
            known_speaker_refs = None  # Dict of speaker label -> data URL

            for i, chunk in enumerate(chunks):
                max_retries = 3
                retry_count = 0
                success = False

                while retry_count < max_retries and not success:
                    try:
                        retry_suffix = f" (retry {retry_count + 1}/{max_retries})" if retry_count > 0 else ""
                        current_app.logger.info(f"Processing chunk {i+1}/{len(chunks)}: {chunk['filename']} ({chunk['size_mb']:.1f}MB){retry_suffix}")

                        # Transcribe chunk using connector
                        with open(chunk['path'], 'rb') as chunk_file:
                            # For diarization: first chunk gets diarize=True, subsequent chunks
                            # get diarize=True + known_speaker_references
                            if use_diarization:
                                request = TranscriptionRequest(
                                    audio_file=chunk_file,
                                    filename=chunk['filename'],
                                    mime_type='audio/mpeg',  # Chunks are always MP3
                                    language=language,
                                    diarize=True,
                                    known_speaker_names=known_speaker_names,
                                    known_speaker_references=known_speaker_refs
                                )
                            else:
                                request = TranscriptionRequest(
                                    audio_file=chunk_file,
                                    filename=chunk['filename'],
                                    mime_type='audio/mpeg',
                                    language=language,
                                    diarize=False
                                )

                            response = connector.transcribe(request)

                        # For the first diarized chunk, extract speaker samples for subsequent chunks
                        if use_diarization and i == 0 and response.segments:
                            current_app.logger.info(f"First chunk diarized with {len(response.speakers or [])} speakers, extracting samples...")

                            # Extract speaker samples from the first chunk
                            speaker_samples = extract_speaker_samples(
                                audio_path=chunk['path'],
                                segments=[{
                                    'speaker': seg.speaker,
                                    'start_time': seg.start_time,
                                    'end_time': seg.end_time
                                } for seg in response.segments],
                                output_dir=temp_dir,
                                min_duration=2.0,
                                max_duration=10.0,
                                max_speakers=4
                            )

                            if speaker_samples:
                                # Convert to data URLs for the API
                                known_speaker_refs = samples_to_data_urls(speaker_samples)
                                known_speaker_names = list(known_speaker_refs.keys())
                                current_app.logger.info(f"Extracted speaker references for {len(known_speaker_names)} speakers: {known_speaker_names}")
                            else:
                                current_app.logger.warning("Could not extract speaker samples from first chunk")

                        # Store chunk result
                        chunk_result = {
                            'index': chunk['index'],
                            'start_time': chunk['start_time'],
                            'end_time': chunk['end_time'],
                            'duration': chunk['duration'],
                            'size_mb': chunk['size_mb'],
                            'transcription': response.text,
                            'filename': chunk['filename'],
                            'segments': response.segments if use_diarization else None,
                            'speakers': response.speakers if use_diarization else None
                        }
                        chunk_results.append(chunk_result)
                        current_app.logger.info(f"Chunk {i+1} transcribed successfully: {len(response.text)} characters")
                        success = True

                    except Exception as chunk_error:
                        retry_count += 1
                        error_msg = str(chunk_error)

                        if retry_count < max_retries:
                            wait_time = 15 if "timeout" not in error_msg.lower() else 30
                            current_app.logger.warning(f"Chunk {i+1} failed (attempt {retry_count}/{max_retries}): {chunk_error}. Retrying in {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            current_app.logger.error(f"Chunk {i+1} failed after {max_retries} attempts: {chunk_error}")
                            chunk_result = {
                                'index': chunk['index'],
                                'start_time': chunk['start_time'],
                                'end_time': chunk['end_time'],
                                'transcription': f"[Chunk {i+1} transcription failed: {str(chunk_error)}]",
                                'filename': chunk['filename']
                            }
                            chunk_results.append(chunk_result)

                # Small delay between chunks
                if i < len(chunks) - 1:
                    time.sleep(2)

            # Merge transcriptions
            current_app.logger.info(f"Merging {len(chunk_results)} chunk transcriptions...")

            if use_diarization:
                # For diarized chunks, merge text AND segments with adjusted timestamps
                merged_text, merged_segments, all_speakers = merge_diarized_chunks(chunk_results)

                if not merged_text.strip():
                    raise ChunkProcessingError("Merged transcription is empty")

                # Log statistics
                chunking_service.log_processing_statistics(chunk_results)

                current_app.logger.info(f"Merged diarization: {len(merged_segments)} segments, {len(all_speakers)} speakers: {all_speakers}")

                # Return a TranscriptionResponse so segments are preserved
                from src.services.transcription import TranscriptionResponse
                return TranscriptionResponse(
                    text=merged_text,
                    segments=merged_segments,
                    speakers=all_speakers,
                    provider=connector.PROVIDER_NAME,
                    model=getattr(connector, 'model', 'unknown')
                )
            else:
                merged_transcription = chunking_service.merge_transcriptions(chunk_results)

                if not merged_transcription.strip():
                    raise ChunkProcessingError("Merged transcription is empty")

                # Log statistics
                chunking_service.log_processing_statistics(chunk_results)

                return merged_transcription

        except Exception as e:
            current_app.logger.error(f"Chunking transcription failed for {filepath}: {e}")
            if 'chunks' in locals():
                chunking_service.cleanup_chunks(chunks)
            raise ChunkProcessingError(f"Chunked transcription failed: {str(e)}")


def transcribe_with_connector(app_context, recording_id, filepath, original_filename, start_time, mime_type=None, language=None, diarize=None, min_speakers=None, max_speakers=None, tag_id=None):
    """
    Transcribe audio using the new connector-based architecture.

    This function uses the transcription connector system which supports:
    - OpenAI Whisper (whisper-1)
    - OpenAI GPT-4o Transcribe (gpt-4o-transcribe, gpt-4o-mini-transcribe)
    - OpenAI GPT-4o Transcribe Diarize (gpt-4o-transcribe-diarize) - with speaker labels
    - Custom ASR endpoints (whisper-asr-webservice, WhisperX, etc.)

    Args:
        app_context: Flask app context
        recording_id: ID of the recording to process
        filepath: Path to the audio file
        original_filename: Original filename for logging
        start_time: Processing start time
        mime_type: MIME type of the audio file
        language: Optional language code override
        diarize: Whether to enable diarization (None = use connector default)
        min_speakers: Optional minimum speakers
        max_speakers: Optional maximum speakers
        tag_id: Optional tag ID to apply custom prompt from
    """
    from src.services.transcription import (
        get_connector, TranscriptionRequest, TranscriptionCapability
    )

    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            current_app.logger.error(f"Error: Recording {recording_id} not found for transcription.")
            return

        try:
            current_app.logger.info(f"Starting connector-based transcription for recording {recording_id}...")
            recording.status = 'PROCESSING'
            transcription_start_time = time.time()
            db.session.commit()

            # Get the active transcription connector
            connector = get_connector()
            connector_name = connector.PROVIDER_NAME
            current_app.logger.info(f"Using transcription connector: {connector_name}")

            # Handle video extraction (keep existing logic)
            actual_filepath = filepath
            actual_content_type = mime_type or mimetypes.guess_type(original_filename)[0] or 'application/octet-stream'
            actual_filename = original_filename

            # Use codec detection to check if file is a video
            try:
                is_video = is_video_file(filepath, timeout=10)
                if is_video:
                    current_app.logger.info(f"Video detected for {original_filename}")
            except FFProbeError as e:
                current_app.logger.warning(f"Failed to probe {original_filename}: {e}. Falling back to MIME type detection.")
                video_mime_types = [
                    'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm',
                    'video/avi', 'video/x-ms-wmv', 'video/3gpp'
                ]
                is_video = (
                    actual_content_type.startswith('video/') or
                    actual_content_type in video_mime_types
                )

            if is_video:
                current_app.logger.info(f"Video container detected, extracting audio...")
                try:
                    audio_filepath, audio_mime_type = extract_audio_from_video(filepath)
                    actual_filepath = audio_filepath
                    actual_content_type = audio_mime_type
                    actual_filename = os.path.basename(audio_filepath)

                    recording.audio_path = audio_filepath
                    recording.mime_type = audio_mime_type
                    db.session.commit()
                    current_app.logger.info(f"Audio extracted: {audio_filepath}")
                except Exception as e:
                    current_app.logger.error(f"Failed to extract audio from video: {str(e)}")
                    recording.status = 'FAILED'
                    recording.error_msg = f"Audio extraction failed: {str(e)}"
                    db.session.commit()
                    raise  # Re-raise so job queue marks the job as failed

            # Validate and convert audio format if needed using unified conversion utility
            # This respects:
            # - connector_specs.unsupported_codecs (e.g., opus for OpenAI)
            # - AUDIO_UNSUPPORTED_CODECS environment variable (user-specified exclusions)
            # - AUDIO_COMPRESS_UPLOADS setting (lossless compression)
            connector_specs = connector.specifications
            converted_filepath = None  # Track converted file for cleanup and retry

            try:
                # Check if chunking will be needed (affects which codecs are supported)
                needs_chunking_check = (
                    chunking_service and
                    chunking_service.needs_chunking(actual_filepath, False, connector_specs)
                )

                conversion_result = convert_if_needed(
                    filepath=actual_filepath,
                    original_filename=actual_filename,
                    needs_chunking=needs_chunking_check,
                    is_asr_endpoint=False,  # Using connector architecture
                    delete_original=False,  # Keep original, we may need it for retry
                    connector_specs=connector_specs
                )

                if conversion_result.was_converted:
                    current_app.logger.info(
                        f"Audio converted: {conversion_result.original_codec} → {conversion_result.final_codec}, "
                        f"size: {conversion_result.original_size_mb:.1f}MB → {conversion_result.final_size_mb:.1f}MB"
                    )
                    converted_filepath = conversion_result.output_path
                    actual_filepath = converted_filepath
                    actual_content_type = conversion_result.mime_type
                    actual_filename = os.path.basename(converted_filepath)
            except (FFmpegError, FFmpegNotFoundError) as conv_error:
                current_app.logger.error(f"Audio conversion failed: {conv_error}")
                raise  # Let the job fail - can't process this file
            except Exception as e:
                current_app.logger.warning(f"Could not validate/convert audio: {e}, proceeding with original file")

            # Determine if we should diarize
            if diarize is None:
                # Use connector's default diarization setting
                should_diarize = connector.supports_diarization
            else:
                should_diarize = diarize and connector.supports_diarization

            if should_diarize and not connector.supports_diarization:
                current_app.logger.warning(f"Diarization requested but connector '{connector_name}' doesn't support it")
                should_diarize = False

            # Check if chunking is needed for large files
            # The chunking service respects this priority:
            # 1. Connector handles internally (e.g., ASR endpoint) → no app-level chunking
            # 2. User's ENABLE_CHUNKING=false → no chunking
            # 3. User's CHUNK_LIMIT setting → use their settings
            # 4. Connector defaults (max_file_size, recommended_chunk_seconds)
            # 5. App default (20MB)
            current_app.logger.info(f"Chunking service available: {chunking_service is not None}")
            current_app.logger.info(f"Connector specs: max_duration={connector_specs.max_duration_seconds}s, "
                                   f"handles_internally={connector_specs.handles_chunking_internally}, "
                                   f"recommended_chunk={connector_specs.recommended_chunk_seconds}s")

            if chunking_service:
                should_chunk = chunking_service.needs_chunking(actual_filepath, False, connector_specs)
                current_app.logger.info(f"Chunking decision: should_chunk={should_chunk}")
            else:
                should_chunk = False
                current_app.logger.warning("Chunking service is disabled (ENABLE_CHUNKING=false or service not initialized)")

            # Retry loop for handling format/codec errors with MP3 conversion
            max_attempts = 2
            last_error = None

            for attempt in range(max_attempts):
                try:
                    if should_chunk:
                        # Use chunking for large files
                        file_size_mb = os.path.getsize(actual_filepath) / (1024 * 1024)
                        current_app.logger.info(f"File {actual_filepath} is large ({file_size_mb:.1f}MB), using chunking for transcription")
                        chunk_result = transcribe_chunks_with_connector(
                            connector, actual_filepath, actual_filename, actual_content_type, language,
                            diarize=should_diarize  # Pass diarization setting for speaker reference tracking
                        )

                        # Handle result based on type (TranscriptionResponse for diarized, string for plain)
                        if hasattr(chunk_result, 'segments') and chunk_result.segments and chunk_result.has_diarization():
                            # Diarized response - store with segments for click-to-seek and speaker identification
                            recording.transcription = chunk_result.to_storage_format()
                            current_app.logger.info(f"Chunked diarized transcription completed: {len(chunk_result.text)} characters, {len(chunk_result.segments)} segments")
                        else:
                            # Plain text response
                            transcription_text = chunk_result.text if hasattr(chunk_result, 'text') else chunk_result
                            recording.transcription = transcription_text
                            current_app.logger.info(f"Chunked transcription completed: {len(transcription_text)} characters")
                    else:
                        # Build the transcription request for single file
                        with open(actual_filepath, 'rb') as audio_file:
                            request = TranscriptionRequest(
                                audio_file=audio_file,
                                filename=actual_filename,
                                mime_type=actual_content_type,
                                language=language,
                                diarize=should_diarize,
                                min_speakers=min_speakers,
                                max_speakers=max_speakers
                            )

                            current_app.logger.info(f"Transcribing with connector: diarize={should_diarize}, language={language}")
                            response = connector.transcribe(request)

                        # Store the result
                        if response.segments and response.has_diarization():
                            # Store as JSON with segments (diarized format)
                            recording.transcription = response.to_storage_format()
                            current_app.logger.info(f"Transcription completed with {len(response.segments)} segments and {len(response.speakers or [])} speakers")
                        else:
                            # Store as plain text
                            recording.transcription = response.text
                            current_app.logger.info(f"Transcription completed: {len(response.text)} characters")

                        # Store speaker embeddings if available
                        if response.speaker_embeddings:
                            recording.speaker_embeddings = response.speaker_embeddings
                            current_app.logger.info(f"Stored speaker embeddings for speakers: {list(response.speaker_embeddings.keys())}")

                    # If we reach here, transcription succeeded
                    break

                except Exception as e:
                    last_error = e
                    error_msg = str(e).lower()

                    # Check if this is a format/codec error that might be fixed by MP3 conversion
                    is_format_error = any(phrase in error_msg for phrase in [
                        'corrupted', 'unsupported', 'invalid', 'format', 'codec',
                        'could not find codec', 'audio file', 'decode'
                    ])

                    # Only retry with MP3 conversion on first attempt for format errors
                    if attempt == 0 and is_format_error and not converted_filepath:
                        current_app.logger.warning(f"Transcription failed with possible format error: {e}")
                        current_app.logger.info(f"Attempting MP3 conversion and retry...")

                        # Check if file is already MP3
                        try:
                            codec_info = get_codec_info(actual_filepath, timeout=10)
                            audio_codec = codec_info.get('audio_codec', '').lower()
                            needs_conversion = audio_codec != 'mp3'
                        except FFProbeError:
                            needs_conversion = not actual_filename.lower().endswith('.mp3')

                        if needs_conversion:
                            try:
                                converted_filepath = convert_to_mp3(actual_filepath)
                                current_app.logger.info(f"Successfully converted to MP3: {converted_filepath}")
                                actual_filepath = converted_filepath
                                actual_content_type = 'audio/mpeg'
                                actual_filename = os.path.basename(converted_filepath)
                                # Recalculate if chunking is needed after conversion
                                should_chunk = (
                                    chunking_service and
                                    chunking_service.needs_chunking(actual_filepath, False, connector_specs)
                                )
                                continue  # Retry with converted file
                            except (FFmpegError, FFmpegNotFoundError) as conv_error:
                                current_app.logger.error(f"Failed to convert to MP3: {conv_error}")
                                # Fall through to raise original error
                        else:
                            current_app.logger.warning(f"File is already MP3 but still getting format error")

                    # Not a format error or already retried - propagate the error
                    raise

            # Clean up converted file if we created one and transcription succeeded
            if converted_filepath and os.path.exists(converted_filepath):
                try:
                    os.remove(converted_filepath)
                    current_app.logger.debug(f"Cleaned up converted file: {converted_filepath}")
                except OSError:
                    pass  # Best effort cleanup

            # Calculate and save transcription duration
            transcription_end_time = time.time()
            recording.transcription_duration_seconds = int(transcription_end_time - transcription_start_time)
            db.session.commit()
            current_app.logger.info(f"Transcription completed in {recording.transcription_duration_seconds}s")

            # Check if auto-summarization is disabled
            disable_auto_summarization = SystemSetting.get_setting('disable_auto_summarization', False)
            will_auto_summarize = not disable_auto_summarization

            # Generate title immediately
            generate_title_task(app_context, recording_id, will_auto_summarize=will_auto_summarize)

            if disable_auto_summarization:
                current_app.logger.info(f"Auto-summarization disabled, skipping summary for recording {recording_id}")
                recording = db.session.get(Recording, recording_id)
                if recording:
                    recording.status = 'COMPLETED'
                    recording.completed_at = datetime.utcnow()
                    db.session.commit()

                    # Apply auto-shares for group tags after processing completes
                    apply_team_tag_auto_shares(recording_id)

                    # Export transcription-only if auto-export is enabled
                    if ENABLE_AUTO_EXPORT:
                        export_recording(recording_id)
            else:
                # Auto-generate summary for all recordings
                current_app.logger.info(f"Auto-generating summary for recording {recording_id}")
                generate_summary_only_task(app_context, recording_id)

        except Exception as e:
            db.session.rollback()
            error_msg = str(e)
            error_type = type(e).__name__
            current_app.logger.error(f"Connector transcription FAILED for recording {recording_id}: [{error_type}] {error_msg}", exc_info=True)

            # Handle timeout errors specifically - log the configured timeout for debugging
            if "timed out" in error_msg.lower() or "timeout" in error_msg.lower() or "Timeout" in error_type:
                try:
                    from src.services.transcription import get_registry
                    registry = get_registry()
                    # Get timeout from connector config if available
                    connector_timeout = getattr(registry.get_active_connector(), 'timeout', None)
                    if connector_timeout:
                        current_app.logger.error(f"Timeout details - configured connector timeout: {connector_timeout}s")
                    else:
                        # Fall back to database/env setting
                        asr_timeout = SystemSetting.get_setting('asr_timeout_seconds', 1800)
                        current_app.logger.error(f"Timeout details - configured timeout: {asr_timeout}s")
                except Exception:
                    pass  # Don't fail the error handling if we can't get timeout info

            # Don't set recording.status = 'FAILED' here - let the job queue handle it
            # The job queue will decide whether to retry or permanently fail,
            # and only set FAILED status when all retries are exhausted

            # Re-raise so job queue marks the job as failed (and potentially retries)
            raise


def transcribe_audio_asr(app_context, recording_id, filepath, original_filename, start_time, mime_type=None, language=None, diarize=False, min_speakers=None, max_speakers=None, tag_id=None):
    """Transcribes audio using the ASR webservice."""
    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            current_app.logger.error(f"Error: Recording {recording_id} not found for ASR transcription.")
            return

        try:
            current_app.logger.info(f"Starting ASR transcription for recording {recording_id}...")
            recording.status = 'PROCESSING'
            transcription_start_time = time.time()
            db.session.commit()

            # Check if we need to extract audio from video container
            actual_filepath = filepath
            actual_content_type = mime_type or mimetypes.guess_type(original_filename)[0] or 'application/octet-stream'
            actual_filename = original_filename

            # Use codec detection to check if file is a video
            try:
                is_video = is_video_file(filepath, timeout=10)
                if is_video:
                    current_app.logger.info(f"Video detected via codec analysis for {original_filename}")
            except FFProbeError as e:
                current_app.logger.warning(f"Failed to probe {original_filename}: {e}. Falling back to MIME type detection.")
                # Fallback to MIME type detection
                video_mime_types = [
                    'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm',
                    'video/avi', 'video/x-ms-wmv', 'video/3gpp'
                ]
                is_video = (
                    actual_content_type.startswith('video/') or
                    actual_content_type in video_mime_types
                )

            if is_video:
                current_app.logger.info(f"Video container detected ({actual_content_type}), extracting audio...")
                try:
                    # Extract audio from video (uses compression settings)
                    audio_filepath, audio_mime_type = extract_audio_from_video(filepath)

                    # Update paths and MIME type for ASR processing
                    actual_filepath = audio_filepath
                    actual_content_type = audio_mime_type
                    actual_filename = os.path.basename(audio_filepath)

                    # Update recording with extracted audio path and new MIME type
                    recording.audio_path = audio_filepath
                    recording.mime_type = audio_mime_type
                    db.session.commit()

                    current_app.logger.info(f"Audio extracted successfully: {audio_filepath}")
                except Exception as e:
                    current_app.logger.error(f"Failed to extract audio from video: {str(e)}")
                    recording.status = 'FAILED'
                    recording.error_msg = f"Audio extraction failed: {str(e)}"
                    db.session.commit()
                    raise  # Re-raise so job queue marks the job as failed

            # Keep track of converted filepath for retry logic
            converted_filepath = None

            # Retry loop for handling 500 errors with WAV conversion
            max_attempts = 2
            for attempt in range(max_attempts):
                try:
                    # Use converted MP3 if available from previous attempt
                    current_filepath = converted_filepath if converted_filepath else actual_filepath
                    current_content_type = 'audio/mpeg' if converted_filepath else actual_content_type
                    current_filename = os.path.basename(current_filepath)

                    with open(current_filepath, 'rb') as audio_file:
                        url = f"{ASR_BASE_URL}/asr"
                        params = {
                            'encode': True,
                            'task': 'transcribe',
                            'output': 'json'
                        }
                        if language:
                            params['language'] = language
                        if diarize:
                            # Send both parameter names for compatibility:
                            # - 'diarize' is used by whisper-asr-webservice
                            # - 'enable_diarization' is used by WhisperX
                            params['diarize'] = True
                            params['enable_diarization'] = True
                            # Only request speaker embeddings if explicitly enabled (WhisperX only)
                            if ASR_RETURN_SPEAKER_EMBEDDINGS:
                                params['return_speaker_embeddings'] = True
                        if min_speakers:
                            params['min_speakers'] = min_speakers
                        if max_speakers:
                            params['max_speakers'] = max_speakers

                        content_type = current_content_type
                        current_app.logger.info(f"Using MIME type {content_type} for ASR upload.")
                        files = {'audio_file': (current_filename, audio_file, content_type)}

                        with httpx.Client() as client:
                            # Get configurable ASR timeout from database (default 30 minutes)
                            asr_timeout_seconds = SystemSetting.get_setting('asr_timeout_seconds', 1800)
                            # Use generous timeouts: write=300s for large file uploads, pool=None to wait indefinitely
                            timeout = httpx.Timeout(None, connect=60.0, read=float(asr_timeout_seconds), write=300.0, pool=None)
                            current_app.logger.info(f"Sending ASR request to {url} with params: {params} (timeout: {asr_timeout_seconds}s)")
                            response = client.post(url, params=params, files=files, timeout=timeout)
                            current_app.logger.info(f"ASR request completed with status: {response.status_code}")
                            response.raise_for_status()

                            # Parse the JSON response from ASR
                            # Try to parse as JSON first, fall back to content-type check for error handling
                            response_text = response.text
                            try:
                                asr_response_data = response.json()
                            except Exception as json_err:
                                # If JSON parsing fails, check if it looks like HTML (error page)
                                if response_text.strip().startswith('<'):
                                    current_app.logger.error(f"ASR returned HTML error page (status {response.status_code}): {response_text[:500]}")
                                    raise Exception(f"ASR service returned HTML error page (status {response.status_code})")
                                else:
                                    current_app.logger.error(f"ASR returned non-JSON response (status {response.status_code}): {response_text[:500]}")
                                    raise Exception(f"ASR service returned invalid response: {json_err}")

                            # Extract speaker embeddings if present
                            if 'speaker_embeddings' in asr_response_data:
                                current_app.logger.info(f"Received speaker embeddings for speakers: {list(asr_response_data['speaker_embeddings'].keys())}")
                                # Store speaker embeddings in the recording
                                recording.speaker_embeddings = asr_response_data['speaker_embeddings']
                                db.session.commit()

                    # If we reach here, the request was successful
                    break

                except httpx.HTTPStatusError as e:
                    # Check if it's a 500 error and we haven't tried MP3 conversion yet
                    if e.response.status_code == 500 and attempt == 0 and not converted_filepath:
                        current_app.logger.warning(f"ASR returned 500 error for recording {recording_id}, attempting high-quality MP3 conversion and retry...")

                        # Check if file is already MP3 using codec detection
                        try:
                            codec_info = get_codec_info(actual_filepath, timeout=10)
                            audio_codec = codec_info.get('audio_codec')
                            needs_conversion = audio_codec != 'mp3'
                        except FFProbeError:
                            # Fallback to extension check if probe fails
                            needs_conversion = not actual_filename.lower().endswith('.mp3')

                        if needs_conversion:
                            try:
                                current_app.logger.info(f"Converting {actual_filename} to high-quality MP3 format for retry...")
                                temp_mp3_filepath = convert_to_mp3(actual_filepath)
                                current_app.logger.info(f"Successfully converted to MP3: {temp_mp3_filepath}")

                                converted_filepath = temp_mp3_filepath
                                # Continue to next iteration to retry with MP3
                                continue
                            except (FFmpegError, FFmpegNotFoundError) as conv_error:
                                current_app.logger.error(f"Failed to convert to MP3: {conv_error}")
                                # Re-raise the original HTTP error if conversion fails
                                raise e
                        else:
                            # Already an MP3 file, can't convert further
                            current_app.logger.error(f"File is already MP3 but still getting 500 error")
                            raise e
                    else:
                        # Not a 500 error or already tried conversion, propagate the error
                        raise e

            # Optional: Preserve converted file for debugging
            if os.getenv('PRESERVE_DEBUG_CONVERSIONS', 'false').lower() == 'true':
                if converted_filepath and os.path.exists(converted_filepath):
                    try:
                        # Get file size and basic info for debugging
                        converted_size = os.path.getsize(converted_filepath)
                        converted_size_mb = converted_size / (1024 * 1024)

                        # Create a debug copy in a known location
                        debug_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'debug_converted')
                        os.makedirs(debug_dir, exist_ok=True)

                        # Copy the converted file with a timestamp
                        from shutil import copy2
                        file_ext = os.path.splitext(converted_filepath)[1] or '.mp3'
                        debug_filename = f"debug_{recording_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}{file_ext}"
                        debug_filepath = os.path.join(debug_dir, debug_filename)
                        copy2(converted_filepath, debug_filepath)

                        current_app.logger.info(f"DEBUG: Converted file preserved at: {debug_filepath}")
                        current_app.logger.info(f"DEBUG: Converted file size: {converted_size_mb:.2f} MB ({converted_size} bytes)")
                        current_app.logger.info(f"DEBUG: Original file: {actual_filename}")
                        current_app.logger.info(f"DEBUG: Recording ID: {recording_id}")

                    except Exception as debug_error:
                        current_app.logger.warning(f"DEBUG: Failed to preserve converted file: {debug_error}")

            # Clean up the temporary converted file
            try:
                if converted_filepath and os.path.exists(converted_filepath):
                    os.remove(converted_filepath)
                    current_app.logger.info(f"Cleaned up temporary converted file: {converted_filepath}")
            except Exception as cleanup_error:
                current_app.logger.warning(f"Failed to clean up temporary converted file: {cleanup_error}")

            # Debug logging for ASR response
            current_app.logger.info(f"ASR response keys: {list(asr_response_data.keys())}")

            # Log the complete raw JSON response (truncated for readability)
            import json as json_module
            raw_json_str = json_module.dumps(asr_response_data, indent=2)
            if len(raw_json_str) > 5000:
                current_app.logger.info(f"Raw ASR response (first 5000 chars): {raw_json_str[:5000]}...")
            else:
                current_app.logger.info(f"Raw ASR response: {raw_json_str}")

            if 'segments' in asr_response_data:
                current_app.logger.info(f"Number of segments: {len(asr_response_data['segments'])}")

                # Collect all unique speakers from the response
                all_speakers = set()
                segments_with_speakers = 0
                segments_without_speakers = 0

                for segment in asr_response_data['segments']:
                    if 'speaker' in segment and segment['speaker'] is not None:
                        all_speakers.add(segment['speaker'])
                        segments_with_speakers += 1
                    else:
                        segments_without_speakers += 1

                current_app.logger.info(f"Unique speakers found in raw response: {sorted(list(all_speakers))}")
                current_app.logger.info(f"Segments with speakers: {segments_with_speakers}, without speakers: {segments_without_speakers}")

                # Log first few segments for debugging
                for i, segment in enumerate(asr_response_data['segments'][:5]):
                    segment_keys = list(segment.keys())
                    current_app.logger.info(f"Segment {i} keys: {segment_keys}")
                    current_app.logger.info(f"Segment {i}: speaker='{segment.get('speaker')}', text='{segment.get('text', '')[:50]}...'")

            # Simplify the JSON data
            simplified_segments = []
            if 'segments' in asr_response_data and isinstance(asr_response_data['segments'], list):
                last_known_speaker = None

                for i, segment in enumerate(asr_response_data['segments']):
                    speaker = segment.get('speaker')
                    text = segment.get('text', '').strip()

                    # If segment doesn't have a speaker, use the previous segment's speaker
                    if speaker is None:
                        if last_known_speaker is not None:
                            speaker = last_known_speaker
                            current_app.logger.info(f"Assigned speaker '{speaker}' to segment {i} from previous segment")
                        else:
                            speaker = 'UNKNOWN_SPEAKER'
                            current_app.logger.warning(f"No previous speaker available for segment {i}, using UNKNOWN_SPEAKER")
                    else:
                        # Update the last known speaker when we have a valid one
                        last_known_speaker = speaker

                    simplified_segments.append({
                        'speaker': speaker,
                        'sentence': text,
                        'start_time': segment.get('start'),
                        'end_time': segment.get('end')
                    })

            # Log final simplified segments count
            current_app.logger.info(f"Created {len(simplified_segments)} simplified segments")
            null_speaker_count = sum(1 for seg in simplified_segments if seg['speaker'] is None)
            if null_speaker_count > 0:
                current_app.logger.warning(f"Found {null_speaker_count} segments with null speakers in final output")

            # Store the simplified JSON as a string
            recording.transcription = json.dumps(simplified_segments)

            # Commit the transcription data
            db.session.commit()

            # Calculate and save transcription duration
            transcription_end_time = time.time()
            recording.transcription_duration_seconds = int(transcription_end_time - transcription_start_time)
            db.session.commit()
            current_app.logger.info(f"ASR transcription completed for recording {recording_id} in {recording.transcription_duration_seconds}s.")

            # Check if auto-summarization is disabled
            disable_auto_summarization = SystemSetting.get_setting('disable_auto_summarization', False)
            will_auto_summarize = not disable_auto_summarization

            # Generate title immediately (pass flag so it knows whether to set COMPLETED)
            generate_title_task(app_context, recording_id, will_auto_summarize=will_auto_summarize)

            if disable_auto_summarization:
                current_app.logger.info(f"Auto-summarization disabled, skipping summary for recording {recording_id}")
                recording = db.session.get(Recording, recording_id)
                if recording:
                    recording.status = 'COMPLETED'
                    recording.completed_at = datetime.utcnow()
                    db.session.commit()

                    # Apply auto-shares for group tags after processing completes
                    apply_team_tag_auto_shares(recording_id)

                    # Export transcription-only if auto-export is enabled
                    if ENABLE_AUTO_EXPORT:
                        export_recording(recording_id)
            else:
                # Auto-generate summary for all recordings
                current_app.logger.info(f"Auto-generating summary for recording {recording_id}")
                generate_summary_only_task(app_context, recording_id)

        except Exception as e:
            db.session.rollback()

            # Handle timeout errors specifically
            error_msg = str(e)
            error_type = type(e).__name__
            current_app.logger.error(f"ASR processing FAILED for recording {recording_id}: [{error_type}] {error_msg}")

            if "timed out" in error_msg.lower() or "timeout" in error_msg.lower() or "Timeout" in error_type:
                asr_timeout = SystemSetting.get_setting('asr_timeout_seconds', 1800)
                current_app.logger.error(f"Timeout details - configured ASR timeout: {asr_timeout}s. Error: {error_msg}")

            # Don't set recording.status = 'FAILED' here - let the job queue handle it
            # The job queue will decide whether to retry or permanently fail,
            # and only set FAILED status when all retries are exhausted

            # Re-raise so job queue marks the job as failed (and potentially retries)
            raise


def transcribe_audio_task(app_context, recording_id, filepath, filename_for_asr, start_time, language=None, min_speakers=None, max_speakers=None, tag_id=None):
    """Runs the transcription and summarization in a background thread.

    Args:
        app_context: Flask app context
        recording_id: ID of the recording to process
        filepath: Path to the audio file
        filename_for_asr: Filename to use for ASR
        start_time: Processing start time
        language: Optional language code override (from upload form)
        min_speakers: Optional minimum speakers override (from upload form)
        max_speakers: Optional maximum speakers override (from upload form)
        tag_id: Optional tag ID to apply custom prompt from
    """
    # Use new connector-based architecture if enabled
    if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
        with app_context:
            recording = db.session.get(Recording, recording_id)
            # Determine diarization setting based on connector capabilities
            # The connector will handle this, but we pass the user's preference
            diarize_setting = None  # Let connector decide based on its capabilities

            # Use language from upload form if provided, otherwise use user's default
            if language:
                user_transcription_language = language
            else:
                user_transcription_language = recording.owner.transcription_language if recording and recording.owner else None

            mime_type = recording.mime_type if recording else None

        transcribe_with_connector(
            app_context, recording_id, filepath, filename_for_asr, start_time,
            mime_type=mime_type,
            language=user_transcription_language,
            diarize=diarize_setting,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            tag_id=tag_id
        )

        # After transcription completes, calculate processing time
        with app_context:
            recording = db.session.get(Recording, recording_id)
            if recording and recording.status in ['COMPLETED', 'FAILED']:
                end_time = datetime.utcnow()
                recording.processing_time_seconds = (end_time - start_time).total_seconds()
                db.session.commit()
        return

    # Legacy path: use old implementation for backwards compatibility
    if USE_ASR_ENDPOINT:
        with app_context:
            recording = db.session.get(Recording, recording_id)
            # Environment variable ASR_DIARIZE overrides user setting
            if 'ASR_DIARIZE' in os.environ:
                diarize_setting = ASR_DIARIZE
            elif USE_ASR_ENDPOINT:
                # When using ASR endpoint, use the configured ASR_DIARIZE value
                diarize_setting = ASR_DIARIZE
            else:
                diarize_setting = recording.owner.diarize if recording.owner else False

            # Use language from upload form if provided, otherwise use user's default
            if language:
                user_transcription_language = language
            else:
                user_transcription_language = recording.owner.transcription_language if recording.owner else None
        # Use min/max speakers from upload form (already processed with precedence hierarchy)
        # If None, ASR will auto-detect the number of speakers
        final_min_speakers = min_speakers
        final_max_speakers = max_speakers

        transcribe_audio_asr(app_context, recording_id, filepath, filename_for_asr, start_time,
                           mime_type=recording.mime_type,
                           language=user_transcription_language,
                           diarize=diarize_setting,
                           min_speakers=final_min_speakers,
                           max_speakers=final_max_speakers,
                           tag_id=tag_id)

        # After ASR task completes, calculate processing time
        with app_context:
            recording = db.session.get(Recording, recording_id)
            if recording.status in ['COMPLETED', 'FAILED']:
                end_time = datetime.utcnow()
                recording.processing_time_seconds = (end_time - start_time).total_seconds()
                db.session.commit()
        return

    with app_context: # Need app context for db operations in thread
        recording = db.session.get(Recording, recording_id)
        if not recording:
            current_app.logger.error(f"Error: Recording {recording_id} not found for transcription.")
            return

        try:
            current_app.logger.info(f"Starting transcription for recording {recording_id} ({filename_for_asr})...")
            recording.status = 'PROCESSING'
            transcription_start_time = time.time()
            db.session.commit()

            # Check if chunking is needed for large files
            # Get connector specifications for smart chunking decisions
            connector_specs = None
            if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
                try:
                    from src.services.transcription import get_registry
                    registry = get_registry()
                    connector = registry.get_active_connector()
                    if connector:
                        connector_specs = connector.specifications
                except Exception as e:
                    current_app.logger.warning(f"Could not get connector specs for chunking: {e}")

            # Use connector-aware chunking (respects connector.handles_chunking_internally,
            # user ENV settings, and connector defaults in that priority order)
            should_chunk = (chunking_service and
                           chunking_service.needs_chunking(filepath, USE_ASR_ENDPOINT, connector_specs))

            if should_chunk:
                current_app.logger.info(f"File {filepath} is large ({os.path.getsize(filepath)/1024/1024:.1f}MB), using chunking for transcription")
                transcription_text = transcribe_with_chunking(app_context, recording_id, filepath, filename_for_asr)
            else:
                # --- Standard transcription for smaller files ---
                transcription_text = transcribe_single_file(filepath, recording)

            recording.transcription = transcription_text

            # Calculate and save transcription duration
            transcription_end_time = time.time()
            recording.transcription_duration_seconds = int(transcription_end_time - transcription_start_time)
            db.session.commit()
            current_app.logger.info(f"Transcription completed for recording {recording_id} in {recording.transcription_duration_seconds}s. Text length: {len(recording.transcription)}")

            # Check if auto-summarization is disabled
            disable_auto_summarization = SystemSetting.get_setting('disable_auto_summarization', False)
            will_auto_summarize = not disable_auto_summarization

            # Generate title immediately (pass flag so it knows whether to set COMPLETED)
            generate_title_task(app_context, recording_id, will_auto_summarize=will_auto_summarize)

            if disable_auto_summarization:
                current_app.logger.info(f"Auto-summarization disabled, skipping summary for recording {recording_id}")
                recording.status = 'COMPLETED'
                recording.completed_at = datetime.utcnow()
                db.session.commit()

                # Apply auto-shares for group tags after processing completes
                apply_team_tag_auto_shares(recording_id)

                # Export transcription-only if auto-export is enabled
                if ENABLE_AUTO_EXPORT:
                    export_recording(recording_id)
            else:
                # Auto-generate summary for all recordings
                current_app.logger.info(f"Auto-generating summary for recording {recording_id}")
                generate_summary_only_task(app_context, recording_id)

        except Exception as e:
            db.session.rollback() # Rollback if any step failed critically
            current_app.logger.error(f"Processing FAILED for recording {recording_id}: {str(e)}", exc_info=True)
            # Retrieve recording again in case session was rolled back
            recording = db.session.get(Recording, recording_id)
            if recording:
                 # Ensure status reflects failure even after rollback/retrieve attempt
                if recording.status not in ['COMPLETED', 'FAILED']: # Avoid overwriting final state
                    recording.status = 'FAILED'
                if not recording.transcription: # If transcription itself failed
                     recording.transcription = format_error_for_storage(str(e))
                # Add error note to summary if appropriate stage was reached
                if recording.status == 'SUMMARIZING' and not recording.summary:
                     recording.summary = f"[Processing failed during summarization: {str(e)}]"

                end_time = datetime.utcnow()
                recording.processing_time_seconds = (end_time - start_time).total_seconds()
                db.session.commit()

            # Re-raise so job queue marks the job as failed
            raise


def transcribe_single_file(filepath, recording):
    """Transcribe a single audio file using OpenAI Whisper API."""

    # Check if we need to extract audio from video container
    actual_filepath = filepath
    mime_type = recording.mime_type if recording else None

    # Use codec detection to check if file is a video
    try:
        is_video = is_video_file(filepath, timeout=10)
        if is_video:
            current_app.logger.info(f"Video detected via codec analysis for {filepath}")
    except FFProbeError as e:
        current_app.logger.warning(f"Failed to probe {filepath}: {e}. Falling back to MIME type detection.")
        # Fallback to MIME type detection
        if mime_type:
            is_video = mime_type.startswith('video/')
        else:
            is_video = False

    if is_video:
        current_app.logger.info(f"Video container detected for Whisper transcription, extracting audio...")
        try:
            # Extract audio from video (uses compression settings)
            audio_filepath, audio_mime_type = extract_audio_from_video(filepath)
            actual_filepath = audio_filepath

            # Update recording with extracted audio path and new MIME type if recording exists
            if recording:
                recording.audio_path = audio_filepath
                recording.mime_type = audio_mime_type
                db.session.commit()

            current_app.logger.info(f"Audio extracted successfully for Whisper: {audio_filepath}")
        except Exception as e:
            current_app.logger.error(f"Failed to extract audio from video for Whisper: {str(e)}")
            if recording:
                recording.status = 'FAILED'
                recording.error_msg = f"Audio extraction failed: {str(e)}"
                db.session.commit()
            raise Exception(f"Audio extraction failed: {str(e)}")

    # List of formats supported by Whisper API
    WHISPER_SUPPORTED_FORMATS = ['flac', 'm4a', 'mp3', 'mp4', 'mpeg', 'mpga', 'oga', 'ogg', 'wav', 'webm']

    # Get user transcription language preference
    user_transcription_language = None
    if recording and recording.owner:
        user_transcription_language = recording.owner.transcription_language

    transcription_language = user_transcription_language

    try:
        with open(actual_filepath, 'rb') as audio_file:
            transcription_client = OpenAI(
                api_key=transcription_api_key,
                base_url=transcription_base_url,
                http_client=http_client_no_proxy
            )
            whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")

            transcription_params = {
                "model": whisper_model,
                "file": audio_file
            }

            if transcription_language:
                transcription_params["language"] = transcription_language
                current_app.logger.info(f"Using transcription language: {transcription_language}")
            else:
                current_app.logger.info("Transcription language not set, using auto-detection or service default.")

            transcript = transcription_client.audio.transcriptions.create(**transcription_params)
            return transcript.text

    except Exception as e:
        # Check if it's a format error
        error_message = str(e)
        if "Invalid file format" in error_message or "Supported formats" in error_message:
            file_ext = os.path.splitext(actual_filepath)[1].lower().lstrip('.')
            current_app.logger.warning(f"Unsupported audio format '{file_ext}' detected, converting to MP3...")

            # Convert to MP3 using centralized utility
            temp_mp3_filepath = None
            try:
                temp_mp3_filepath = convert_to_mp3(actual_filepath)
                current_app.logger.info(f"Successfully converted {actual_filepath} to MP3 format")

                # Retry transcription with converted file
                with open(temp_mp3_filepath, 'rb') as audio_file:
                    transcription_client = OpenAI(
                        api_key=transcription_api_key,
                        base_url=transcription_base_url,
                        http_client=http_client_no_proxy
                    )

                    whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")
                    transcription_params = {
                        "model": whisper_model,
                        "file": audio_file
                    }

                    if transcription_language:
                        transcription_params["language"] = transcription_language

                    transcript = transcription_client.audio.transcriptions.create(**transcription_params)
                    return transcript.text

            except (FFmpegError, FFmpegNotFoundError) as conv_error:
                current_app.logger.error(f"Failed to convert audio format: {conv_error}")
                raise Exception(f"Audio format conversion failed: {str(conv_error)}")
            finally:
                # Clean up temporary converted file
                if temp_mp3_filepath and os.path.exists(temp_mp3_filepath):
                    try:
                        os.unlink(temp_mp3_filepath)
                        current_app.logger.info(f"Cleaned up temporary converted file: {temp_mp3_filepath}")
                    except Exception as cleanup_error:
                        current_app.logger.warning(f"Failed to clean up temporary file {temp_mp3_filepath}: {cleanup_error}")
        else:
            # Re-raise if it's not a format error
            raise



def transcribe_with_chunking(app_context, recording_id, filepath, filename_for_asr):
    """Transcribe a large audio file using chunking."""
    import tempfile

    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")

    # Create temporary directory for chunks
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Create chunks
            current_app.logger.info(f"Creating chunks for large file: {filepath}")
            chunks = chunking_service.create_chunks(filepath, temp_dir)

            if not chunks:
                raise ChunkProcessingError("No chunks were created from the audio file")

            current_app.logger.info(f"Created {len(chunks)} chunks, processing each with Whisper API...")

            # Process each chunk with proper timeout and retry handling
            chunk_results = []

            # Create HTTP client with proper timeouts
            timeout_config = httpx.Timeout(
                connect=30.0,    # 30 seconds to establish connection
                read=300.0,      # 5 minutes to read response (for large audio files)
                write=60.0,      # 1 minute to write request
                pool=10.0        # 10 seconds to get connection from pool
            )

            http_client_with_timeout = httpx.Client(
                verify=True,
                timeout=timeout_config,
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2)
            )

            transcription_client = OpenAI(
                api_key=transcription_api_key,
                base_url=transcription_base_url,
                http_client=http_client_with_timeout,
                max_retries=3,  # Increased retries for better reliability
                timeout=300.0   # 5 minute timeout for API calls
            )
            whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")

            # Get user language preference
            user_transcription_language = None
            with app_context:
                recording = db.session.get(Recording, recording_id)
                if recording and recording.owner:
                    user_transcription_language = recording.owner.transcription_language

            for i, chunk in enumerate(chunks):
                max_chunk_retries = 3
                chunk_retry_count = 0
                chunk_success = False

                while chunk_retry_count < max_chunk_retries and not chunk_success:
                    try:
                        retry_suffix = f" (retry {chunk_retry_count + 1}/{max_chunk_retries})" if chunk_retry_count > 0 else ""
                        current_app.logger.info(f"Processing chunk {i+1}/{len(chunks)}: {chunk['filename']} ({chunk['size_mb']:.1f}MB){retry_suffix}")

                        # Log detailed timing for each step
                        step_start_time = time.time()

                        # Step 1: File opening
                        file_open_start = time.time()
                        with open(chunk['path'], 'rb') as chunk_file:
                            file_open_time = time.time() - file_open_start
                            current_app.logger.info(f"Chunk {i+1}: File opened in {file_open_time:.2f}s")

                            # Step 2: Prepare transcription parameters
                            param_start = time.time()
                            transcription_params = {
                                "model": whisper_model,
                                "file": chunk_file
                            }

                            if user_transcription_language:
                                transcription_params["language"] = user_transcription_language

                            param_time = time.time() - param_start
                            current_app.logger.info(f"Chunk {i+1}: Parameters prepared in {param_time:.2f}s")

                            # Step 3: API call with detailed timing
                            api_start = time.time()
                            current_app.logger.info(f"Chunk {i+1}: Starting API call to {transcription_base_url}")

                            # Log connection details
                            current_app.logger.info(f"Chunk {i+1}: Using timeout config - connect: 30s, read: 300s, write: 60s")
                            current_app.logger.info(f"Chunk {i+1}: Max retries: 2, API timeout: 300s")

                            try:
                                transcript = transcription_client.audio.transcriptions.create(**transcription_params)
                            except Exception as chunk_error:
                                # Check if it's a format error (unlikely for chunks since they're MP3, but handle it)
                                error_msg = str(chunk_error)
                                if "Invalid file format" in error_msg or "Supported formats" in error_msg:
                                    current_app.logger.warning(f"Chunk {i+1} format issue, attempting conversion...")
                                    # Convert chunk to MP3 if needed
                                    temp_mp3_path = None
                                    try:
                                        temp_mp3_path = convert_to_mp3(chunk['path'])
                                        with open(temp_mp3_path, 'rb') as converted_chunk:
                                            transcription_params['file'] = converted_chunk
                                            transcript = transcription_client.audio.transcriptions.create(**transcription_params)
                                    except (FFmpegError, FFmpegNotFoundError) as conv_error:
                                        current_app.logger.error(f"Failed to convert chunk {i+1}: {conv_error}")
                                        raise chunk_error
                                    finally:
                                        if temp_mp3_path and os.path.exists(temp_mp3_path):
                                            os.unlink(temp_mp3_path)
                                else:
                                    raise

                            api_time = time.time() - api_start
                            current_app.logger.info(f"Chunk {i+1}: API call completed in {api_time:.2f}s")

                            # Step 4: Process response
                            response_start = time.time()
                            chunk_result = {
                                'index': chunk['index'],
                                'start_time': chunk['start_time'],
                                'end_time': chunk['end_time'],
                                'duration': chunk['duration'],
                                'size_mb': chunk['size_mb'],
                                'transcription': transcript.text,
                                'filename': chunk['filename'],
                                'processing_time': api_time  # Store the actual API processing time
                            }
                            chunk_results.append(chunk_result)
                            response_time = time.time() - response_start

                            total_time = time.time() - step_start_time
                            current_app.logger.info(f"Chunk {i+1}: Response processed in {response_time:.2f}s")
                            current_app.logger.info(f"Chunk {i+1}: Total processing time: {total_time:.2f}s")
                            current_app.logger.info(f"Chunk {i+1} transcribed successfully: {len(transcript.text)} characters")
                            chunk_success = True

                    except Exception as chunk_error:
                        chunk_retry_count += 1
                        error_msg = str(chunk_error)

                        if chunk_retry_count < max_chunk_retries:
                            # Determine wait time based on error type
                            if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                                wait_time = 30  # 30 seconds for timeout errors
                            elif "rate limit" in error_msg.lower():
                                wait_time = 60  # 1 minute for rate limit errors
                            else:
                                wait_time = 15  # 15 seconds for other errors

                            current_app.logger.warning(f"Chunk {i+1} failed (attempt {chunk_retry_count}/{max_chunk_retries}): {chunk_error}. Retrying in {wait_time} seconds...")
                            time.sleep(wait_time)
                        else:
                            current_app.logger.error(f"Chunk {i+1} failed after {max_chunk_retries} attempts: {chunk_error}")
                            # Add failed chunk to results
                            chunk_result = {
                                'index': chunk['index'],
                                'start_time': chunk['start_time'],
                                'end_time': chunk['end_time'],
                                'transcription': f"[Chunk {i+1} transcription failed after {max_chunk_retries} attempts: {str(chunk_error)}]",
                                'filename': chunk['filename']
                            }
                            chunk_results.append(chunk_result)

                # Add small delay between chunks to avoid overwhelming the API
                if i < len(chunks) - 1:  # Don't delay after the last chunk
                    time.sleep(2)

            # Merge transcriptions
            current_app.logger.info(f"Merging {len(chunk_results)} chunk transcriptions...")
            merged_transcription = chunking_service.merge_transcriptions(chunk_results)

            if not merged_transcription.strip():
                raise ChunkProcessingError("Merged transcription is empty")

            # Log detailed performance statistics and analysis
            chunking_service.log_processing_statistics(chunk_results)

            # Get performance recommendations
            recommendations = chunking_service.get_performance_recommendations(chunk_results)
            if recommendations:
                current_app.logger.info("=== PERFORMANCE RECOMMENDATIONS ===")
                for i, rec in enumerate(recommendations, 1):
                    current_app.logger.info(f"{i}. {rec}")
                current_app.logger.info("=== END RECOMMENDATIONS ===")

            current_app.logger.info(f"Chunked transcription completed. Final length: {len(merged_transcription)} characters")
            return merged_transcription

        except Exception as e:
            current_app.logger.error(f"Chunking transcription failed for {filepath}: {e}")
            # Clean up chunks if they exist
            if 'chunks' in locals():
                chunking_service.cleanup_chunks(chunks)
            raise ChunkProcessingError(f"Chunked transcription failed: {str(e)}")
        finally:
            # Cleanup is handled by tempfile.TemporaryDirectory context manager
            pass
