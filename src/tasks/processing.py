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
from src.models import Recording, Tag, Event, TranscriptChunk, SystemSetting, GroupMembership, RecordingTag, InternalShare, SharedRecordingState, User, NamingTemplate
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
    transcription_api_key, transcription_base_url, chunking_service, ENABLE_CHUNKING
)
from src.file_exporter import export_recording, ENABLE_AUTO_EXPORT
from src.services.transcription_tracking import transcription_tracker

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
    # Handle lines individually to preserve Markdown hard line breaks (two spaces at end)
    lines = cleaned.split('\n')
    cleaned_lines = []
    
    for line in lines:
        # If a line consists only of whitespace (e.g., after tag removal),
        # make it completely empty. This is necessary for the \n{3,} regex to work later.
        if not line.strip():
            cleaned_lines.append("")
        else:
            # Preserve lines containing text exactly as they are
            # This keeps trailing spaces needed for Markdown hard line breaks intact
            cleaned_lines.append(line)

    # Join lines and collapse 3+ consecutive newlines into exactly 2 (one blank line)
    cleaned = '\n'.join(cleaned_lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    # Final strip to remove leading/trailing whitespace
    return cleaned.strip()

# Configuration from environment
# Note: Legacy ASR paths (USE_ASR_ENDPOINT, transcribe_audio_asr, etc.) were removed.
# All transcription now uses the connector architecture via transcribe_with_connector().
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

        # Resolve naming template: first tag with template → user default → None
        naming_template = None
        for tag in recording.tags:
            if tag.naming_template_id:
                naming_template = tag.naming_template
                current_app.logger.info(f"Using naming template '{naming_template.name}' from tag '{tag.name}' for recording {recording_id}")
                break

        if not naming_template and recording.owner and recording.owner.default_naming_template_id:
            naming_template = recording.owner.default_naming_template
            if naming_template:
                current_app.logger.info(f"Using user's default naming template '{naming_template.name}' for recording {recording_id}")

        # Check if we need to generate AI title
        needs_ai_title = naming_template is None or naming_template.needs_ai_title()

        # Early exit conditions
        if not needs_ai_title:
            # Template doesn't need AI - we can skip LLM call entirely
            current_app.logger.info(f"Naming template doesn't require AI title for recording {recording_id}, skipping LLM call")
            ai_title = None
        elif client is None:
            current_app.logger.warning(f"Skipping AI title generation for {recording_id}: OpenRouter client not configured.")
            ai_title = None
        elif not recording.transcription or len(recording.transcription.strip()) < 10:
            current_app.logger.warning(f"Transcription for recording {recording_id} is too short or empty. Skipping AI title generation.")
            ai_title = None
        else:
            # Generate AI title via LLM
            ai_title = _generate_ai_title(recording)

        # Apply naming template if we have one
        final_title = None
        if naming_template:
            final_title = naming_template.apply(
                original_filename=recording.original_filename,
                meeting_date=recording.meeting_date,
                ai_title=ai_title
            )
            if final_title:
                current_app.logger.info(f"Applied naming template for recording {recording_id}: '{final_title}'")

        # Fallback chain: template result → AI title → filename
        if not final_title:
            if ai_title:
                final_title = ai_title
            elif recording.original_filename:
                # Use filename without extension as last resort
                import os
                final_title = os.path.splitext(recording.original_filename)[0]
                current_app.logger.info(f"Using filename as title for recording {recording_id}: '{final_title}'")

        if final_title:
            recording.title = final_title
            current_app.logger.info(f"Title set for recording {recording_id}: {final_title}")
        else:
            current_app.logger.warning(f"Could not generate title for recording {recording_id}")

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


def _generate_ai_title(recording):
    """Generate an AI title for a recording using LLM.

    Args:
        recording: Recording model instance

    Returns:
        Generated title string, or None if generation fails
    """
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
            current_app.logger.info(f"Title generation for recording {recording.id}: Using reasoning field as fallback")
            # Try to extract a title from the reasoning field
            lines = reasoning.strip().split('\n')
            # Look for the last line that might be the title
            for line in reversed(lines):
                line = line.strip()
                if line and not line.startswith('I') and len(line.split()) <= 8:
                    raw_response = line
                    break

        title = clean_llm_response(raw_response) if raw_response else None

        if title:
            current_app.logger.info(f"AI title generated for recording {recording.id}: {title}")
        else:
            current_app.logger.warning(f"Empty AI title generated for recording {recording.id}")

        return title

    except Exception as e:
        current_app.logger.error(f"Error generating AI title for recording {recording.id}: {str(e)}")
        current_app.logger.error(f"Exception details:", exc_info=True)
        return None


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

        # Get folder custom prompt (if recording has a folder)
        # Folder prompt has lower priority than tag prompts (tags override folders)
        folder_custom_prompt = None
        if recording.folder and recording.folder.custom_prompt and recording.folder.custom_prompt.strip():
            folder_custom_prompt = recording.folder.custom_prompt.strip()
            current_app.logger.info(f"Found custom prompt from folder '{recording.folder.name}' for recording {recording_id}")

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
        # Priority order: custom_prompt_override > tag custom prompt > folder custom prompt > user summary prompt > admin default prompt > hardcoded fallback
        summarization_instructions = ""
        if custom_prompt_override:
            current_app.logger.info(f"Using custom prompt override for recording {recording_id} (length: {len(custom_prompt_override)})")
            summarization_instructions = custom_prompt_override
        elif tag_custom_prompt:
            current_app.logger.info(f"Using tag custom prompt for recording {recording_id}")
            summarization_instructions = tag_custom_prompt
        elif folder_custom_prompt:
            current_app.logger.info(f"Using folder custom prompt for recording {recording_id}")
            summarization_instructions = folder_custom_prompt
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

        # Add folder information if recording is in a folder
        if recording.folder:
            context_parts.append(f"Folder: {recording.folder.name}")

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

        # Delete existing events for this recording before extracting new ones
        existing_events = Event.query.filter_by(recording_id=recording_id).all()
        if existing_events:
            current_app.logger.info(f"Clearing {len(existing_events)} existing events for recording {recording_id}")
            for event in existing_events:
                db.session.delete(event)
            db.session.commit()

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
    Merge diarized transcription chunks while remapping speaker labels to be unique.

    Since ASR services can't maintain speaker identity across chunks, each chunk's
    speakers are remapped to unique IDs:
    - Chunk 1: SPEAKER_00, SPEAKER_01 → SPEAKER_00, SPEAKER_01
    - Chunk 2: SPEAKER_00, SPEAKER_01 → SPEAKER_02, SPEAKER_03
    - etc.

    This function:
    1. Remaps speaker labels to be unique across all chunks
    2. Updates both segments and transcription text with new labels
    3. Adjusts timestamps based on chunk start_time

    Args:
        chunk_results: List of chunk results with 'transcription', 'segments', 'start_time'

    Returns:
        Tuple of (merged_text, merged_segments, all_speakers)
    """
    from src.services.transcription import TranscriptionSegment
    import re

    if not chunk_results:
        return "", [], []

    # Sort chunks by start time to ensure correct order
    sorted_chunks = sorted(chunk_results, key=lambda x: x.get('start_time', 0))

    merged_parts = []
    merged_segments = []
    all_speakers = set()
    next_speaker_number = 0  # Track the next available speaker number

    for chunk_idx, chunk in enumerate(sorted_chunks):
        chunk_segments = chunk.get('segments') or []

        # Build speaker remapping for this chunk
        # Maps original speaker label -> new unique speaker label
        chunk_speakers = set()
        for seg in chunk_segments:
            if hasattr(seg, 'speaker'):
                speaker = seg.speaker
            else:
                speaker = seg.get('speaker', 'Unknown')
            if speaker:
                chunk_speakers.add(speaker)

        # Also check chunk metadata for speakers
        if chunk.get('speakers'):
            for s in chunk['speakers']:
                chunk_speakers.add(s)

        # Create remapping: sort speakers to ensure deterministic ordering
        speaker_remap = {}
        for original_speaker in sorted(chunk_speakers):
            if original_speaker and original_speaker != 'Unknown':
                # Extract number from speaker label (e.g., SPEAKER_00 -> 0)
                # For first chunk, keep original numbering; for subsequent chunks, remap
                if chunk_idx == 0:
                    # First chunk: keep original labels but track highest number
                    speaker_remap[original_speaker] = original_speaker
                    match = re.search(r'(\d+)$', original_speaker)
                    if match:
                        num = int(match.group(1))
                        next_speaker_number = max(next_speaker_number, num + 1)
                else:
                    # Subsequent chunks: remap to new unique numbers
                    new_speaker = f"SPEAKER_{next_speaker_number:02d}"
                    speaker_remap[original_speaker] = new_speaker
                    next_speaker_number += 1

        # Update transcription text with remapped speakers
        chunk_text = chunk.get('transcription', '').strip()
        if chunk_text and chunk_idx > 0:
            # Replace speaker labels in text (e.g., [SPEAKER_00]: -> [SPEAKER_02]:)
            for original, remapped in speaker_remap.items():
                if original != remapped:
                    # Handle various formats: [SPEAKER_00]:, SPEAKER_00:, (SPEAKER_00)
                    chunk_text = re.sub(
                        rf'\[{re.escape(original)}\]',
                        f'[{remapped}]',
                        chunk_text
                    )
                    chunk_text = re.sub(
                        rf'(?<!\[){re.escape(original)}(?![\d])',
                        remapped,
                        chunk_text
                    )

        if chunk_text:
            merged_parts.append(chunk_text)

        # Merge segments with adjusted timestamps and remapped speakers
        chunk_start_offset = chunk.get('start_time', 0)

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

            # Remap speaker label
            remapped_speaker = speaker_remap.get(speaker, speaker)
            all_speakers.add(remapped_speaker)

            # Adjust timestamps by chunk offset
            adjusted_start = (start_time or 0) + chunk_start_offset
            adjusted_end = (end_time or 0) + chunk_start_offset

            merged_segments.append(TranscriptionSegment(
                text=text,
                speaker=remapped_speaker,
                start_time=adjusted_start,
                end_time=adjusted_end
            ))

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

            # Check transcription budget before processing
            can_proceed, usage_pct, budget_msg = transcription_tracker.check_budget(recording.user_id)
            if not can_proceed:
                current_app.logger.warning(f"User {recording.user_id} exceeded transcription budget: {budget_msg}")
                recording.status = 'FAILED'
                recording.error_msg = budget_msg
                db.session.commit()
                return
            elif budget_msg:
                # Log warning but continue
                current_app.logger.warning(budget_msg)

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

                    # Legacy/repair fallback path: in the normal upload flow, video containers should
                    # already be converted/extracted before the Recording is stored. We still handle
                    # video here for historical records / reprocess jobs where a video object may
                    # remain in recording.audio_path.
                    # TODO(storage-cleanup): after migrating legacy video-backed recordings to audio
                    # objects and normalizing cached durations, this branch should be removable.
                    try:
                        from src.services.storage import get_storage_service
                        storage = get_storage_service()
                        old_audio_locator = recording.audio_path
                        extracted_key = storage.build_recording_key(actual_filename, recording.id)
                        stored_audio = storage.upload_local_file(
                            audio_filepath,
                            extracted_key,
                            content_type=audio_mime_type,
                            delete_source=False,
                        )
                        recording.audio_path = stored_audio.locator
                        recording.mime_type = audio_mime_type
                        try:
                            extracted_duration = chunking_service.get_audio_duration(audio_filepath) if chunking_service else None
                            if extracted_duration and extracted_duration > 0:
                                recording.audio_duration_seconds = float(extracted_duration)
                        except Exception as duration_err:
                            current_app.logger.warning(f"Could not determine extracted audio duration for recording {recording.id}: {duration_err}")
                        db.session.commit()
                        current_app.logger.info(f"Audio extracted and stored: {stored_audio.locator}")

                        # Best-effort cleanup of previous stored source (e.g. original video)
                        if old_audio_locator and old_audio_locator != stored_audio.locator:
                            try:
                                storage.delete(old_audio_locator, missing_ok=True)
                            except Exception as cleanup_err:
                                current_app.logger.warning(f"Failed to delete original source after audio extraction: {cleanup_err}")
                    except Exception:
                        # Do NOT persist temporary extracted path in DB. Keep previous locator and continue
                        # processing with the local extracted file for this job only.
                        recording.mime_type = audio_mime_type
                        db.session.commit()
                        current_app.logger.warning(
                            "Failed to persist extracted audio via storage layer; keeping previous recording.audio_path and continuing with temporary local file",
                            exc_info=True,
                        )
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
                            # Store as plain text (ensure it's a string)
                            transcription_text = response.text if isinstance(response.text, str) else ''
                            recording.transcription = transcription_text
                            current_app.logger.info(f"Transcription completed: {len(transcription_text)} characters")

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

            # Persist/correct audio duration cache when we have local access during processing.
            # Prefer the original job input path for billing consistency, but fall back to the
            # actual processed file (e.g., extracted audio from video) when needed.
            try:
                cached_audio_duration = None
                if chunking_service:
                    # Legacy cache-repair fallback: new uploads should already populate
                    # recording.audio_duration_seconds, and extracted-video paths above also try to do
                    # that. This probe exists to repair historical rows and edge cases during
                    # processing/reprocessing while we still have local access to media.
                    # TODO(storage-cleanup): after running backfill/migrations for missing
                    # audio_duration_seconds (and cleaning legacy video-backed objects), remove this
                    # multi-path probing block and rely on DB-stored duration.
                    duration_probe_candidates = []
                    if filepath:
                        duration_probe_candidates.append(filepath)
                    if actual_filepath and actual_filepath not in duration_probe_candidates:
                        duration_probe_candidates.append(actual_filepath)
                    for candidate_path in duration_probe_candidates:
                        cached_audio_duration = chunking_service.get_audio_duration(candidate_path)
                        if cached_audio_duration and cached_audio_duration > 0:
                            recording.audio_duration_seconds = float(cached_audio_duration)
                            break
            except Exception as duration_err:
                current_app.logger.warning(f"Failed to update cached audio duration for recording {recording_id}: {duration_err}")

            db.session.commit()
            current_app.logger.info(f"Transcription completed in {recording.transcription_duration_seconds}s")

            # Record transcription usage for billing/budgeting
            try:
                # Get actual audio duration (not processing time)
                audio_duration = recording.audio_duration_seconds
                if (audio_duration is None or audio_duration <= 0) and chunking_service:
                    audio_duration = chunking_service.get_audio_duration(filepath)

                if audio_duration and audio_duration > 0:
                    # Get model name from connector if available
                    model_name = getattr(connector, 'model', None) or connector_name
                    transcription_tracker.record_usage(
                        user_id=recording.user_id,
                        connector_type=connector_name,
                        audio_duration_seconds=int(audio_duration),
                        model_name=model_name
                    )
                    current_app.logger.info(f"Recorded transcription usage: {int(audio_duration)}s for user {recording.user_id}")
                else:
                    current_app.logger.warning(f"Could not determine audio duration for usage tracking")
            except Exception as usage_err:
                # Don't fail transcription if usage tracking fails
                current_app.logger.warning(f"Failed to record transcription usage: {usage_err}")

            # Apply auto speaker labelling if enabled and embeddings available
            if recording.speaker_embeddings:
                try:
                    from src.services.speaker_embedding_matcher import (
                        apply_auto_speaker_labels,
                        apply_speaker_names_to_transcription,
                        update_speaker_profiles_from_recording
                    )

                    user = User.query.get(recording.user_id)
                    if user and user.auto_speaker_labelling:
                        current_app.logger.info(f"Applying auto speaker labelling for recording {recording.id}")
                        speaker_map = apply_auto_speaker_labels(recording, user)

                        if speaker_map:
                            current_app.logger.info(f"Auto-matched speakers: {speaker_map}")
                            # Apply names to transcription
                            if apply_speaker_names_to_transcription(recording, speaker_map):
                                current_app.logger.info(f"Applied speaker names to transcription")
                                # Update speaker profiles with new embeddings
                                updated_count = update_speaker_profiles_from_recording(recording, speaker_map, user)
                                if updated_count > 0:
                                    current_app.logger.info(f"Updated {updated_count} speaker profiles with new embeddings")
                            else:
                                current_app.logger.warning(f"Failed to apply speaker names to transcription for recording {recording.id}")
                        else:
                            current_app.logger.info(f"No speakers matched for auto-labelling")
                except Exception as auto_label_err:
                    # Don't fail transcription if auto-labelling fails
                    current_app.logger.warning(f"Failed to apply auto speaker labelling: {auto_label_err}")

            # Check if auto-summarization is disabled (admin setting or user preference)
            admin_setting = SystemSetting.get_setting('disable_auto_summarization', False)
            admin_disabled = admin_setting if isinstance(admin_setting, bool) else str(admin_setting).lower() == 'true'
            user = User.query.get(recording.user_id)
            user_disabled = user and user.auto_summarization is False
            will_auto_summarize = not admin_disabled and not user_disabled

            # Generate title immediately
            generate_title_task(app_context, recording_id, will_auto_summarize=will_auto_summarize)

            if not will_auto_summarize:
                reason = "admin setting" if admin_disabled else "user preference"
                current_app.logger.info(f"Auto-summarization disabled ({reason}), skipping summary for recording {recording_id}")
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


def transcribe_audio_task(app_context, recording_id, filepath, filename_for_asr, start_time, language=None, min_speakers=None, max_speakers=None, tag_id=None):
    """Runs the transcription and summarization in a background thread.

    Uses the connector-based architecture which supports:
    - OpenAI Whisper (whisper-1)
    - OpenAI GPT-4o Transcribe (gpt-4o-transcribe, gpt-4o-mini-transcribe)
    - OpenAI GPT-4o Transcribe Diarize (gpt-4o-transcribe-diarize)
    - Custom ASR endpoints (whisper-asr-webservice, WhisperX, etc.)

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
    with app_context:
        recording = db.session.get(Recording, recording_id)
        # Determine diarization setting based on connector capabilities
        # The connector will handle this, but we pass the user's preference
        diarize_setting = None  # Let connector decide based on its capabilities

        # Use language from upload form if provided, otherwise use user's default
        # language='' (empty string) means auto-detect, language=None means use default
        if language is not None:
            # Explicit language selection (including empty string for auto-detect)
            user_transcription_language = language if language else None  # '' becomes None for connector
        else:
            # No language specified - use user's default
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


def transcribe_incognito(filepath, original_filename, language=None, min_speakers=None, max_speakers=None, user=None):
    """
    Perform transcription without any database operations.
    Used for Incognito Mode where no data is persisted.

    Args:
        filepath: Path to the audio file
        original_filename: Original filename for logging/processing
        language: Optional language code for transcription
        min_speakers: Optional minimum speakers for diarization
        max_speakers: Optional maximum speakers for diarization
        user: Optional user object for language/diarization preferences

    Returns:
        dict with transcription, title, processing_time, etc.
    """
    import time
    import mimetypes
    from src.services.transcription import get_registry, TranscriptionRequest

    start_time = time.time()
    result = {
        'transcription': None,
        'title': 'Incognito Recording',
        'processing_time_seconds': 0,
        'audio_duration_seconds': None,
        'error': None
    }

    try:
        # Get the active connector
        registry = get_registry()
        connector = registry.get_active_connector()

        if not connector:
            raise Exception("No transcription connector available")

        connector_specs = connector.specifications
        connector_name = type(connector).__name__
        current_app.logger.info(f"[Incognito] Using transcription connector: {connector_name}")

        # Determine mime type
        mime_type = mimetypes.guess_type(original_filename)[0] or 'audio/mpeg'

        # Handle video extraction if needed
        actual_filepath = filepath
        actual_filename = original_filename
        actual_content_type = mime_type

        # Check if file is video and needs audio extraction
        try:
            is_video = is_video_file(filepath, timeout=10)
            if is_video:
                current_app.logger.info(f"[Incognito] Video detected, extracting audio...")
                audio_filepath, audio_mime_type = extract_audio_from_video(filepath, cleanup_original=False)
                actual_filepath = audio_filepath
                actual_content_type = audio_mime_type
                actual_filename = os.path.basename(audio_filepath)
        except FFProbeError as e:
            current_app.logger.warning(f"[Incognito] Failed to probe file: {e}")
            # Check by extension
            video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.flv', '.m4v']
            if any(original_filename.lower().endswith(ext) for ext in video_extensions):
                audio_filepath, audio_mime_type = extract_audio_from_video(filepath, cleanup_original=False)
                actual_filepath = audio_filepath
                actual_content_type = audio_mime_type
                actual_filename = os.path.basename(audio_filepath)

        # Convert audio format if needed
        try:
            needs_chunking_check = (
                chunking_service and
                chunking_service.needs_chunking(actual_filepath, False, connector_specs)
            )

            conversion_result = convert_if_needed(
                filepath=actual_filepath,
                original_filename=actual_filename,
                needs_chunking=needs_chunking_check,
                is_asr_endpoint=False,
                delete_original=False,
                connector_specs=connector_specs
            )

            if conversion_result.was_converted:
                current_app.logger.info(f"[Incognito] Audio converted: {conversion_result.original_codec} -> {conversion_result.final_codec}")
                actual_filepath = conversion_result.output_path
                actual_content_type = conversion_result.mime_type
                actual_filename = os.path.basename(conversion_result.output_path)
        except Exception as e:
            current_app.logger.warning(f"[Incognito] Audio conversion check failed: {e}, proceeding with original")

        # Get audio duration if chunking service is available
        if chunking_service:
            try:
                result['audio_duration_seconds'] = int(chunking_service.get_audio_duration(actual_filepath))
            except Exception as e:
                current_app.logger.warning(f"[Incognito] Could not get audio duration: {e}")

        # Determine diarization settings
        should_diarize = connector.supports_diarization

        # Use user's language preference if not explicitly provided
        if language is None and user:
            language = user.transcription_language

        # Check if chunking is needed
        should_chunk = (chunking_service and
                       chunking_service.needs_chunking(actual_filepath, False, connector_specs))

        current_app.logger.info(f"[Incognito] Starting transcription: diarize={should_diarize}, language={language}, chunking={should_chunk}")

        if should_chunk:
            # Use chunking for large files
            chunk_result = transcribe_chunks_with_connector(
                connector, actual_filepath, actual_filename, actual_content_type, language,
                diarize=should_diarize
            )

            if hasattr(chunk_result, 'segments') and chunk_result.segments and chunk_result.has_diarization():
                result['transcription'] = chunk_result.to_storage_format()
            else:
                result['transcription'] = chunk_result.text if hasattr(chunk_result, 'text') else chunk_result
        else:
            # Single file transcription
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

                response = connector.transcribe(request)

            if response.segments and response.has_diarization():
                result['transcription'] = response.to_storage_format()
            else:
                result['transcription'] = response.text

        result['processing_time_seconds'] = int(time.time() - start_time)
        current_app.logger.info(f"[Incognito] Transcription completed in {result['processing_time_seconds']}s")

        # Generate a title if we have transcription
        if result['transcription'] and len(result['transcription']) > 10:
            result['title'] = _generate_incognito_title(result['transcription'], user)

        return result

    except Exception as e:
        current_app.logger.error(f"[Incognito] Transcription failed: {str(e)}", exc_info=True)
        result['error'] = str(e)
        result['processing_time_seconds'] = int(time.time() - start_time)
        return result


def _generate_incognito_title(transcription_text, user=None):
    """Generate a title for incognito recording without database storage."""
    if not client:
        return "Incognito Recording"

    try:
        # Get formatted text for LLM
        formatted_text = format_transcription_for_llm(transcription_text)
        # Limit text for title generation
        limited_text = formatted_text[:5000]

        # Get user language preference
        user_output_language = user.output_language if user else None
        language_directive = f"Please provide the title in {user_output_language}." if user_output_language else ""

        prompt_text = f"""Create a short title for this conversation:

{limited_text}

Requirements:
- Maximum 8 words
- No phrases like "Discussion about" or "Meeting on"
- Just the main topic

{language_directive}

Title:"""

        system_message_content = "You are an AI assistant that generates concise titles for audio transcriptions. Respond only with the title."
        if user_output_language:
            system_message_content += f" Ensure your response is in {user_output_language}."

        # Use call_llm_completion without user_id tracking for incognito
        completion = client.chat.completions.create(
            model=TEXT_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_message_content},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.7,
            max_tokens=100
        )

        raw_response = completion.choices[0].message.content
        title = clean_llm_response(raw_response) if raw_response else None

        if title and len(title.strip()) > 0:
            return title.strip()

    except Exception as e:
        current_app.logger.warning(f"[Incognito] Title generation failed: {e}")

    return "Incognito Recording"


def generate_incognito_summary(transcription_text, user=None):
    """Generate a summary for incognito recording without database storage."""
    if not client:
        return None

    try:
        # Get formatted text for LLM
        formatted_text = format_transcription_for_llm(transcription_text)

        # Get configurable transcript length limit
        transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
        if transcript_limit == -1:
            transcript_text = formatted_text
        else:
            transcript_text = formatted_text[:transcript_limit]

        # Get user preferences
        user_output_language = user.output_language if user else None
        user_summary_prompt = user.summary_prompt if user else None

        language_directive = f"IMPORTANT: You MUST provide the summary in {user_output_language}." if user_output_language else ""

        # Determine summarization instructions
        if user_summary_prompt:
            summarization_instructions = user_summary_prompt
        else:
            admin_default_prompt = SystemSetting.get_setting('admin_default_summary_prompt', None)
            if admin_default_prompt:
                summarization_instructions = admin_default_prompt
            else:
                summarization_instructions = """Generate a comprehensive summary that includes the following sections:
- **Key Issues Discussed**: A bulleted list of the main topics
- **Key Decisions Made**: A bulleted list of any decisions reached
- **Action Items**: A bulleted list of tasks assigned, including who is responsible if mentioned"""

        # Build messages
        system_message_content = "You are an AI assistant that generates comprehensive summaries for meeting transcripts. Respond only with the summary in Markdown format."
        if user_output_language:
            system_message_content += f" You MUST generate the entire summary in {user_output_language}."

        prompt_text = f"""Transcription:
\"\"\"
{transcript_text}
\"\"\"

Summarization Instructions:
{summarization_instructions}

{language_directive}"""

        current_app.logger.info(f"[Incognito] Generating summary...")

        # Use client directly without user tracking for incognito
        completion = client.chat.completions.create(
            model=TEXT_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_message_content},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.5,
            max_tokens=int(os.environ.get("SUMMARY_MAX_TOKENS", "3000"))
        )

        raw_response = completion.choices[0].message.content
        summary = clean_llm_response(raw_response) if raw_response else None

        if summary:
            current_app.logger.info(f"[Incognito] Summary generated: {len(summary)} characters")
            return summary

    except Exception as e:
        current_app.logger.warning(f"[Incognito] Summary generation failed: {e}")

    return None
