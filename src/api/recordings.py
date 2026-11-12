"""
Recording upload, processing, and management.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import re
from datetime import date, datetime, timedelta
from src.services.llm_settings import get_temperature
from src.services.job_queue import job_queue
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file, Response, current_app, make_response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from urllib.parse import quote

from src.database import db
from src.services.recording_deletion import delete_recording_completely, cleanup_orphaned_speakers_quietly
from src.models import *
from src.utils import *
from src.config.app_config import USE_NEW_TRANSCRIPTION_ARCHITECTURE


# Model validation + the full default-resolution chain now live in one shared
# module so every ingestion path (upload, reprocess, merge, stitch, share,
# auto-process) resolves transcription params identically. Kept as an alias so
# existing references in this file keep working.
from src.services.transcription_defaults import (
    resolve_transcription_model as _resolve_transcription_model,
)
from src.tasks.processing import format_transcription_for_llm, _resolve_timestamp_template_format
from src.utils.dates import to_utc_naive
from src.utils.transcript_render import BASIC_TEMPLATE_FORMAT, parse_segments, render_segments
from src.services.speaker import (
    update_speaker_usage, apply_speaker_map, participants_from_segments, update_voice_profiles,
)

# Incognito mode - disabled by default, enable via environment variable
from src.services.document import (
    absolutize_markdown_links, add_document_header, append_conversation_turn,
    docx_attachment_response, link_base_url, markdown_attachment_response,
    new_document, process_markdown_to_docx,
)
from src.services.llm import client, chat_client, call_chat_completion, process_streaming_with_thinking, TokenBudgetExceeded
from src.services.embeddings import process_recording_chunks
from src.file_exporter import export_recording
from src.utils.mime import served_media_mime, sandbox_media_response
from src.utils.search import LIKE_ESCAPE, contains_pattern
from src.services.storage import get_storage_service
from src.services.recording_ingestion import ingest
from src.services.recording_filters import (
    needs_transcription_condition,
    needs_summary_condition,
    needs_speakers_condition,
)
from src.utils.localization import api_error, format_number, translate

# Create blueprint
recordings_bp = Blueprint('recordings', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
ENABLE_CHUNKING = os.environ.get('ENABLE_CHUNKING', 'true').lower() == 'true'


def reindex_recording_chunks_async(recording_id):
    """Queue a rebuild of a recording's semantic-search (Inquire) chunks.

    The Inquire/RAG chunks are a snapshot of recording.transcription. When the
    transcription text changes (speaker names applied, transcript edited) the
    chunks must be rebuilt or Inquire keeps answering from the stale text (e.g.
    'SPEAKER_00' instead of the applied name). The rebuild is an index job on
    the index workers, as after transcription (#541): it survives a restart, is
    retried, and an edit made while it runs is picked up before it ends. No-op
    when Inquire mode is off.
    """
    if not ENABLE_INQUIRE_MODE:
        return
    try:
        recording = db.session.get(Recording, recording_id)
        if recording is not None:
            job_queue.enqueue(recording.user_id, recording_id, 'index_chunks')
    except Exception as e:
        current_app.logger.error(f"Could not queue the Inquire chunks for recording {recording_id}: {e}", exc_info=True)


# Global helpers (will be injected from app)
has_recording_access = None
editable_recording_ids = None
get_user_recording_status = None
set_user_recording_status = None
enrich_recording_dict_with_user_status = None
bcrypt = None
csrf = None
limiter = None
chunking_service = None

def init_recordings_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, editable_recording_ids, get_user_recording_status, set_user_recording_status, enrich_recording_dict_with_user_status, bcrypt, csrf, limiter, chunking_service
    has_recording_access = kwargs.get('has_recording_access')
    editable_recording_ids = kwargs.get('editable_recording_ids')
    get_user_recording_status = kwargs.get('get_user_recording_status')
    set_user_recording_status = kwargs.get('set_user_recording_status')
    enrich_recording_dict_with_user_status = kwargs.get('enrich_recording_dict_with_user_status')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')
    chunking_service = kwargs.get('chunking_service')


# --- Routes ---

@recordings_bp.route('/recording/<int:recording_id>/download/transcript')
@login_required
def download_transcript_with_template(recording_id):
    """Download transcript with custom template formatting."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        use_translation = request.args.get('translation') in ('1', 'true', 'yes')
        source_transcription = recording.transcription
        if use_translation and recording.transcription_translation:
            source_transcription = recording.transcription_translation

        if not source_transcription:
            return jsonify(api_error('no_transcription_for_recording')), 400

        # Get template ID from query params
        template_id = request.args.get('template_id', type=int)

        # Get the template
        if template_id:
            template = TranscriptTemplate.query.filter_by(
                id=template_id,
                user_id=current_user.id
            ).first()
        else:
            # Use default template
            template = TranscriptTemplate.query.filter_by(
                user_id=current_user.id,
                is_default=True
            ).first()

        # Plain text transcription: returned as-is (no template formatting applies)
        segments = parse_segments(source_transcription)
        is_diarized = segments is not None
        if not is_diarized:
            formatted_transcript = source_transcription
        elif not template:
            # No template found: use a basic format
            formatted_transcript = render_segments(segments, BASIC_TEMPLATE_FORMAT)
        else:
            formatted_transcript = render_segments(segments, template.template, template.header)

        # Output format: 'txt' (default) or 'md'. Markdown wraps the templated
        # body with a title heading so it renders as a proper document, and
        # swaps the extension + content-type. The transcript body itself is the
        # same templated text in both cases (the template lines are valid
        # markdown as-is).
        output_format = (request.args.get('format') or 'txt').lower()
        if output_format not in ('txt', 'md'):
            output_format = 'txt'

        if output_format == 'md':
            title = recording.title or 'Transcript'
            body = f"# {title}\n\n{formatted_transcript}\n"
            response = make_response(body)
            ext = 'md'
            content_type = 'text/markdown; charset=utf-8'
        else:
            response = make_response(formatted_transcript)
            ext = 'txt'
            content_type = 'text/plain; charset=utf-8'

        # Create response
        if is_diarized and template:
            filename = f"{recording.title or 'transcript'}_{template.name}.{ext}"
        elif is_diarized:
            filename = f"{recording.title or 'transcript'}_formatted.{ext}"
        else:
            # Plain text transcription
            filename = f"{recording.title or 'transcript'}.{ext}"
        # Strip only what is illegal in a filename; keep non-ASCII titles intact
        # and advertise them through RFC 5987 filename* (same approach as the
        # summary/chat/notes downloads below).
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename)
        filename = re.sub(r'\s+', ' ', filename).strip() or f'transcript-{recording_id}.{ext}'
        response.headers['Content-Type'] = content_type
        try:
            filename.encode('ascii')
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        except UnicodeEncodeError:
            ascii_fallback = f'transcript-{recording_id}.{ext}'
            response.headers['Content-Disposition'] = (
                f'attachment; filename="{ascii_fallback}"; '
                + "filename*=UTF-8''" + quote(filename, safe='')
            )

        return response

    except Exception as e:
        current_app.logger.error(f"Error downloading transcript: {e}")
        return jsonify(api_error('transcript_download_failed')), 500




@recordings_bp.route('/recording/<int:recording_id>/download/summary')
@login_required
def download_summary_word(recording_id):
    """Download recording summary as a Word document, or as Markdown with ?format=md (#584)."""
    try:
        import re

        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not recording.summary:
            return jsonify(api_error('no_summary')), 400

        if (request.args.get('format') or '').lower() == 'md':
            # The summary is stored as Markdown: it is sent as written, under
            # a title heading, with in-app links made absolute.
            title = recording.title or 'Summary'
            body = f"# {title}\n\n{absolutize_markdown_links(recording.summary.strip(), link_base_url())}\n"
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', f"summary-{recording.title or recording_id}.md")
            filename = re.sub(r'\s+', ' ', filename).strip()
            return markdown_attachment_response(body, filename, f'summary-{recording_id}.md')

        # Title and metadata
        from src.utils.timezones import to_user_local  # owner's local time (#412)
        metadata = [f'Uploaded: {to_user_local(recording.created_at, recording.owner).strftime("%Y-%m-%d %H:%M")}']
        if recording.meeting_date:
            metadata.append(f'Recording Date: {to_user_local(recording.meeting_date, recording.owner).strftime("%Y-%m-%d")}')
        if recording.participants:
            metadata.append(f'Participants: {recording.participants}')
        visible_tags = recording.get_visible_tags(current_user)
        if visible_tags:
            tags_str = ', '.join([tag.name for tag in visible_tags])
            metadata.append(f'Tags: {tags_str}')
        doc = new_document()
        add_document_header(doc, f'Summary: {recording.title or "Untitled Recording"}', metadata)

        # Process markdown content using the helper function
        process_markdown_to_docx(doc, recording.summary, link_base_url())

        # Create safe filename
        safe_title = re.sub(r'[<>:"/\\|?*]', '', recording.title or 'Untitled')
        safe_title = re.sub(r'[-\s]+', '-', safe_title).strip('-')
        filename = f'summary-{safe_title}.docx' if safe_title else f'summary-recording-{recording_id}.docx'

        return docx_attachment_response(doc, filename, f'download-{recording_id}.docx')

    except Exception as e:
        current_app.logger.error(f"Error generating summary Word document: {e}")
        return jsonify(api_error('word_document_failed')), 500



@recordings_bp.route('/recording/<int:recording_id>/download/chat', methods=['POST'])
@login_required
def download_chat_word(recording_id):
    """Download chat conversation as a Word document."""
    try:
        import re

        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Get chat messages from request
        data = request.json
        if not data or 'messages' not in data:
            return jsonify(api_error('no_messages_provided')), 400

        messages = data['messages']
        if not messages:
            return jsonify(api_error('no_messages_to_download')), 400

        # Title and metadata
        from src.utils.timezones import to_user_local, now_local, user_timezone  # owner's local time (#412)
        doc = new_document()
        add_document_header(doc, f'Chat Conversation: {recording.title or "Untitled Recording"}', [
            f'Recording Date: {to_user_local(recording.meeting_date or recording.created_at, recording.owner).strftime("%Y-%m-%d %H:%M")}',
            f'Chat Export Date: {now_local(user_timezone(current_user)).strftime("%Y-%m-%d %H:%M")}',
        ])

        # Add chat messages
        base_url = link_base_url()
        for message in messages:
            append_conversation_turn(
                doc,
                message.get('role', 'unknown'),
                message.get('content', ''),
                message.get('thinking', ''),
                base_url=base_url,
            )

        # Create safe filename; "scope": "response" (one answer and its
        # question) adds -response. Any other value, a non-string included,
        # names the whole chat.
        scope = data.get('scope')
        suffix = '-response' if isinstance(scope, str) and scope == 'response' else ''
        safe_title = re.sub(r'[<>:"/\\|?*]', '', recording.title or 'Untitled')
        safe_title = re.sub(r'[-\s]+', '-', safe_title).strip('-')
        filename = f'chat-{safe_title}{suffix}.docx' if safe_title else f'chat-recording-{recording_id}{suffix}.docx'

        return docx_attachment_response(doc, filename, f'download-{recording_id}{suffix}.docx')

    except Exception as e:
        current_app.logger.error(f"Error generating chat Word document: {e}")
        return jsonify(api_error('word_document_failed')), 500



@recordings_bp.route('/recording/<int:recording_id>/download/notes')
@login_required
def download_notes_word(recording_id):
    """Download recording notes as a Word document."""
    try:
        import re

        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not recording.notes:
            return jsonify(api_error('no_notes')), 400

        # Title and metadata
        from src.utils.timezones import to_user_local  # owner's local time (#412)
        metadata = [f'Uploaded: {to_user_local(recording.created_at, recording.owner).strftime("%Y-%m-%d %H:%M")}']
        if recording.meeting_date:
            metadata.append(f'Recording Date: {to_user_local(recording.meeting_date, recording.owner).strftime("%Y-%m-%d")}')
        if recording.participants:
            metadata.append(f'Participants: {recording.participants}')
        visible_tags = recording.get_visible_tags(current_user)
        if visible_tags:
            tags_str = ', '.join([tag.name for tag in visible_tags])
            metadata.append(f'Tags: {tags_str}')
        doc = new_document()
        add_document_header(doc, f'Notes: {recording.title or "Untitled Recording"}', metadata)

        # Process markdown content using the helper function
        process_markdown_to_docx(doc, recording.notes, link_base_url())

        # Create safe filename
        safe_title = re.sub(r'[<>:"/\\|?*]', '', recording.title or 'Untitled')
        safe_title = re.sub(r'[-\s]+', '-', safe_title).strip('-')
        filename = f'notes-{safe_title}.docx' if safe_title else f'notes-recording-{recording_id}.docx'

        return docx_attachment_response(doc, filename, f'download-{recording_id}.docx')

    except Exception as e:
        current_app.logger.error(f"Error generating notes Word document: {e}")
        return jsonify(api_error('word_document_failed')), 500



@recordings_bp.route('/api/recordings/export-existing', methods=['POST'])
@login_required
def export_existing_recordings():
    """Export the current user's already-processed recordings to disk.

    Auto-export only fires when a recording finishes processing, so anything
    transcribed before the feature was enabled never got written. This endpoint
    lets a user backfill those files to the configured AUTO_EXPORT_DIR on demand.
    It reuses the same writer as auto-export (`export_recording`), so the output
    format, per-user directory, and template resolution are all identical.
    """
    from src.file_exporter import ENABLE_AUTO_EXPORT, export_recording

    if not ENABLE_AUTO_EXPORT:
        return jsonify(api_error('auto_export_disabled')), 400

    try:
        # Candidate recordings: owned by the user and holding exportable content.
        recordings = Recording.query.filter(
            Recording.user_id == current_user.id,
            Recording.status == 'COMPLETED'
        ).filter(
            (Recording.transcription.isnot(None)) | (Recording.summary.isnot(None))
        ).all()

        recording_ids = [r.id for r in recordings]
        total = len(recording_ids)

        exported = 0
        failed = 0
        # export_recording opens its own app context and re-fetches by id, so it
        # is safe to call per-recording from within this request.
        for rid in recording_ids:
            try:
                if export_recording(rid):
                    exported += 1
                else:
                    failed += 1
            except Exception as e:
                current_app.logger.error(f"Bulk export failed for recording {rid}: {e}")
                failed += 1

        return jsonify({
            'exported': exported,
            'failed': failed,
            'total': total,
        })

    except Exception as e:
        current_app.logger.error(f"Error during bulk export: {e}")
        return jsonify(api_error('recordings_export_failed')), 500


@recordings_bp.route('/api/recordings/merge', methods=['POST'])
@login_required
def merge_recordings_endpoint():
    """Merge two or more of the current user's recordings into a new one.

    Body (JSON):
      - recording_ids (list[int], required, ordered): sources to merge, in the
        order they should be concatenated.
      - title (str, optional): title for the merged recording.
      - delete_originals (bool, optional): delete the sources after a successful
        merge (default False).
      - remove_original_audio (bool, optional): keep the sources but remove
        their audio after a successful merge (#413); ignored with
        delete_originals.

    Validation happens in-request; the audio concat runs later on a worker
    (job_type 'merge'), so a long merge never blocks the request. The merged
    recording is created immediately in PROCESSING and queued for the normal
    transcription + summarization pipeline. See src/services/recording_merge.py.
    """
    from src.services.recording_merge import create_merge_recording, MergeError, _UNSET

    data = request.get_json(silent=True) or {}
    raw_ids = data.get('recording_ids')

    if not isinstance(raw_ids, list) or len(raw_ids) < 2:
        return jsonify(api_error('merge_needs_two')), 400

    try:
        recording_ids = [int(rid) for rid in raw_ids]
    except (ValueError, TypeError):
        return jsonify(api_error('invalid_recording_ids')), 400

    title = data.get('title')
    delete_originals = bool(data.get('delete_originals', False))
    remove_original_audio = bool(data.get('remove_original_audio', False)) and not delete_originals

    # notes_source_id: which source's notes to keep on the merged recording.
    #   key absent      -> default to the first source
    #   null            -> keep no notes
    #   <id>            -> that source's notes
    if 'notes_source_id' in data:
        raw_notes_src = data.get('notes_source_id')
        if raw_notes_src is None:
            notes_source_id = None
        else:
            try:
                notes_source_id = int(raw_notes_src)
            except (ValueError, TypeError):
                notes_source_id = _UNSET
    else:
        notes_source_id = _UNSET

    # Storage quota (#413): keeping the originals with their audio adds a copy.
    # The answer names the two ways out the merge dialog offers.
    if not delete_originals and not remove_original_audio:
        from src.services.storage_quota import check_room, StorageQuotaExceeded
        merged_bytes = db.session.query(db.func.coalesce(db.func.sum(Recording.file_size), 0)).filter(
            Recording.id.in_([int(i) for i in recording_ids if str(i).isdigit()]),
            Recording.user_id == current_user.id, Recording.audio_deleted_at.is_(None)).scalar() or 0
        try:
            check_room(current_user, int(merged_bytes))
        except StorageQuotaExceeded as quota_error:
            body, status = quota_error.response()
            data_out = body.get_json()
            data_out['merge_options'] = ['remove_original_audio', 'delete_originals']
            return jsonify(data_out), status

    try:
        recording = create_merge_recording(
            current_user,
            recording_ids,
            title=title,
            delete_originals=delete_originals,
            notes_source_id=notes_source_id,
            remove_original_audio=remove_original_audio,
        )
    except MergeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error merging recordings: {e}")
        return jsonify(api_error('merge_failed')), 500

    return jsonify({
        'success': True,
        'recording_id': recording.id,
        'recording': recording.to_dict(viewer_user=current_user),
    })


@recordings_bp.route('/recording/<int:recording_id>/generate_summary', methods=['POST'])
@login_required
def generate_summary_endpoint(recording_id):
    """Generate summary for a recording that doesn't have one.

    Optional JSON body:
      - custom_prompt (string): user-supplied summarization instructions.
      - prompt_mode ('replace' | 'append', default 'replace'): in 'replace'
        mode the custom prompt overrides the resolved default; in 'append'
        mode it is appended after the resolved default as additional context.
      - additional_instructions (string): added for this run after the prompt
        that runs (#584).
      - prompt_variables (object): replaces the recording's template variables.
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('summary_generate_denied')), 403

        # Check if transcription exists
        if not recording.transcription or len(recording.transcription.strip()) < 10:
            return jsonify(api_error('no_transcription_for_summary')), 400

        # Check if transcription is an error message (not actual content)
        if is_transcription_error(recording.transcription):
            return jsonify(api_error('summary_blocked_transcription_failed')), 400

        # Check if already processing
        if recording.status in ['PROCESSING', 'SUMMARIZING']:
            return jsonify(api_error('recording_already_processing')), 400

        # Check if OpenRouter client is available
        if client is None:
            return jsonify(api_error('summary_openrouter_not_configured')), 503

        # Optional custom prompt + mode (issue / discussion #253)
        data = request.get_json(silent=True) or {}
        from src.services.reprocessing import summary_prompt_params
        custom_prompt, custom_prompt_append = summary_prompt_params(
            data.get('custom_prompt'), data.get('prompt_mode'), data.get('additional_instructions'))
        if isinstance(data.get('prompt_variables'), dict):
            from src.utils.prompt_variables import sanitize_variable_values
            recording.prompt_variables = sanitize_variable_values(data['prompt_variables'])
            db.session.commit()

        current_app.logger.info(
            f"Queueing summary generation for recording {recording_id}"
            + (f" with custom prompt (append={custom_prompt_append}, length={len(custom_prompt)})"
               if custom_prompt else "")
        )

        # Queue summary generation job
        job_params = {'user_id': current_user.id}
        if custom_prompt:
            job_params['custom_prompt'] = custom_prompt
            job_params['custom_prompt_append'] = custom_prompt_append

        job_queue.enqueue(
            user_id=current_user.id,
            recording_id=recording.id,
            job_type='summarize',
            params=job_params,
        )

        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.summaryGenerationQueued')
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error starting summary generation for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500



@recordings_bp.route('/recording/<int:recording_id>/update_speakers', methods=['POST'])
@login_required
def update_speakers(recording_id):
    """Updates speaker labels in a transcription with provided names."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_edit_denied')), 403

        data = request.json
        speaker_map = data.get('speaker_map')
        regenerate_summary = data.get('regenerate_summary', False)

        if speaker_map is None:
            return jsonify(api_error('no_speaker_map')), 400

        # Same rename and follow-up steps as API v1 /speakers/assign (#412).
        from src.services.speaker_assignment import apply_speaker_names
        result = apply_speaker_names(recording, current_user, speaker_map, regenerate_summary)
        summary_queued = result['summary_queued']

        # Return recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.speakersUpdated'),
            'recording': recording_dict,
            'summary_queued': summary_queued
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating speakers for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500



@recordings_bp.route('/recording/<int:recording_id>/update_transcript', methods=['POST'])
@login_required
def update_transcript(recording_id):
    """Updates the complete transcript data including text edits and speaker changes."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_edit_denied')), 403

        data = request.json
        transcript_data = data.get('transcript_data')
        speaker_map = data.get('speaker_map', {})
        regenerate_summary = data.get('regenerate_summary', False)

        if not transcript_data or not isinstance(transcript_data, list):
            return jsonify(api_error('invalid_transcript_data')), 400

        # Apply the names exactly as update_speakers does, including the voice
        # profiles and snippets: this route saves the names whenever the user
        # also staged a line edit in the speaker modal.
        from src.services.voice_profiles import speech_seconds_by_label
        seconds_by_key = speech_seconds_by_label(transcript_data)
        speaker_names_used, label_to_name = apply_speaker_map(transcript_data, speaker_map, current_user)

        # Saved speakers first (update_speaker_usage commits on its own), so
        # the segments link to them in the same commit as the names.
        if speaker_names_used:
            update_speaker_usage(speaker_names_used)
        from src.services.speaker_links import link_list
        link_list(transcript_data, recording)

        # Save the updated transcript
        recording.transcription = json.dumps(transcript_data)
        recording.participants = participants_from_segments(transcript_data)

        update_voice_profiles(recording, label_to_name, current_user, seconds_by_key)

        db.session.commit()

        # The transcript text changed — rebuild Inquire chunks so semantic
        # search reflects the edits (names, corrections). Background + best-effort.
        reindex_recording_chunks_async(recording_id)

        summary_queued = False
        if regenerate_summary:
            current_app.logger.info(f"Queueing summary regeneration for recording {recording_id} after transcript update.")
            job_queue.enqueue(
                user_id=current_user.id,
                recording_id=recording.id,
                job_type='summarize',
                params={'user_id': current_user.id}
            )
            summary_queued = True
            # Export will happen after summary regenerates
        else:
            # Re-export the recording if auto-export is enabled
            export_recording(recording_id)

        # Return recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.transcriptUpdated'),
            'recording': recording_dict,
            'summary_queued': summary_queued
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating transcript for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500



@recordings_bp.route('/recording/<int:recording_id>/auto_identify_speakers', methods=['POST'])
@login_required
def auto_identify_speakers(recording_id):
    """
    Automatically identifies speakers in a transcription using an LLM.
    Strips existing names and re-identifies all speakers from scratch.
    """
    from src.services.speaker_identification import identify_speakers_from_transcript
    from src.services.llm import LLMNotConfiguredError

    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not recording.transcription:
            return jsonify(api_error('no_transcription_for_identification')), 400

        try:
            transcription_data = json.loads(recording.transcription)
        except (json.JSONDecodeError, TypeError):
            return jsonify(api_error('transcription_format_unsupported_for_identification')), 400

        if not isinstance(transcription_data, list):
            return jsonify(api_error('transcription_format_unsupported_for_identification')), 400

        speaker_map = identify_speakers_from_transcript(transcription_data, current_user.id)

        if not speaker_map:
            return jsonify(api_error('no_speakers_in_transcription')), 400

        return jsonify({'success': True, 'speaker_map': speaker_map})

    except LLMNotConfiguredError:
        return jsonify(api_error('text_model_not_configured')), 503
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 503
    except Exception as e:
        current_app.logger.error(f"Error during auto speaker identification for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error_detail', detail=str(e))), 500

# --- Chat with Transcription ---


@recordings_bp.route('/recording/<int:recording_id>/reprocess_transcription', methods=['POST'])
@login_required
def reprocess_transcription(recording_id):
    """Reprocess transcription for a given recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_reprocess_denied')), 403

        # Checks, clearing and settings are shared with every reprocess path (#412).
        from src.services.reprocessing import queue_transcription_reprocess, ReprocessError
        data = request.json or {}
        overrides = {
            'min_speakers': data.get('min_speakers'),
            'max_speakers': data.get('max_speakers'),
            'hotwords': data.get('hotwords'),
            'initial_prompt': data.get('initial_prompt'),
            'transcription_model': data.get('transcription_model'),
        }
        if 'language' in data:
            overrides['language'] = data.get('language')
        try:
            queue_transcription_reprocess(recording, current_user, overrides)
        except ReprocessError as e:
            return jsonify(api_error(e.code)), e.status
        current_app.logger.info(f"Queued transcription reprocessing for recording {recording_id}")

        # Get queue position for response
        queue_position = job_queue.get_position_in_queue(recording.id)
        queue_status = job_queue.get_queue_status()

        # Return recording with per-user status and queue info
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.transcriptionReprocessingQueued'),
            'recording': recording_dict,
            'queue_position': queue_position,
            'queue_status': queue_status
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error reprocessing transcription for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500




@recordings_bp.route('/recording/<int:recording_id>/reprocess_summary', methods=['POST'])
@login_required
def reprocess_summary(recording_id):
    """Reprocess summary for a given recording (requires existing transcription)."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_reprocess_denied')), 403

        # Checks, clearing and job parameters are shared with every reprocess
        # path, including API v1 and the bulk action (#412).
        from src.services.reprocessing import queue_summary_reprocess, ReprocessError
        data = request.get_json() or {}
        try:
            queue_summary_reprocess(recording, current_user,
                                    custom_prompt=data.get('custom_prompt'),
                                    prompt_mode=data.get('prompt_mode'),
                                    prompt_variables=data.get('prompt_variables'),
                                    additional_instructions=data.get('additional_instructions'))
        except ReprocessError as e:
            return jsonify(api_error(e.code)), e.status
        current_app.logger.info(f"Queued summary reprocessing for recording {recording_id}")

        # Refresh recording to get updated status
        db.session.refresh(recording)

        # Return recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.summaryReprocessingStarted'),
            'recording': recording_dict
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error reprocessing summary for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


def _summary_prompt_recording(recording_id):
    """The recording and an error response for the summary prompt routes.

    Only editors can regenerate a summary, so both routes need edit access.
    """
    recording = db.session.get(Recording, recording_id)
    if not recording or not has_recording_access(recording, current_user):
        return None, (jsonify(api_error('recording_not_found')), 404)
    if not has_recording_access(recording, current_user, require_edit=True):
        return None, (jsonify(api_error('recording_reprocess_denied')), 403)
    return recording, None


@recordings_bp.route('/recording/<int:recording_id>/summary_prompt', methods=['GET'])
@login_required
def get_summary_prompt(recording_id):
    """The summary prompt the current user would regenerate with (#584).

    Returns {prompt, source, source_name, source_key, is_owner, save_target,
    start_from}. Only the current user's own prompts are included.
    """
    recording, error = _summary_prompt_recording(recording_id)
    if error:
        return error
    from src.services.summary_prompt import describe_summary_prompt
    return jsonify(describe_summary_prompt(recording, current_user))


@recordings_bp.route('/recording/<int:recording_id>/summary_prompt', methods=['POST'])
@login_required
def save_summary_prompt_route(recording_id):
    """Save an edited summary prompt to a tag, the folder or the user's default (#584).

    JSON body: {target: 'tag' | 'folder' | 'user', id, prompt}. Returns the
    prompt description as the GET route does.
    """
    recording, error = _summary_prompt_recording(recording_id)
    if error:
        return error
    from src.services.summary_prompt import SummaryPromptError, describe_summary_prompt, save_summary_prompt
    data = request.get_json(silent=True) or {}
    try:
        save_summary_prompt(recording, current_user, data.get('target'), data.get('id'), data.get('prompt'))
    except SummaryPromptError as e:
        db.session.rollback()
        return jsonify(api_error(e.code)), e.status
    return jsonify(describe_summary_prompt(recording, current_user))



@recordings_bp.route('/recording/<int:recording_id>/regenerate_title', methods=['POST'])
@login_required
def regenerate_title(recording_id):
    """Regenerate the AI title for a recording based on its existing transcription."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_edit_denied')), 403

        # Same title step as processing (naming template, AI title, fallbacks), so
        # regenerating gives the title the pipeline would give (#412). The checks
        # below apply only when the title needs an LLM call.
        from src.services.titling import compute_title, needs_ai_title
        if needs_ai_title(recording):
            if not recording.transcription or len(recording.transcription.strip()) < 10:
                return jsonify(api_error('no_transcription_for_title')), 400

            if is_transcription_error(recording.transcription):
                return jsonify(api_error('title_blocked_transcription_failed')), 400

            if client is None:
                return jsonify(api_error('title_openrouter_not_configured')), 503

        new_title = compute_title(recording, raise_budget_errors=True)
        if not new_title:
            return jsonify(api_error('title_generation_failed')), 500

        recording.title = new_title
        recording.title_source = 'auto'
        db.session.commit()
        # Keep the exported file in step with the new title, as /save does.
        try:
            export_recording(recording.id)
        except Exception as export_error:
            current_app.logger.warning(f"Export after title regeneration failed for {recording.id}: {export_error}")

        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'title': new_title,
            'recording': recording_dict
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating title for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/recording/<int:recording_id>/reset_status', methods=['POST'])
@login_required
def reset_status(recording_id):
    """Resets the status of a stuck or failed recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_modify_denied')), 403

        # Allow resetting if it's stuck or failed. QUEUED is included for a
        # recording whose job never ran: reprocess refuses it as "already
        # being processed", so this is its only way out.
        if recording.status in ['PENDING', 'QUEUED', 'PROCESSING', 'SUMMARIZING', 'FAILED']:
            recording.status = 'FAILED'
            recording.error_message = "Manually reset from stuck or failed state."
            # A job still waiting in the queue would claim the recording
            # later and overwrite the reset. A running job cannot be stopped
            # from here; it finishes on its own and sets a final status.
            ProcessingJob.query.filter_by(recording_id=recording.id, status='queued').update(
                {'status': 'failed', 'error_message': 'Cancelled by manual reset.'},
                synchronize_session=False)
            db.session.commit()
            current_app.logger.info(f"Manually reset status for recording {recording_id} to FAILED.")

            # Return recording with per-user status
            recording_dict = recording.to_dict(viewer_user=current_user)
            enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
            return jsonify({'success': True, 'message': translate('server.apiMessage.recordingStatusReset'), 'recording': recording_dict})
        else:
            return jsonify(api_error('recording_not_resettable', status=recording.status)), 400

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error resetting status for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

# --- Authentication Routes ---


def _text_model_configured():
    from src.services.llm import TEXT_MODEL_CONFIGURED
    return TEXT_MODEL_CONFIGURED


@recordings_bp.route('/')
@recordings_bp.route('/recordings/<int:recording_id>')
@recordings_bp.route('/label/<label_name>')
@login_required
def index(recording_id=None, label_name=None):
    # recording_id is a deep link (#301): the server just serves the SPA shell
    # here; the frontend reads it from the path and pre-selects the recording.
    # label_name works the same way for /label/<name>: the shell is served and
    # the frontend resolves the name against the viewer's own accessible tags
    # and applies it as the tag filter, so one link points at a group of
    # recordings. Neither value is used server-side.
    # Check if user is a group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    # Pass the ASR config, inquire mode config, and user language preference to the template
    user_language = current_user.ui_language if current_user.is_authenticated and current_user.ui_language else 'en'

    # Default target language for the translation modal: the account's
    # preferred output language, falling back to the interface language
    # (translation spec). The endpoint applies the same fallback when no
    # target is sent, so the modal simply shows what will be used.
    default_translation_language = ''
    if current_user.is_authenticated:
        if getattr(current_user, 'output_language', None):
            default_translation_language = _canonical_translation_language(current_user.output_language) or ''
        if not default_translation_language:
            from src.utils.language import output_language_name_for_code
            default_translation_language = output_language_name_for_code(user_language) or ''

    # The "Audio removed" filter matters when retention removes audio; users
    # who can delete recordings can also remove audio by hand.
    enable_audio_removed_filter = (_self.ENABLE_AUTO_DELETION and _self.DELETION_MODE == 'audio_only') or _self.USERS_CAN_DELETE or current_user.is_admin

    # Get connector capabilities (new architecture)
    # Defaults to USE_ASR_ENDPOINT for backwards compatibility
    connector_supports_diarization = USE_ASR_ENDPOINT
    connector_supports_speaker_count = USE_ASR_ENDPOINT  # ASR endpoint supports min/max speakers
    connector_supports_exact_speaker_count = False  # Exact-N connectors (openasr) declare it (#362)
    connector_supports_hotwords = USE_ASR_ENDPOINT
    connector_supports_initial_prompt = USE_ASR_ENDPOINT
    if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
        try:
            from src.services.transcription import get_registry
            registry = get_registry()
            connector = registry.get_active_connector()
            if connector:
                connector_supports_diarization = connector.supports_diarization
                connector_supports_speaker_count = connector.supports_speaker_count_control
                connector_supports_exact_speaker_count = connector.supports_exact_speaker_count
                connector_supports_hotwords = connector.supports_hotwords
                connector_supports_initial_prompt = connector.supports_initial_prompt
        except Exception as e:
            current_app.logger.warning(f"Could not get connector capabilities: {e}")

    # Phase B of #287 (c)(d): server-side chunk streaming for in-app
    # recordings. Off by default; enabling the env var flips the
    # data-server-recording-chunks attribute and the audio composable
    # opens an /upload/session for each in-app recording instead of
    # accumulating the whole blob in RAM.
    server_recording_chunks_enabled = os.environ.get(
        'ENABLE_SERVER_RECORDING_CHUNKS', 'false'
    ).lower() == 'true'

    # Absolute hours ceiling on in-app recordings (Phase C of #287 c/d).
    # Replaces the legacy hard-stop-on-size behaviour for users with
    # server-side streaming enabled; size-based stop still applies as a
    # fallback when streaming is off.
    try:
        recording_max_hours = float(os.environ.get('RECORDING_MAX_HOURS', '8'))
    except (TypeError, ValueError):
        recording_max_hours = 8.0

    # MediaRecorder timeslice (seconds) for in-app recordings: how often a
    # chunk is emitted and streamed to the server. Smaller = finer crash
    # recovery but more requests / files / DB commits; larger = less server
    # load. Default 5s. At scale (many concurrent recorders on SQLite) raise
    # this and/or RECORDING_SESSION_COMMIT_BATCH_SIZE. Clamped to [1, 60].
    try:
        recording_chunk_seconds = int(float(os.environ.get('RECORDING_CHUNK_SECONDS', '5')))
    except (TypeError, ValueError):
        recording_chunk_seconds = 5
    recording_chunk_seconds = max(1, min(60, recording_chunk_seconds))

    # Video bitrate cap (kbps) for the opt-in tab/window/screen video capture
    # (#303). Screen content compresses well, so the 2500 kbps default keeps
    # an hour of capture near 1 GB. Clamped to [100, 50000].
    try:
        recording_video_kbps = int(float(os.environ.get('RECORDING_VIDEO_KBPS', '2500')))
    except (TypeError, ValueError):
        recording_video_kbps = 2500
    recording_video_kbps = max(100, min(50000, recording_video_kbps))

    return render_template('index.html',
                         use_asr_endpoint=USE_ASR_ENDPOINT,  # Backwards compat
                         connector_supports_diarization=connector_supports_diarization,
                         connector_supports_speaker_count=connector_supports_speaker_count,
                         connector_supports_exact_speaker_count=connector_supports_exact_speaker_count,
                         connector_supports_hotwords=connector_supports_hotwords,
                         connector_supports_initial_prompt=connector_supports_initial_prompt,
                         inquire_mode_enabled=ENABLE_INQUIRE_MODE,
                         enable_audio_removed_filter=enable_audio_removed_filter,
                         enable_internal_sharing=_self.ENABLE_INTERNAL_SHARING,
                         user_language=user_language,
                         default_translation_language=default_translation_language,
                         is_team_admin=is_team_admin,
                         server_recording_chunks_enabled=server_recording_chunks_enabled,
                         recording_max_hours=recording_max_hours,
                         recording_chunk_seconds=recording_chunk_seconds,
                         recording_video_kbps=recording_video_kbps,
                         text_model_configured=_text_model_configured())



def get_accessible_recording_ids(user_id):
    """
    Get all recording IDs that a user has access to.

    Includes:
    - Recordings owned by the user
    - Recordings shared with the user via InternalShare
    - Recordings shared via group tags (if team membership exists)

    Args:
        user_id (int): User ID to check access for

    Returns:
        list: List of recording IDs the user can access
    """
    accessible_ids = set()

    # 1. User's own recordings
    own_recordings = db.session.query(Recording.id).filter_by(user_id=user_id).all()
    accessible_ids.update([r.id for r in own_recordings])

    # 2. Internally shared recordings
    if _self.ENABLE_INTERNAL_SHARING:
        shared_recordings = db.session.query(InternalShare.recording_id).filter_by(
            shared_with_user_id=user_id
        ).all()
        accessible_ids.update([r.recording_id for r in shared_recordings])

    return list(accessible_ids)


@recordings_bp.route('/recordings', methods=['GET'])
def get_recordings():
    """Get all recordings for the current user (simple list)."""
    try:
        # Check if user is logged in
        if not current_user.is_authenticated:
            return jsonify([])  # Return empty array if not logged in

        # Filter recordings by the current user
        from src.services.recording_list import RecordingListBatch, full_load_options
        stmt = select(Recording).where(Recording.user_id == current_user.id).order_by(Recording.created_at.desc())
        recordings = db.session.execute(stmt.options(*full_load_options())).scalars().all()
        # Pre-batch the duplicate-info groupings so we don't run a
        # per-row query inside each recording's to_dict() call.
        dup_map = Recording.get_duplicate_info_map(current_user.id)
        batch = RecordingListBatch(recordings, current_user, duplicates=False)
        return jsonify([
            recording.to_dict(viewer_user=current_user, duplicate_info_map=dup_map, batch=batch)
            for recording in recordings
        ])
    except Exception as e:
        current_app.logger.error(f"Error fetching recordings: {e}")
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/api/recordings', methods=['GET'])
@login_required
def get_recordings_paginated():
    """Get recordings with pagination and server-side filtering (includes shared recordings)."""
    import re
    try:
        # Parse query parameters
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 25, type=int), 100)  # Cap at 100 per page
        search_query = request.args.get('q', '').strip()
        # 'archived' is the user's own archive (#394); 'audio_removed' is the
        # state left when the media file was deleted and the transcript kept.
        show_archived = request.args.get('archived', '').lower() == 'true'
        show_audio_removed = request.args.get('audio_removed', '').lower() == 'true'
        show_shared = request.args.get('shared', '').lower() == 'true'
        show_starred = request.args.get('starred', '').lower() == 'true'
        show_inbox = request.args.get('inbox', '').lower() == 'true'
        needs_transcription = request.args.get('needs_transcription', '').lower() == 'true'
        needs_summary = request.args.get('needs_summary', '').lower() == 'true'
        needs_speakers = request.args.get('needs_speakers', '').lower() == 'true'
        sort_by = request.args.get('sort_by', 'created_at')  # 'created_at' or 'meeting_date'
        folder_filter = request.args.get('folder', '').strip()  # folder_id or 'none' for no folder
        status_filter = request.args.get('status', '').strip().upper()  # e.g. 'COMPLETED' (used by the merge picker)

        # Get all accessible recording IDs (own + shared)
        accessible_recording_ids = get_accessible_recording_ids(current_user.id)

        if not accessible_recording_ids:
            return jsonify({
                'recordings': [],
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total': 0,
                    'total_pages': 0,
                    'has_next': False,
                    'has_prev': False
                }
            })

        # Build base query to include accessible recordings
        stmt = select(Recording).where(Recording.id.in_(accessible_recording_ids))

        # Apply status filter (AND with other filters). Used by the merge picker
        # to request only mergeable (COMPLETED) recordings server-side.
        if status_filter:
            stmt = stmt.where(Recording.status == status_filter)

        # Audio removed filter (AND with other filters)
        if show_audio_removed:
            stmt = stmt.where(Recording.audio_deleted_at.is_not(None))

        # Archive: the archived view shows only archived recordings; the main
        # list hides them, except in search results, so archiving never makes
        # a recording unfindable.
        from src.services.recording_state import archived_condition
        if show_archived:
            stmt = stmt.where(archived_condition(Recording, current_user.id))
        elif not search_query:
            stmt = stmt.where(db.not_(archived_condition(Recording, current_user.id)))

        # Apply shared filter (AND with other filters)
        if show_shared:
            # Only show recordings shared with current user (not owned by them)
            stmt = stmt.where(Recording.user_id != current_user.id)

        # Apply starred filter (AND with other filters)
        # For starred/inbox we need to consider both owned recordings and shared recordings
        if show_starred:
            from src.models.sharing import SharedRecordingState
            # For owned recordings: check Recording.is_highlighted
            # For shared recordings: check SharedRecordingState.is_highlighted
            starred_subq = select(SharedRecordingState.recording_id).where(
                db.and_(
                    SharedRecordingState.user_id == current_user.id,
                    SharedRecordingState.is_highlighted == True
                )
            ).scalar_subquery()
            stmt = stmt.where(
                db.or_(
                    db.and_(Recording.user_id == current_user.id, Recording.is_highlighted == True),
                    Recording.id.in_(starred_subq)
                )
            )

        # Apply inbox filter (AND with other filters)
        if show_inbox:
            from src.models.sharing import SharedRecordingState
            # For owned recordings: check Recording.is_inbox
            # For shared recordings: check SharedRecordingState.is_inbox
            inbox_subq = select(SharedRecordingState.recording_id).where(
                db.and_(
                    SharedRecordingState.user_id == current_user.id,
                    SharedRecordingState.is_inbox == True
                )
            ).scalar_subquery()
            stmt = stmt.where(
                db.or_(
                    db.and_(Recording.user_id == current_user.id, Recording.is_inbox == True),
                    Recording.id.in_(inbox_subq)
                )
            )

        # Apply folder filter (AND with other filters)
        if folder_filter:
            if folder_filter.lower() == 'none':
                # Filter recordings with no folder
                stmt = stmt.where(Recording.folder_id.is_(None))
            elif folder_filter.isdecimal():
                folder_id = int(folder_filter)
                # include_children=true adds the folder's descendants (#585).
                if request.args.get('include_children', '').lower() == 'true':
                    from src.services.folder_tree import folder_filter_ids
                    stmt = stmt.where(Recording.folder_id.in_(folder_filter_ids(folder_id)))
                else:
                    stmt = stmt.where(Recording.folder_id == folder_id)
            else:
                # A bad value is refused, not ignored (#514).
                return jsonify({'error': translate('server.apiError.invalid_folder_id'),
                                'code': 'invalid_parameter'}), 400

        if needs_transcription:
            stmt = stmt.where(needs_transcription_condition())

        if needs_summary:
            stmt = stmt.where(needs_summary_condition())

        if needs_speakers:
            stmt = stmt.where(needs_speakers_condition())

        # Apply search filters if provided
        if search_query:
            # Extract date filters
            date_filters = re.findall(r'date:(\S+)', search_query.lower())
            date_from_filters = re.findall(r'date_from:(\S+)', search_query.lower())
            date_to_filters = re.findall(r'date_to:(\S+)', search_query.lower())
            tag_filters = re.findall(r'tag:(\S+)', search_query.lower())
            speaker_filters = re.findall(r'speaker:(\S+)', search_query.lower())

            # Remove special syntax to get text search
            text_query = re.sub(r'date:\S+', '', search_query, flags=re.IGNORECASE)
            text_query = re.sub(r'date_from:\S+', '', text_query, flags=re.IGNORECASE)
            text_query = re.sub(r'date_to:\S+', '', text_query, flags=re.IGNORECASE)
            text_query = re.sub(r'tag:\S+', '', text_query, flags=re.IGNORECASE)
            text_query = re.sub(r'speaker:\S+', '', text_query, flags=re.IGNORECASE).strip()

            # Apply date filters (#412). Each filter is a range of local calendar
            # days in the viewer's timezone (sent by the browser, else the account
            # zone), converted to UTC because meeting_date / created_at are stored
            # as naive UTC. Ranges are half-open [start, end): the last day is
            # included up to midnight (date_to and lastmonth dropped it before).
            from src.utils.timezones import local_day_start_utc, now_local, request_timezone
            _zone = request_timezone(request.args.get('tz'), current_user)
            _today = now_local(_zone).date()

            def _days(first, after_last=None):
                start_utc = local_day_start_utc(first, _zone)
                end_utc = local_day_start_utc(after_last, _zone) if after_last else None
                meeting = [Recording.meeting_date >= start_utc]
                created = [Recording.created_at >= start_utc]
                if end_utc is not None:
                    meeting.append(Recording.meeting_date < end_utc)
                    created.append(Recording.created_at < end_utc)
                return db.or_(db.and_(*meeting),
                              db.and_(Recording.meeting_date.is_(None), *created))

            def _month_start(year, month):
                return date(year, month, 1)

            def _next_month(d):
                return date(d.year + (d.month == 12), d.month % 12 + 1, 1)

            for date_filter in date_filters:
                if date_filter == 'today':
                    stmt = stmt.where(_days(_today, _today + timedelta(days=1)))
                elif date_filter == 'yesterday':
                    stmt = stmt.where(_days(_today - timedelta(days=1), _today))
                elif date_filter == 'thisweek':
                    stmt = stmt.where(_days(_today - timedelta(days=_today.weekday())))
                elif date_filter == 'lastweek':
                    this_week = _today - timedelta(days=_today.weekday())
                    stmt = stmt.where(_days(this_week - timedelta(days=7), this_week))
                elif date_filter == 'thismonth':
                    stmt = stmt.where(_days(_today.replace(day=1)))
                elif date_filter == 'lastmonth':
                    this_month = _today.replace(day=1)
                    last_month = (this_month - timedelta(days=1)).replace(day=1)
                    stmt = stmt.where(_days(last_month, this_month))
                elif re.match(r'^\d{4}-\d{2}-\d{2}$', date_filter):
                    try:
                        target = datetime.strptime(date_filter, '%Y-%m-%d').date()
                        stmt = stmt.where(_days(target, target + timedelta(days=1)))
                    except ValueError:
                        pass
                elif re.match(r'^\d{4}-\d{2}$', date_filter):
                    year, month = map(int, date_filter.split('-'))
                    if 1 <= month <= 12:
                        first = _month_start(year, month)
                        stmt = stmt.where(_days(first, _next_month(first)))
                elif re.match(r'^\d{4}$', date_filter):
                    year = int(date_filter)
                    stmt = stmt.where(_days(date(year, 1, 1), date(year + 1, 1, 1)))

            # Apply date range filters (date_to includes that whole day)
            if date_from_filters and date_from_filters[0]:
                try:
                    date_from = datetime.strptime(date_from_filters[0], '%Y-%m-%d').date()
                    stmt = stmt.where(_days(date_from))
                except ValueError:
                    pass  # Invalid date format, ignore

            if date_to_filters and date_to_filters[0]:
                try:
                    date_to = datetime.strptime(date_to_filters[0], '%Y-%m-%d').date()
                    end_utc = local_day_start_utc(date_to + timedelta(days=1), _zone)
                    stmt = stmt.where(db.or_(
                        Recording.meeting_date < end_utc,
                        db.and_(Recording.meeting_date.is_(None), Recording.created_at < end_utc)))
                except ValueError:
                    pass  # Invalid date format, ignore

            # Apply tag filters
            if tag_filters:
                # Join with tags table and filter by tag names
                tag_conditions = []
                for tag_filter in tag_filters:
                    # Replace underscores back to spaces for matching
                    tag_name = tag_filter.replace('_', ' ')
                    # Exact, case-insensitive: the filter is built from a tag
                    # the user picked (sidebar chip, or a /label/<name> link),
                    # so 'foo' must not also drag in 'foobar'. lower() == is
                    # used over ilike so '%' and '_' in a tag name stay literal.
                    tag_conditions.append(
                        db.func.lower(Tag.name) == tag_name.lower()
                    )

                stmt = stmt.join(RecordingTag).join(Tag).where(db.or_(*tag_conditions))

            # Apply speaker filters
            if speaker_filters:
                speaker_conditions = []
                for speaker_filter in speaker_filters:
                    # Replace underscores back to spaces for matching
                    speaker_name = speaker_filter.replace('_', ' ')
                    speaker_conditions.append(Recording.participants.ilike(
                        contains_pattern(speaker_name), escape=LIKE_ESCAPE))
                stmt = stmt.where(db.or_(*speaker_conditions))

            # Apply text search
            if text_query:
                from src.models.sharing import SharedRecordingState

                # Search in user-specific notes:
                # - For owned recordings: search Recording.notes
                # - For shared recordings: search SharedRecordingState.personal_notes

                # Every character typed is matched literally (#485)
                pattern = contains_pattern(text_query)
                text_conditions = [
                    Recording.title.ilike(pattern, escape=LIKE_ESCAPE),
                    Recording.participants.ilike(pattern, escape=LIKE_ESCAPE),
                    Recording.transcription.ilike(pattern, escape=LIKE_ESCAPE),
                    # Search owner's notes for owned recordings
                    db.and_(
                        Recording.user_id == current_user.id,
                        Recording.notes.ilike(pattern, escape=LIKE_ESCAPE)
                    )
                ]

                # Add search for personal notes in shared recordings
                # Use a subquery to check if personal_notes match
                shared_notes_subq = select(SharedRecordingState.recording_id).where(
                    db.and_(
                        SharedRecordingState.user_id == current_user.id,
                        SharedRecordingState.personal_notes.ilike(pattern, escape=LIKE_ESCAPE)
                    )
                ).scalar_subquery()

                text_conditions.append(Recording.id.in_(shared_notes_subq))

                stmt = stmt.where(db.or_(*text_conditions))

        # Apply ordering based on sort_by parameter
        if sort_by == 'meeting_date':
            # Sort by meeting_date first, fall back to created_at if no meeting_date
            stmt = stmt.order_by(
                db.case(
                    (Recording.meeting_date.is_not(None), Recording.meeting_date),
                    else_=db.func.date(Recording.created_at)
                ).desc(),
                Recording.created_at.desc()
            )
        else:
            # Default: sort by created_at (upload/processing date)
            stmt = stmt.order_by(Recording.created_at.desc())

        # Get total count for pagination info; the order does not change the
        # count, so it is left out of the counting query (D#441)
        count_stmt = select(db.func.count()).select_from(stmt.order_by(None).subquery())
        total_count = db.session.execute(count_stmt).scalar()

        # Apply pagination
        offset = (page - 1) * per_page
        stmt = stmt.offset(offset).limit(per_page)

        # Execute query. Tags, folders and their names load for the whole
        # page, and the long text columns the list never shows stay behind (D#483).
        from src.services.recording_list import RecordingListBatch, list_load_options
        recordings = db.session.execute(stmt.options(*list_load_options())).scalars().all()

        # Batch-load this viewer's shares for the page so serialization can resolve
        # the #314 share-reason tag/folder without an N+1 query.
        page_rec_ids = [r.id for r in recordings]
        share_map = {}
        if page_rec_ids:
            for s in InternalShare.query.filter(
                InternalShare.recording_id.in_(page_rec_ids),
                InternalShare.shared_with_user_id == current_user.id
            ).all():
                share_map[s.recording_id] = s

        # What the rows need beyond their own columns, read once for the page
        # rather than per row (D#483)
        batch = RecordingListBatch(recordings, current_user, share_map)
        editable_ids = editable_recording_ids(recordings, current_user, share_map)

        # Enrich recordings with sharing metadata
        enriched_recordings = []
        for recording in recordings:
            rec_dict = recording.to_list_dict(viewer_user=current_user, share_map=share_map, batch=batch)

            # Add sharing metadata
            is_owner = recording.user_id == current_user.id
            rec_dict['is_owner'] = is_owner

            # Get per-user status (owner uses Recording fields, recipients use SharedRecordingState)
            user_inbox, user_highlighted = batch.user_status(recording)
            rec_dict['is_inbox'] = user_inbox
            rec_dict['is_highlighted'] = user_highlighted
            rec_dict['is_archived'] = batch.user_archived(recording)

            # Add edit permission info (has_recording_access rules, group admins included)
            rec_dict['can_edit'] = recording.id in editable_ids

            # Add delete permission info (only owner can delete)
            rec_dict['can_delete'] = is_owner and (_self.USERS_CAN_DELETE or current_user.is_admin)

            if not is_owner:
                # This is a shared recording - get owner info and share permissions
                owner = batch.owners.get(recording.user_id)
                rec_dict['owner_username'] = owner.username if owner else "Unknown"
                rec_dict['is_shared'] = True
                # Don't show outgoing share count for recordings you don't own
                rec_dict['shared_with_count'] = 0
                rec_dict['public_share_count'] = 0

                # Get share permissions (reuse the batched map)
                share = share_map.get(recording.id)

                if share:
                    rec_dict['share_info'] = {
                        'share_id': share.id,
                        'owner_username': owner.username if owner else "Unknown",
                        'can_edit': share.can_edit,
                        'can_reshare': share.can_reshare,
                        'shared_at': share.created_at.isoformat()
                    }
                else:
                    # Fallback if share record not found (shouldn't happen)
                    rec_dict['share_info'] = {
                        'can_edit': False,
                        'can_reshare': False
                    }
            else:
                rec_dict['is_shared'] = False

            # Check if recording has group tags (among visible tags)
            has_group_tags = any(tag.is_group_tag for tag in batch.visible_tags(recording))
            rec_dict['has_group_tags'] = has_group_tags

            enriched_recordings.append(rec_dict)

        # Calculate pagination metadata
        total_pages = (total_count + per_page - 1) // per_page
        has_next = page < total_pages
        has_prev = page > 1

        return jsonify({
            'recordings': enriched_recordings,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_count,
                'total_pages': total_pages,
                'has_next': has_next,
                'has_prev': has_prev
            }
        })

    except Exception as e:
        current_app.logger.error(f"Error fetching paginated recordings: {e}")
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/save', methods=['POST'])
@login_required
def save_metadata():
    """Save recording metadata (title, participants, notes, summary, etc.)."""
    try:
        data = request.json
        if not data:
            return jsonify(api_error('no_data_provided')), 400
        recording_id = data.get('id')
        if not recording_id:
            return jsonify(api_error('no_recording_id')), 400

        recording = db.session.get(Recording, recording_id)
        # Check if user has at least view access
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Handle notes separately - no edit permission required (user-specific)
        if 'notes' in data:
            if recording.user_id == current_user.id:
                # Owner saves to Recording.notes
                # Stored as written: notes and summaries are Markdown, and the
                # HTML cleaner would turn "> quote" into "&gt; quote". They are
                # cleaned when converted to HTML (md_to_html), the only form ever
                # shown as HTML.
                recording.notes = data['notes']
            else:
                # Shared user saves to personal_notes (requires SharedRecordingState)
                from src.models.sharing import SharedRecordingState
                state = SharedRecordingState.query.filter_by(
                    recording_id=recording.id,
                    user_id=current_user.id
                ).first()

                if not state:
                    # Create SharedRecordingState if it doesn't exist
                    state = SharedRecordingState(
                        recording_id=recording.id,
                        user_id=current_user.id,
                        is_inbox=True,
                        is_highlighted=False
                    )
                    db.session.add(state)

                state.personal_notes = data['notes']

        # Determine if any fields requiring edit permission are being updated
        edit_fields = ['title', 'participants', 'summary', 'meeting_date']
        requires_edit = any(field in data for field in edit_fields)

        # If edit fields are present, check for edit permission
        if requires_edit and not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_edit_denied')), 403

        # Update fields requiring edit permission
        if requires_edit:
            if 'title' in data:
                if data['title'] != recording.title:
                    recording.title_source = 'user'
                recording.title = data['title']
            if 'participants' in data:
                recording.participants = data['participants']
            if 'summary' in data:
                recording.summary = data['summary']
            if 'meeting_date' in data:
                try:
                    date_str = data['meeting_date']
                    if date_str:
                        # Try to parse as full ISO datetime first. meeting_date is
                        # stored as naive UTC (like created_at), so convert any
                        # zone-aware input to UTC before storing.
                        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str.strip()):
                            # Date only: keep the existing time of day, else noon UTC
                            # so the calendar day reads the same in every timezone.
                            # (Python 3.11 parses a bare date as midnight UTC, which
                            # showed as the previous day west of UTC, #412 B5.)
                            parsed_date = datetime.strptime(date_str.strip(), '%Y-%m-%d')
                            existing_time = recording.meeting_date.time() if recording.meeting_date else datetime.min.time().replace(hour=12)
                            recording.meeting_date = datetime.combine(parsed_date.date(), existing_time)
                        else:
                            recording.meeting_date = to_utc_naive(datetime.fromisoformat(date_str.replace('Z', '+00:00')))
                    else:
                        recording.meeting_date = None
                except (ValueError, TypeError) as e:
                    current_app.logger.warning(f"Could not parse meeting_date '{data.get('meeting_date')}': {e}")

        # Handle per-user status fields (only requires view permission)
        if 'is_inbox' in data or 'is_highlighted' in data:
            set_user_recording_status(
                recording,
                current_user,
                is_inbox=data.get('is_inbox'),
                is_highlighted=data.get('is_highlighted')
            )
        if 'is_archived' in data:
            from src.services.recording_state import set_user_archived
            set_user_archived(recording, current_user, data.get('is_archived'))

        db.session.commit()

        # Re-export the recording if auto-export is enabled and editable fields were changed
        if requires_edit:
            export_recording(recording_id)

        # Return recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({'success': True, 'recording': recording_dict})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving metadata for recording {data.get('id')}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error_saving')), 500


@recordings_bp.route('/recording/<int:recording_id>/update_transcription', methods=['POST'])
@login_required
def update_transcription(recording_id):
    """Updates the transcription content for a recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_edit_denied')), 403

        data = request.json or {}
        new_transcription = data.get('transcription')
        new_translation = data.get('transcription_translation')

        if new_transcription is None and new_translation is None:
            return jsonify(api_error('no_transcription_data')), 400

        # The incoming data could be a JSON string (from ASR edit) or plain text
        if new_transcription is not None:
            recording.transcription = new_transcription
        if new_translation is not None:
            recording.transcription_translation = new_translation
            # Keep transcription_translation_language unchanged.

        # Optional: If the transcription changes, we might want to indicate that the summary is outdated.
        # For now, we'll just save the transcript. A "regenerate summary" button could be a good follow-up.

        db.session.commit()
        current_app.logger.info(f"Transcription for recording {recording_id} was updated.")

        # Re-export the recording if auto-export is enabled
        export_recording(recording_id)

        # Return recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({'success': True, 'message': translate('server.apiMessage.transcriptionUpdated'), 'recording': recording_dict})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating transcription for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error_updating_transcription')), 500


from src.utils.language import canonical_translation_language as _canonical_translation_language


@recordings_bp.route('/recording/<int:recording_id>/translate_transcription', methods=['POST'])
@login_required
def translate_transcription(recording_id):
    """Queue a translation of a recording's transcription."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_edit_denied')), 403

        if not recording.transcription or len(recording.transcription.strip()) < 10:
            return jsonify(api_error('translation_no_transcription')), 400

        from src.utils.error_formatting import is_transcription_error
        if is_transcription_error(recording.transcription):
            return jsonify(api_error('translation_blocked_transcription_failed')), 400

        from src.services.llm import TRANSLATION_MODEL_CONFIGURED
        if not TRANSLATION_MODEL_CONFIGURED:
            return jsonify(api_error('translation_llm_not_configured')), 503

        data = request.get_json(silent=True) or {}
        raw_target = data.get('target_language')

        if raw_target is None or str(raw_target).strip() == '':
            # Resolve to the user's output language, then interface language.
            target_language = None
            if getattr(current_user, 'output_language', None):
                target_language = _canonical_translation_language(current_user.output_language)
            if not target_language:
                try:
                    from src.utils.language import output_language_name_for_code

                    target_language = output_language_name_for_code(getattr(current_user, 'ui_language', None))
                except Exception:
                    target_language = None
        else:
            target_language = _canonical_translation_language(raw_target)
            if target_language is None:
                return jsonify(api_error('translation_invalid_language')), 400

        from src.models import ProcessingJob

        # A second request while one runs would only queue behind it (or get
        # the same job back from enqueue), so say that it is already running.
        running = ProcessingJob.query.filter(
            ProcessingJob.recording_id == recording.id,
            ProcessingJob.job_type == 'translate_transcription',
            ProcessingJob.status.in_(['queued', 'processing']),
        ).first()
        if running:
            return jsonify({
                'success': True,
                'job_id': running.id,
                'already_running': True,
                'message': translate('server.apiMessage.translationAlreadyRunning'),
            })

        # The job resolves a missing language the same way, then English.
        from src.services.transcription_translation import _resolve_target_language, _same_language

        effective_target = target_language or _resolve_target_language(current_user.id, None) or 'English'
        # Only a language chosen at transcription time counts; an
        # auto-detected recording goes to the model (see _same_language).
        if _same_language(recording.transcription_requested_language, effective_target):
            return jsonify({
                'success': True,
                'job_id': None,
                'same_language': True,
                'language': effective_target,
                'message': translate('server.apiMessage.translationSameLanguage', target=effective_target),
            })

        from src.services.job_queue import job_queue

        job_params = {'user_id': current_user.id}
        if target_language:
            job_params['target_language'] = target_language

        job_id = job_queue.enqueue(
            user_id=current_user.id,
            recording_id=recording.id,
            job_type='translate_transcription',
            params=job_params,
        )

        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': translate('server.apiMessage.translationQueued')
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error starting translation for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error_updating_transcription')), 500

# Toggle inbox status endpoint


@recordings_bp.route('/recording/<int:recording_id>/toggle_inbox', methods=['POST'])
@login_required
def toggle_inbox(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        # Only require view access (not edit) - users can manage their own inbox status
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Get current status and toggle it
        current_inbox, current_highlighted = get_user_recording_status(recording, current_user)
        new_inbox, new_highlighted = set_user_recording_status(recording, current_user, is_inbox=not current_inbox)

        return jsonify({'success': True, 'is_inbox': new_inbox})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error toggling inbox status for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500

# Toggle highlighted status endpoint


@recordings_bp.route('/recording/<int:recording_id>/toggle_highlight', methods=['POST'])
@login_required
def toggle_highlight(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        # Only require view access (not edit) - users can manage their own highlight status
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Get current status and toggle it
        current_inbox, current_highlighted = get_user_recording_status(recording, current_user)
        new_inbox, new_highlighted = set_user_recording_status(recording, current_user, is_highlighted=not current_highlighted)

        return jsonify({'success': True, 'is_highlighted': new_highlighted})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error toggling highlighted status for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500




@recordings_bp.route('/recording/<int:recording_id>/toggle_archive', methods=['POST'])
@login_required
def toggle_archive(recording_id):
    """Archive or unarchive a recording for the current user (#394)."""
    try:
        recording = db.session.get(Recording, recording_id)
        # Like inbox and star, archiving is personal and needs only view access.
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404
        from src.services.recording_state import get_user_archived, set_user_archived
        new_value = set_user_archived(recording, current_user, not get_user_archived(recording, current_user))
        return jsonify({'success': True, 'is_archived': new_value})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error toggling archive for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/recording/<int:recording_id>/delete_audio', methods=['POST'])
@login_required
def delete_recording_audio(recording_id):
    """Delete a recording's media file and keep the transcript, summary and notes.

    Same permission as deleting the recording. The recording then shows as
    "Audio removed", exactly like audio-only retention leaves it.
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404
        if recording.user_id != current_user.id:  # a recording without an owner is nobody's (#581)
            return jsonify(api_error('recording_delete_denied')), 403
        if not _self.USERS_CAN_DELETE and not current_user.is_admin:
            return jsonify(api_error('recording_delete_admin_only')), 403
        if recording.audio_deleted_at:
            return jsonify(api_error('audio_already_removed')), 409
        if recording.status not in ('COMPLETED', 'FAILED'):
            return jsonify(api_error('audio_remove_while_processing')), 409

        from src.services.retention import remove_recording_audio
        remove_recording_audio(recording)
        current_app.logger.info(f"User {current_user.id} removed the audio of recording {recording_id}")

        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({'success': True, 'recording': recording_dict})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error removing audio for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


# Shares per signed-in user (#435): a forced share from another page cannot
# fill an account or queue paid transcriptions without bound.
try:
    from limits import parse as _parse_limit
    SHARE_TARGET_LIMIT = _parse_limit(os.environ.get('SHARE_TARGET_RATE_LIMIT', '20 per hour'))
except Exception:
    SHARE_TARGET_LIMIT = None


def _share_target_allowed(user_id):
    if SHARE_TARGET_LIMIT is None or limiter is None or not getattr(limiter, 'enabled', True):
        return True
    return limiter.limiter.hit(SHARE_TARGET_LIMIT, 'share_target', str(user_id))


@recordings_bp.route('/share-target', methods=['POST', 'GET'])
@login_required
def share_target():
    """PWA Web Share Target receiver (issue #285).

    Android Chrome / iOS Safari 16.4+ POST a multipart/form-data body to
    this endpoint when the user picks Speakr from the native share sheet.
    The file arrives under the field name declared in the manifest
    (``shared_audio``). Any text/title/url params the share sheet sent
    become the recording's notes.

    CSRF is exempted at app startup (the share sheet has no way to
    round-trip a CSRF token). Authentication still happens via the
    session cookie carried by the browser; unauthenticated visitors hit
    @login_required and are bounced to the login page. A POST that a
    browser marks as sent from another site (``Sec-Fetch-Site``) is
    refused (#435): a share from the operating system's share sheet is
    marked ``none``, and a page elsewhere could otherwise submit a forged
    share with the signed-in user's cookies.

    Implementation: save the file to ``UPLOAD_FOLDER``, create a Recording
    row applying the same user / folder / tag defaults as ``upload_file``
    where they are unambiguous (no tags / folder context in a share),
    then enqueue a transcription job through the existing job queue.
    Redirect the browser to the SPA so the user lands on the recordings
    list with the new row visible.
    """
    if request.method == 'GET':
        # Some platforms pre-flight with GET. Just land the user on the SPA.
        return redirect(url_for('recordings.index'))

    # Browsers without Fetch Metadata send no header and are let through.
    if request.headers.get('Sec-Fetch-Site', '').lower() in ('cross-site', 'same-site'):
        current_app.logger.warning(
            f"share-target POST from another site refused for user {current_user.id}")
        return redirect(url_for('recordings.index') + '?share_target_error=cross_site')

    if not _share_target_allowed(current_user.id):
        current_app.logger.warning(f"share-target rate limit reached for user {current_user.id}")
        return redirect(url_for('recordings.index') + '?share_target_error=rate_limited')

    shared = request.files.get('shared_audio') or request.files.get('file')
    if not shared or not getattr(shared, 'filename', ''):
        return redirect(url_for('recordings.index') + '?share_target_error=missing_file')

    # The file goes through the same ingestion as every upload (#412 audit
    # P13): storage backend (local or S3), file hash and duplicate check,
    # meeting date, MIME type, title placeholder, recording.created and the
    # transcription job. A share sheet carries no tag or folder; its title,
    # text and url become the title and notes.
    from werkzeug.datastructures import MultiDict
    form = MultiDict()
    share_title = (request.form.get('title') or '').strip()
    if share_title:
        form['title'] = share_title
    notes_parts = [(request.form.get(k) or '').strip() for k in ('title', 'text', 'url')]
    notes_parts = [v for v in notes_parts if v]
    if notes_parts:
        form['notes'] = '\n\n'.join(notes_parts)

    try:
        result = ingest_uploaded_recording(owner=current_user, uploaded_file=shared, form=form,
                                           processing_source='share_target')
    except RequestEntityTooLarge:
        return redirect(url_for('recordings.index') + '?share_target_error=too_large')
    resp, status = result if isinstance(result, tuple) else (result, result.status_code)
    data = resp.get_json(silent=True) or {}
    if status >= 400 or not data.get('id'):
        current_app.logger.warning(f"share-target ingest failed ({status}): {data.get('error')}")
        if data.get('code') in ('audio_decode_failed', 'empty_file'):
            error = 'unreadable'
        else:
            error = {413: 'too_large', 507: 'storage_quota'}.get(status, 'save_failed')
        return redirect(url_for('recordings.index') + f'?share_target_error={error}')

    return redirect(url_for('recordings.index') + f"?share_target=ok&recording_id={data['id']}")


@recordings_bp.route('/upload', methods=['POST'])
@login_required
def upload_file():
    return ingest_uploaded_recording(
        owner=current_user,
        uploaded_file=request.files.get('file'),
        form=request.form,
    )


def ingest_uploaded_recording(
    *,
    owner,
    uploaded_file=None,
    form,
    original_filename=None,
    processing_source='upload',
    success_status=202,
    reuse_duplicate=False,
    prepare=None,
    on_conflict=None,
    source=None,
    enforce_upload_limits=True,
):
    """Run an uploaded file through Speakr's standard ingestion pipeline.

    enforce_upload_limits=False leaves out the upload size limits (the request
    ceiling, max_file_size_mb and the limit on the extracted audio of a video),
    which protect HTTP requests; the file monitor sets it for watched files.

    prepare(recording), when given, runs before the file is stored and the
    recording is first committed, so fields it sets (API v1 participants,
    references, the idempotency key) arrive with the recording itself.
    on_conflict(), when given, answers an upload whose rows hit a unique
    index (a concurrent retry with the same idempotency key, #476) with a
    response, or None for the usual error.
    """
    return ingest(
        owner=owner,
        uploaded_file=uploaded_file,
        form=form,
        original_filename=original_filename,
        processing_source=processing_source,
        success_status=success_status,
        reuse_duplicate=reuse_duplicate,
        prepare=prepare,
        on_conflict=on_conflict,
        source=source,
        enforce_upload_limits=enforce_upload_limits,
    )


def _ingest_join_part(owner, join, filepath, storage, now, *, original_filename, file_size,
                      mime_type, duration, meeting_date, title, notes, folder, tags,
                      prompt_variables, transcribe_params, duplicate_warning, success_status):
    """Store one file of a join; the file that completes the group creates the recording."""
    from src.services.upload_join import (
        store_part, claim_group, create_joined_recording, joined_recording, received_count)
    group, index, count = join
    store_part(owner, join, filepath, original_filename=original_filename, file_size=file_size,
               mime_type=mime_type, duration=duration, meeting_date=meeting_date,
               storage=storage, now=now)

    parts = claim_group(owner.id, group, count)
    if parts is None:
        recording = joined_recording(owner.id, group)
        if recording is not None:
            response_data = recording.to_dict(viewer_user=owner)
        else:
            response_data = {'join_pending': True, 'join_group': group, 'join_index': index,
                             'received': received_count(owner.id, group), 'total': count}
        if duplicate_warning:
            response_data['duplicate_warning'] = duplicate_warning
        return jsonify(response_data), success_status

    recording = create_joined_recording(
        owner, parts, title=title, notes=notes, folder=folder, tags=tags,
        prompt_variables=prompt_variables, transcribe_params=transcribe_params)
    current_app.logger.info(f"Joined {count} uploaded files into recording {recording.id}")
    from src.services.webhook_dispatch import emit_recording_created
    emit_recording_created(recording)
    response_data = recording.to_dict(viewer_user=owner)
    response_data['joined_parts'] = count
    if duplicate_warning:
        response_data['duplicate_warning'] = duplicate_warning
    return jsonify(response_data), success_status


@recordings_bp.route('/upload/join/<string:group>', methods=['DELETE'])
@login_required
def discard_upload_join(group):
    """Discard the uploaded parts of a join the dialog gave up on."""
    from src.services.upload_join import discard_group
    removed = discard_group(current_user.id, group)
    return jsonify({'success': True, 'removed': removed})


@recordings_bp.route('/api/recordings/incognito', methods=['POST'])
@login_required
def upload_incognito():
    """
    Process audio in incognito mode - no database storage.
    Returns transcript/summary directly in response.

    This endpoint is designed for HIPAA-friendly transcription where
    audio data is processed but never persisted to the database.
    Results are returned directly and only stored client-side in sessionStorage.
    """
    # Check if incognito mode is enabled
    if not _self.ENABLE_INCOGNITO_MODE:
        return jsonify(api_error('incognito_disabled')), 403

    import tempfile
    from datetime import datetime
    from src.tasks.processing import transcribe_incognito, generate_incognito_summary

    temp_filepath = None

    try:
        if 'file' not in request.files:
            return jsonify(api_error('no_file_provided')), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify(api_error('no_file_selected')), 400

        original_filename = file.filename
        safe_filename = secure_filename(original_filename)

        # Get file size
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        if file_size == 0:
            return jsonify({'incognito': True, **api_error('empty_file')}), 400

        # Mirror the regular /upload route's option-B size policy:
        # video files (by extension) use the larger video cap because
        # incognito always extracts audio (it never retains the video
        # stream), so it's effectively always in audio-only mode.
        # Audio uploads use the regular cap.
        _VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.mts'}
        _is_video = os.path.splitext(original_filename)[1].lower() in _VIDEO_EXTS
        regular_limit_mb = int(SystemSetting.get_setting('max_file_size_mb', 250))
        audio_only_limit_mb = int(
            SystemSetting.get_setting('max_audio_only_video_size_mb', regular_limit_mb * 4)
        )
        effective_limit_mb = audio_only_limit_mb if _is_video else regular_limit_mb
        if file_size > effective_limit_mb * 1024 * 1024:
            return jsonify({
                **api_error('file_too_large_max', maxSize=format_number(effective_limit_mb)),
                'max_size_mb': float(effective_limit_mb),
            }), 413

        # Save to temp file - use secure temp directory. Suffix carries only
        # the extension, not the user's filename: the temp path is logged and
        # filenames can themselves be sensitive (no PHI in logs or paths).
        _ext = os.path.splitext(safe_filename)[1].lower() or '.audio'
        with tempfile.NamedTemporaryFile(delete=False, suffix=_ext) as tmp:
            temp_filepath = tmp.name
            file.save(temp_filepath)
            current_app.logger.info(f"[Incognito] Temp file saved: {temp_filepath}")

        # Get optional parameters
        # An empty language falls through to the user's default, as on upload;
        # "auto" forces auto-detection (resolved in transcribe_incognito, #412).
        language = request.form.get('language', '')
        min_speakers = request.form.get('min_speakers')
        max_speakers = request.form.get('max_speakers')
        hotwords = request.form.get('hotwords', '').strip() or None
        initial_prompt = request.form.get('initial_prompt', '').strip() or None
        transcription_model = request.form.get('transcription_model', '').strip() or None
        auto_summarize = request.form.get('auto_summarize', 'false').lower() == 'true'
        # The admin switch that disables automatic summaries applies here too (#412).
        _admin_off = SystemSetting.get_setting('disable_auto_summarization', False)
        if (_admin_off if isinstance(_admin_off, bool) else str(_admin_off).lower() == 'true'):
            auto_summarize = False

        # Convert to int if provided
        if min_speakers:
            try:
                min_speakers = int(min_speakers)
            except (ValueError, TypeError):
                min_speakers = None
        if max_speakers:
            try:
                max_speakers = int(max_speakers)
            except (ValueError, TypeError):
                max_speakers = None

        # Log only metadata - NEVER log content for HIPAA compliance. The
        # user's filename is deliberately excluded too (extension only):
        # filenames routinely contain names/dates and count as PHI.
        current_app.logger.info(f"[Incognito] Processing request from user {current_user.id}: "
                               f"ext={_ext}, size={file_size/1024/1024:.2f}MB, "
                               f"language={language}, auto_summarize={auto_summarize}")

        # Perform transcription synchronously (no database operations)
        result = transcribe_incognito(
            filepath=temp_filepath,
            original_filename=original_filename,
            language=language,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            hotwords=hotwords,
            initial_prompt=initial_prompt,
            user=current_user,
            transcription_model=transcription_model
        )

        if result.get('error'):
            current_app.logger.error(f"[Incognito] Transcription failed: {result['error']}")
            # result['error'] is English; a known kind of failure carries
            # error_code and is answered in the interface language (#477).
            if result.get('error_code') == 'audio_decode_failed':
                return jsonify({
                    'incognito': True,
                    **api_error('audio_decode_failed'),
                }), 400
            error_text = result['error']
            if result.get('error_code') in ('audio_converter_missing', 'audio_conversion_timeout'):
                error_text = translate(f"server.apiError.{result['error_code']}")
            return jsonify({
                'incognito': True,
                'error': error_text,
                'error_type': result.get('error_type'),
            }), 500

        # Optionally generate summary
        summary = None
        if auto_summarize and result.get('transcription'):
            current_app.logger.info(f"[Incognito] Auto-summarize requested, generating summary...")
            summary = generate_incognito_summary(result['transcription'], current_user)

        # Build response
        # Render markdown to HTML for summary display
        summary_html = None
        if summary:
            summary_html = md_to_html(summary)

        response_data = {
            'incognito': True,
            'transcription': result.get('transcription'),
            'summary': summary,
            'summary_html': summary_html,
            'title': result.get('title', 'Incognito Recording'),
            'audio_duration_seconds': result.get('audio_duration_seconds'),
            'processing_time_seconds': result.get('processing_time_seconds'),
            'created_at': datetime.utcnow().isoformat() + 'Z',
            'original_filename': original_filename,
            'file_size': file_size
        }

        current_app.logger.info(f"[Incognito] Request completed successfully for user {current_user.id}")

        return jsonify(response_data), 200

    except RequestEntityTooLarge:
        max_size_mb = current_app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024)
        current_app.logger.warning(f"[Incognito] Upload failed: File too large (>{max_size_mb}MB)")
        return jsonify({
            'incognito': True,
            **api_error('file_too_large_max', maxSize=format_number(max_size_mb)),
            'max_size_mb': max_size_mb
        }), 413

    except Exception as e:
        current_app.logger.error(f"[Incognito] Error during processing: {e}", exc_info=True)
        return jsonify({
            'incognito': True,
            **api_error('unexpected_error_processing')
        }), 500

    finally:
        # CRITICAL: Always delete temp file for HIPAA compliance
        if temp_filepath and os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                current_app.logger.info(f"[Incognito] Temp file deleted: {temp_filepath}")
            except Exception as cleanup_error:
                current_app.logger.error(f"[Incognito] Failed to delete temp file {temp_filepath}: {cleanup_error}")


@recordings_bp.route('/api/recordings/incognito/chat', methods=['POST'])
@login_required
def chat_incognito():
    """
    Chat with an incognito recording's transcription.
    Since incognito recordings don't exist in the database, the transcription
    is passed directly in the request.
    """
    # Check if incognito mode is enabled
    if not _self.ENABLE_INCOGNITO_MODE:
        return jsonify(api_error('incognito_disabled')), 403

    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify(api_error('no_data_provided')), 400

        transcription = data.get('transcription')
        user_message = data.get('message')
        message_history = data.get('message_history', [])
        participants = data.get('participants', '')
        notes = data.get('notes', '')

        if not transcription or not isinstance(transcription, str):
            return jsonify(api_error('no_transcription_provided')), 400
        if not user_message or not isinstance(user_message, str):
            return jsonify(api_error('no_message_provided')), 400
        if not isinstance(message_history, list):
            message_history = []

        # Check if chat client is available
        if chat_client is None:
            return jsonify(api_error('chat_client_not_configured')), 503

        # Same prompt as API v1 chat (src/services/recording_chat.py). An
        # incognito recording has no database row: the transcript, participants
        # and notes come from this request (#527).
        from src.services.recording_chat import IncognitoTranscript, build_chat_messages
        transcript = IncognitoTranscript(transcription, participants, notes)
        messages, _ = build_chat_messages(transcript, current_user, user_message, message_history)

        user_id = current_user.id

        current_app.logger.info(f"[Incognito Chat] User {user_id} sending message")

        # Resolved here, in the request context, before the response streams.
        chat_temperature = get_temperature('chat')

        def generate():
            """Stream the chat response."""
            try:
                # No user_id, so nothing is tracked for incognito
                response = call_chat_completion(messages, temperature=chat_temperature, stream=True)

                for chunk in response:
                    if chunk.choices and len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        if hasattr(delta, 'content') and delta.content:
                            yield f"data: {json.dumps({'content': delta.content})}\n\n"

                yield "data: [DONE]\n\n"

            except Exception as e:
                current_app.logger.error(f"[Incognito Chat] Error during streaming: {e}")
                # Provide more helpful error message for connection issues
                error_msg = str(e).lower()
                if 'connection' in error_msg or 'connect' in error_msg or 'refused' in error_msg:
                    yield f"data: {json.dumps(api_error('llm_connection_failed'))}\n\n"
                else:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return Response(generate(), mimetype='text/event-stream')

    except Exception as e:
        current_app.logger.error(f"[Incognito Chat] Error: {e}", exc_info=True)
        # Provide more helpful error message for connection issues
        error_msg = str(e).lower()
        if 'connection' in error_msg or 'connect' in error_msg or 'refused' in error_msg:
            return jsonify(api_error('llm_connection_failed')), 503
        return jsonify(api_error('chat_error')), 500


@recordings_bp.route('/api/recordings/incognito/summary', methods=['POST'])
@login_required
def generate_incognito_summary_endpoint():
    """
    Generate summary for an incognito recording on demand.
    Since incognito recordings don't exist in the database, the transcription
    is passed directly in the request.
    """
    # Check if incognito mode is enabled
    if not _self.ENABLE_INCOGNITO_MODE:
        return jsonify(api_error('incognito_disabled')), 403

    from src.tasks.processing import generate_incognito_summary
    from src.utils import md_to_html

    try:
        data = request.json
        if not data:
            return jsonify(api_error('no_data_provided')), 400

        transcription = data.get('transcription')
        if not transcription:
            return jsonify(api_error('no_transcription_provided')), 400

        # Check if LLM client is available
        if client is None:
            return jsonify(api_error('summary_llm_not_configured')), 503

        current_app.logger.info(f"[Incognito Summary] User {current_user.id} requesting summary generation")

        # Generate summary using existing function
        summary = generate_incognito_summary(transcription, current_user)

        if summary:
            summary_html = md_to_html(summary)
            return jsonify({
                'summary': summary,
                'summary_html': summary_html
            })
        else:
            return jsonify(api_error('summary_generation_failed')), 503

    except Exception as e:
        current_app.logger.error(f"[Incognito Summary] Error: {e}", exc_info=True)
        # Provide more helpful error message for connection issues
        error_msg = str(e).lower()
        if 'connection' in error_msg or 'connect' in error_msg or 'refused' in error_msg:
            return jsonify(api_error('llm_connection_failed')), 503
        return jsonify(api_error('summary_generation_failed_detail', detail=str(e))), 500


# Status Endpoint


@recordings_bp.route('/recording/<int:recording_id>', methods=['DELETE'])
@login_required
def delete_recording(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Check if the recording belongs to the current user
        if recording.user_id != current_user.id:  # a recording without an owner is nobody's (#581)
            return jsonify(api_error('recording_delete_denied')), 403

        # Check deletion permissions (admin-only if USERS_CAN_DELETE is false)
        if not _self.USERS_CAN_DELETE and not current_user.is_admin:
            return jsonify(api_error('recording_delete_admin_only')), 403

        # Log embeddings cleanup for Inquire Mode if enabled
        if ENABLE_INQUIRE_MODE:
            chunk_count = TranscriptChunk.query.filter_by(recording_id=recording_id).count()
            if chunk_count > 0:
                current_app.logger.info(f"Deleting {chunk_count} transcript chunks with embeddings for recording {recording_id}")

        from src.services.recording_deletion import delete_recording_completely, cleanup_orphaned_speakers_quietly
        delete_recording_completely(recording, storage=get_storage_service())
        cleanup_orphaned_speakers_quietly()

        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error_deleting')), 500


# --- Inbox and Archive Endpoints ---

@recordings_bp.route('/api/inbox_recordings', methods=['GET'])
@login_required
def get_inbox_recordings():
    """Get recordings that are in the inbox and currently processing."""
    from sqlalchemy import select
    try:
        stmt = select(Recording).where(
            Recording.user_id == current_user.id,
            Recording.is_inbox == True,
            Recording.status.in_(['PENDING', 'PROCESSING', 'SUMMARIZING'])
        ).order_by(Recording.created_at.desc())

        # Page lookups read once, not per row (D#483)
        from src.services.recording_list import RecordingListBatch, list_load_options
        recordings = db.session.execute(stmt.options(*list_load_options())).scalars().all()
        batch = RecordingListBatch(recordings, current_user)
        return jsonify([recording.to_list_dict(viewer_user=current_user, batch=batch) for recording in recordings])
    except Exception as e:
        current_app.logger.error(f"Error fetching inbox recordings: {e}")
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/api/recordings/audio-removed', methods=['GET'])
@recordings_bp.route('/api/recordings/archived', methods=['GET'])
@login_required
def get_archived_recordings():
    """Get recordings where audio has been deleted but transcription remains.

    /api/recordings/archived is the name from before #394 and is kept for
    existing callers; it does not list recordings archived by the user (use
    GET /api/recordings?archived=true for those).
    """
    from sqlalchemy import select
    try:
        search_query = request.args.get('q', '').strip()

        # Find recordings owned by current user where audio_deleted_at is not null
        stmt = select(Recording).where(
            Recording.user_id == current_user.id,
            Recording.audio_deleted_at.is_not(None)
        ).order_by(Recording.audio_deleted_at.desc())

        # Page lookups read once, not per row (D#483)
        from src.services.recording_list import RecordingListBatch, list_load_options
        recordings = db.session.execute(stmt.options(*list_load_options())).scalars().all()
        batch = RecordingListBatch(recordings, current_user)
        return jsonify([recording.to_list_dict(viewer_user=current_user, batch=batch) for recording in recordings])
    except Exception as e:
        current_app.logger.error(f"Error fetching archived recordings: {e}")
        return jsonify({'error': str(e)}), 500


# --- Recording Detail and Audio Endpoints ---

@recordings_bp.route('/api/recordings/<int:recording_id>', methods=['GET'])
@login_required
def get_recording_detail(recording_id):
    """Get full details for a specific recording including markdown HTML."""
    try:
        recording = db.session.get(Recording, recording_id)

        if not recording:
            return jsonify(api_error('recording_not_found')), 404

        # Check ownership or shared access
        has_access = recording.user_id == current_user.id

        # Check if recording has been shared with current user (if internal sharing is enabled)
        if not has_access and _self.ENABLE_INTERNAL_SHARING:
            share = InternalShare.query.filter_by(
                recording_id=recording_id,
                shared_with_user_id=current_user.id
            ).first()
            has_access = share is not None

        if not has_access:
            return jsonify(api_error('recording_not_found')), 404

        # Return full detail with HTML conversion
        rec_dict = recording.to_dict(include_html=True, viewer_user=current_user)

        # Add sharing metadata
        is_owner = recording.user_id == current_user.id
        rec_dict['is_owner'] = is_owner

        # Add edit permission info (uses has_recording_access which checks group admin status)
        rec_dict['can_edit'] = has_recording_access(recording, current_user, require_edit=True)

        # Add delete permission info (only owner can delete)
        rec_dict['can_delete'] = is_owner and (_self.USERS_CAN_DELETE or current_user.is_admin)

        # Add sharing-related fields
        if not is_owner:
            # This is a shared recording - get owner info and share permissions
            owner = db.session.get(User, recording.user_id)
            rec_dict['owner_username'] = owner.username if owner else "Unknown"
            rec_dict['is_shared'] = True
            # Don't show outgoing share count for recordings you don't own
            rec_dict['shared_with_count'] = 0
            rec_dict['public_share_count'] = 0

            # Get share permissions
            share = InternalShare.query.filter_by(
                recording_id=recording.id,
                shared_with_user_id=current_user.id
            ).first()

            if share:
                rec_dict['share_info'] = {
                    'share_id': share.id,
                    'owner_username': owner.username if owner else "Unknown",
                    'can_edit': share.can_edit,
                    'can_reshare': share.can_reshare,
                    'shared_at': share.created_at.isoformat()
                }
            else:
                # Fallback if share record not found (shouldn't happen)
                rec_dict['share_info'] = {
                    'can_edit': False,
                    'can_reshare': False
                }
        else:
            rec_dict['is_shared'] = False

        # Check if recording has group tags (among visible tags)
        visible_tags = recording.get_visible_tags(current_user)
        has_group_tags = any(tag.is_group_tag for tag in visible_tags) if visible_tags else False
        rec_dict['has_group_tags'] = has_group_tags

        # Enrich with per-user status
        enrich_recording_dict_with_user_status(rec_dict, recording, current_user)

        return jsonify(rec_dict)
    except Exception as e:
        current_app.logger.error(f"Error fetching recording detail: {e}")
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/recording/<int:recording_id>/status', methods=['GET'])
@login_required
def get_recording_status_only(recording_id):
    """
    Lightweight endpoint that returns only the status field.
    Used for polling during processing/summarization.
    Note: Rate limiting exemption is configured at app level.
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Return the status plus audio_ready so pollers can enable playback the
        # moment a merge/stitch finishes writing the audio file (#323).
        return jsonify({
            'status': recording.status,
            'audio_ready': bool(recording.audio_path) and recording.audio_deleted_at is None,
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching status for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/batch-status', methods=['POST'])
@login_required
def get_batch_recording_status():
    """
    Batch endpoint to get status for multiple recordings at once.
    More efficient than polling individual status endpoints.

    Request body: {"recording_ids": [1, 2, 3]}
    Response: {"statuses": {"1": "COMPLETED", "2": "PROCESSING", "3": "FAILED"}}
    """
    try:
        data = request.get_json()
        if not data or 'recording_ids' not in data:
            return jsonify(api_error('recording_ids_required')), 400

        recording_ids = data['recording_ids']
        if not isinstance(recording_ids, list):
            return jsonify(api_error('invalid_recording_ids_list')), 400

        # Limit batch size to prevent abuse
        if len(recording_ids) > 50:
            return jsonify(api_error('batch_limit_50')), 400

        # Query all recordings at once
        recordings = Recording.query.filter(Recording.id.in_(recording_ids)).all()

        # Build response with only accessible recordings
        statuses = {}
        for recording in recordings:
            if has_recording_access(recording, current_user):
                statuses[str(recording.id)] = recording.status

        return jsonify({'statuses': statuses})
    except Exception as e:
        current_app.logger.error(f"Error fetching batch status: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/job-queue-status', methods=['GET'])
@login_required
def get_job_queue_status():
    """
    Get detailed job queue status for all jobs (active, completed, and failed).
    Returns status for the user's jobs within the last hour.
    """
    try:
        from src.models import ProcessingJob
        from src.services.job_queue import job_queue, queue_name_for
        from datetime import timedelta

        # Expire all cached objects to ensure we see latest data from worker threads
        db.session.expire_all()

        # Get all jobs for the user (active + recent completed/failed within last hour)
        cutoff_time = datetime.utcnow() - timedelta(hours=1)
        # Inquire chunk jobs run unseen after a recording is completed.
        all_jobs = ProcessingJob.query.filter(
            ProcessingJob.user_id == current_user.id,
            ProcessingJob.job_type.notin_(job_queue.jobs_for_queue('index')),
            db.or_(
                ProcessingJob.status.in_(['queued', 'processing']),
                db.and_(
                    ProcessingJob.status.in_(['completed', 'failed']),
                    ProcessingJob.completed_at >= cutoff_time
                )
            )
        ).order_by(ProcessingJob.created_at.desc()).all()

        places = {}
        for name in {queue_name_for(job.job_type) for job in all_jobs if job.status == 'queued'}:
            queue_jobs = job_queue.jobs_for_queue(name)
            order = job_queue.turn_order(queue_jobs)
            currently_processing = ProcessingJob.query.filter(
                ProcessingJob.status == 'processing',
                ProcessingJob.job_type.in_(queue_jobs)
            ).count()
            for place, queued in enumerate(order, 1):
                places[queued.id] = place + currently_processing

        job_details = []
        for job in all_jobs:
            recording = db.session.get(Recording, job.recording_id)
            recording_title = None
            if recording:
                recording_title = recording.title or recording.original_filename or 'Untitled'

            queue_type = queue_name_for(job.job_type)
            position = places.get(job.id)

            job_details.append({
                'id': job.id,
                'recording_id': job.recording_id,
                'recording_title': recording_title,
                'job_status': job.status,
                'job_type': job.job_type,
                'queue_type': queue_type,
                'position': position,
                'is_new_upload': job.is_new_upload,
                'error_message': job.error_message,
                'progress_done': job.progress_done,
                'progress_total': job.progress_total,
                'created_at': job.created_at.isoformat() if job.created_at else None,
                'started_at': job.started_at.isoformat() if job.started_at else None,
                'completed_at': job.completed_at.isoformat() if job.completed_at else None
            })

        return jsonify({'jobs': job_details})
    except Exception as e:
        current_app.logger.error(f"Error fetching job queue status: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/jobs/<int:job_id>/retry', methods=['POST'])
@login_required
def retry_failed_job(job_id):
    """Retry a failed job."""
    try:
        from src.models import ProcessingJob

        job = db.session.get(ProcessingJob, job_id)
        if not job:
            return jsonify(api_error('job_not_found')), 404

        if job.user_id != current_user.id:
            return jsonify(api_error('access_denied')), 403

        if job.status != 'failed':
            return jsonify(api_error('job_retry_failed_only')), 400

        # Only one job of a type may be queued or running per recording
        # (ux_processing_job_active_type), so a newer one blocks the retry.
        active = ProcessingJob.query.filter(
            ProcessingJob.recording_id == job.recording_id,
            ProcessingJob.job_type == job.job_type,
            ProcessingJob.status.in_(['queued', 'processing']),
        ).first()
        if active:
            return jsonify(api_error('recording_already_processing')), 409

        # Reset job for retry
        job.status = 'queued'
        job.error_message = None
        job.retry_count = 0
        job.started_at = None
        job.completed_at = None
        job.progress_done = None
        job.progress_total = None
        db.session.commit()

        current_app.logger.info(f"Job {job_id} queued for retry by user {current_user.id}")
        return jsonify({'success': True, 'message': translate('server.apiMessage.jobQueuedForRetry')})

    except IntegrityError:
        # A job of the same type was queued between the check and the commit.
        db.session.rollback()
        return jsonify(api_error('recording_already_processing')), 409
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error retrying job {job_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/jobs/<int:job_id>', methods=['DELETE'])
@login_required
def delete_job(job_id):
    """Delete a job (clear from queue or history)."""
    try:
        from src.models import ProcessingJob
        import os

        job = db.session.get(ProcessingJob, job_id)
        if not job:
            return jsonify(api_error('job_not_found')), 404

        if job.user_id != current_user.id:
            return jsonify(api_error('access_denied')), 403

        # If it's a failed new upload, also delete the recording
        if job.status == 'failed' and job.is_new_upload:
            recording = db.session.get(Recording, job.recording_id)
            if recording:
                # Delete audio file
                if recording.audio_path:
                    try:
                        get_storage_service().delete(recording.audio_path, missing_ok=True)
                    except Exception as e:
                        current_app.logger.error(f"Error deleting audio file: {e}")
                # Delete ALL processing jobs for this recording first
                ProcessingJob.query.filter_by(recording_id=recording.id).delete()
                db.session.delete(recording)
        else:
            # Just delete this job
            db.session.delete(job)
        db.session.commit()

        current_app.logger.info(f"Job {job_id} deleted by user {current_user.id}")
        return jsonify({'success': True, 'message': translate('server.apiMessage.jobDeleted')})

    except Exception as e:
        current_app.logger.error(f"Error deleting job {job_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/jobs/clear-completed', methods=['POST'])
@login_required
def clear_completed_jobs():
    """Clear all completed jobs for the current user."""
    try:
        from src.models import ProcessingJob

        deleted = ProcessingJob.query.filter(
            ProcessingJob.user_id == current_user.id,
            ProcessingJob.status == 'completed'
        ).delete(synchronize_session=False)

        db.session.commit()
        current_app.logger.info(f"Cleared {deleted} completed jobs for user {current_user.id}")
        return jsonify({'success': True, 'deleted': deleted})

    except Exception as e:
        current_app.logger.error(f"Error clearing completed jobs: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/status/<int:recording_id>', methods=['GET'])
@login_required
def get_status(recording_id):
    """Endpoint to check the transcription/summarization status (full recording data)."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Ensure events are loaded (refresh the recording to get latest relationships)
        db.session.refresh(recording)

        # Get recording dict and enrich with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)

        return jsonify(recording_dict)
    except Exception as e:
        current_app.logger.error(f"Error fetching status for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/audio/<int:recording_id>')
@login_required
def get_audio(recording_id):
    """Serve audio file for a recording.

    Query parameters:
        download: If 'true', serves file as attachment for download
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not recording.audio_path:
            return jsonify(api_error('recording_or_audio_not_found')), 404

        # Check if the recording belongs to the current user or has been shared with them
        has_access = recording.user_id == current_user.id

        # Check if recording has been shared with current user (if internal sharing is enabled)
        if not has_access and _self.ENABLE_INTERNAL_SHARING:
            share = InternalShare.query.filter_by(
                recording_id=recording_id,
                shared_with_user_id=current_user.id
            ).first()
            has_access = share is not None

        if not has_access:
            return jsonify(api_error('recording_or_audio_not_found')), 404
        storage = get_storage_service()

        # Check if download is requested
        download = request.args.get('download', 'false').lower() == 'true'
        download_name = None
        if download:
            # Generate filename from recording title or use default
            filename = recording.title or f'recording_{recording_id}'
            # Sanitize filename and add extension
            filename = "".join(c for c in filename if c.isalnum() or c in (' ', '-', '_')).strip()
            ext = os.path.splitext(recording.original_filename or '')[1] or '.mp3'
            download_name = f"{filename}{ext}"

        serve_mime = served_media_mime(recording.mime_type, recording.audio_path)
        delivery = storage.get_audio_delivery(
            recording.audio_path,
            download=download,
            mime_type=serve_mime,
            download_name=download_name,
            is_public=False,
        )

        if delivery.mode == 'redirect_url':
            return redirect(delivery.url, code=302)

        if not delivery.local_path or not os.path.exists(delivery.local_path):
            current_app.logger.error(f"Audio file missing from server: {recording.audio_path}")
            return jsonify(api_error('audio_file_missing')), 404

        if download:
            return sandbox_media_response(send_file(
                delivery.local_path,
                as_attachment=True,
                download_name=download_name,
                mimetype=serve_mime,
                conditional=True,
            ))

        return sandbox_media_response(send_file(delivery.local_path, mimetype=serve_mime, conditional=True))
    except Exception as e:
        current_app.logger.error(f"Error serving audio for recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


# --- Chat with Transcription ---

@recordings_bp.route('/chat', methods=['POST'])
@login_required
def chat_with_transcription():
    """Chat with a specific recording's transcription."""
    try:
        data = request.json
        if not data:
            return jsonify(api_error('no_data_provided')), 400

        recording_id = data.get('recording_id')
        user_message = data.get('message')
        message_history = data.get('message_history', [])

        if not recording_id:
            return jsonify(api_error('no_recording_id')), 400
        if not user_message:
            return jsonify(api_error('no_message_provided')), 400

        # Get the recording
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Check if transcription exists
        if not recording.transcription or len(recording.transcription.strip()) < 10:
            return jsonify(api_error('no_transcription_for_recording')), 400

        # Check if transcription is an error message (not actual content)
        if is_transcription_error(recording.transcription):
            return jsonify(api_error('chat_blocked_transcription_failed')), 400

        # Check if chat client is available
        if chat_client is None:
            return jsonify(api_error('chat_client_not_configured')), 503

        # Prepare the system prompt with the transcription
        user_chat_output_language = current_user.output_language if current_user.is_authenticated else None

        language_instruction = ""
        if user_chat_output_language:
            language_instruction = f"Please provide all your responses in {user_chat_output_language}."

        user_name = current_user.name if current_user.is_authenticated and current_user.name else "User"
        user_title = current_user.job_title if current_user.is_authenticated and current_user.job_title else "a professional"
        user_company = current_user.company if current_user.is_authenticated and current_user.company else "their organization"

        # Optionally include timestamps in the chat transcript per the user's setting (#304).
        _chat_ts = bool(current_user.is_authenticated and current_user.chat_include_timestamps)
        formatted_transcription = format_transcription_for_llm(
            recording.transcription,
            include_timestamps=_chat_ts,
            template_format=_resolve_timestamp_template_format(
                current_user, current_user.chat_timestamp_template_id) if _chat_ts else None,
        )

        # Get configurable transcript length limit for chat
        transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
        if transcript_limit == -1:
            # No limit
            chat_transcript = formatted_transcription
        else:
            chat_transcript = formatted_transcription[:transcript_limit]

        # When timestamps are in the transcript, ask the model to cite them so
        # the chat panel can render clickable seek chips (bracketed [h:mm:ss]).
        timestamp_instruction = ""
        if _chat_ts:
            timestamp_instruction = (
                "\nWhen you reference a specific moment in the recording, cite its "
                "timestamp in square brackets exactly as it appears in the transcript "
                "(for example [00:07:35]). The interface renders these as clickable "
                "links that start playback at that moment, so cite them wherever they "
                "support your answer. Never invent a timestamp that is not in the "
                "transcript.\n"
            )

        system_prompt = f"""You are a professional meeting and audio transcription analyst assisting {user_name}, who is a(n) {user_title} at {user_company}. {language_instruction} Analyze the following meeting information and respond to the specific request.
{timestamp_instruction}
Following are the meeting participants and their roles:
{recording.participants or "No specific participants information provided."}

Following is the meeting transcript:
<<start transcript>>
{chat_transcript or "No transcript available."}
<<end transcript>>

Additional context and notes about the meeting:
{recording.notes or "none"}
"""

        # Prepare messages array with system prompt and conversation history
        messages = [{"role": "system", "content": system_prompt}]
        if message_history:
            messages.extend(message_history)
        messages.append({"role": "user", "content": user_message})

        # Capture context before generator starts (app context may not be available inside generator)
        user_id = current_user.id
        app = current_app._get_current_object()

        # Resolved here, in the request context, before the response streams.
        chat_temperature = get_temperature('chat')

        def generate():
            # Push app context for entire generator execution
            # This is needed because call_chat_completion checks budget which requires db access
            ctx = app.app_context()
            ctx.push()
            try:
                # Enable streaming with user_id for budget enforcement
                stream = call_chat_completion(
                    messages=messages,
                    temperature=chat_temperature,
                    max_tokens=int(os.environ.get("CHAT_MAX_TOKENS", "2000")),
                    stream=True,
                    user_id=user_id,
                    operation_type='chat'
                )

                # Use helper function to process streaming with thinking tag support
                for response in process_streaming_with_thinking(stream, user_id=user_id, operation_type='chat', app=app):
                    yield response

            except TokenBudgetExceeded as e:
                app.logger.warning(f"Token budget exceeded for user {user_id}: {e}")
                yield f"data: {json.dumps({'error': str(e), 'budget_exceeded': True})}\n\n"
            except Exception as e:
                app.logger.error(f"Error during chat stream generation: {str(e)}")
                # Provide more helpful error message for connection issues
                error_msg = str(e).lower()
                if 'connection' in error_msg or 'connect' in error_msg or 'refused' in error_msg:
                    yield f"data: {json.dumps(api_error('llm_connection_failed'))}\n\n"
                else:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                ctx.pop()

        return Response(generate(), mimetype='text/event-stream')

    except Exception as e:
        current_app.logger.error(f"Error in chat endpoint: {str(e)}")
        error_msg = str(e).lower()
        if 'connection' in error_msg or 'connect' in error_msg or 'refused' in error_msg:
            return jsonify(api_error('llm_connection_failed')), 503
        return jsonify({'error': str(e)}), 500


# --- Tag Management for Recordings ---

@recordings_bp.route('/api/recordings/<int:recording_id>/tags', methods=['POST'])
@login_required
def add_tag_to_recording(recording_id):
    """Add a tag to a recording. Triggers auto-share for group tags."""
    try:
        recording = db.session.get(Recording, recording_id)
        # Check if user has view access to this recording
        # (Edit permission will be checked for group tags specifically)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        data = request.get_json()
        tag_id = data.get('tag_id')

        if not tag_id:
            return jsonify(api_error('tag_id_required')), 400

        tag = db.session.get(Tag, tag_id)
        if not tag:
            return jsonify(api_error('tag_not_found')), 404

        # Check if user has access to this tag and permission to apply it
        if tag.group_id:
            # Group tag - check membership first
            membership = GroupMembership.query.filter_by(
                group_id=tag.group_id,
                user_id=current_user.id
            ).first()
            if not membership:
                return jsonify(api_error('tag_access_denied')), 403

            # Only file owner or group admin can apply group tags
            if recording.user_id != current_user.id and membership.role != 'admin':
                return jsonify(api_error('group_tag_apply_restricted')), 403

            # Group tags require edit permission
            if not has_recording_access(recording, current_user, require_edit=True):
                return jsonify(api_error('group_tag_apply_denied')), 403
        else:
            # Personal tag - only the tag owner can use it (view access is sufficient)
            if tag.user_id != current_user.id:
                return jsonify(api_error('personal_tag_apply_own_only')), 403

        # Check if tag is already on the recording
        existing = RecordingTag.query.filter_by(
            recording_id=recording_id,
            tag_id=tag_id
        ).first()

        if existing:
            return jsonify(api_error('tag_already_on_recording')), 400

        # Get the next order position
        max_order = db.session.query(db.func.max(RecordingTag.order)).filter_by(
            recording_id=recording_id
        ).scalar() or 0

        # Add the tag
        recording_tag = RecordingTag(
            recording_id=recording_id,
            tag_id=tag_id,
            order=max_order + 1
        )
        db.session.add(recording_tag)

        # A group tag with sharing shares the completed recording (same rule
        # as API v1 and the end of processing: src/services/tag_sharing.py).
        from src.services.tag_sharing import apply_tag_shares
        shares_created = apply_tag_shares(recording, tag, sharing_enabled=_self.ENABLE_INTERNAL_SHARING)
        if shares_created > 0:
            current_app.logger.info(f"Created {shares_created} auto-shares for recording {recording_id} via group tag '{tag.name}'")

        db.session.commit()

        # Return updated recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'recording': recording_dict,
            'tag': tag.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error adding tag to recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/<int:recording_id>/tags/<int:tag_id>', methods=['DELETE'])
@login_required
def remove_tag_from_recording(recording_id, tag_id):
    """Remove a tag from a recording. Cleans up auto-shares for group tags."""
    try:
        recording = db.session.get(Recording, recording_id)
        # Check if user has view access to this recording
        # (Edit permission will be checked for group tags specifically)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Find the recording-tag association
        recording_tag = RecordingTag.query.filter_by(
            recording_id=recording_id,
            tag_id=tag_id
        ).first()

        if not recording_tag:
            return jsonify(api_error('tag_not_on_recording')), 404

        # Get the tag to check permissions and for cleanup
        tag = db.session.get(Tag, tag_id)
        if tag:
            # Check permissions to remove this specific tag
            if tag.group_id:
                # Group tag - only file owner or group admin can remove
                membership = GroupMembership.query.filter_by(
                    group_id=tag.group_id,
                    user_id=current_user.id
                ).first()
                if recording.user_id != current_user.id:
                    if not membership or membership.role != 'admin':
                        return jsonify(api_error('group_tag_remove_restricted')), 403

                # Group tags require edit permission
                if not has_recording_access(recording, current_user, require_edit=True):
                    return jsonify(api_error('group_tag_remove_denied')), 403
            else:
                # Personal tag - can be removed by tag owner (view access) or recording owner (edit access)
                if tag.user_id != current_user.id:
                    # Not the tag owner, must be recording owner with edit permission
                    if not has_recording_access(recording, current_user, require_edit=True):
                        return jsonify(api_error('personal_tag_remove_own_only')), 403

        # Remove the association
        db.session.delete(recording_tag)

        # Shares this group tag made end, unless another group tag or the
        # recording's group folder still gives them (#570, #582)
        from src.services.auto_shares import drop_stale_auto_shares
        shares_removed = drop_stale_auto_shares(recording_ids=[recording_id])
        if shares_removed > 0:
            current_app.logger.info(f"Removed {shares_removed} auto-shares for recording {recording_id} after removing tag {tag_id}")

        db.session.commit()

        # Return updated recording with per-user status
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'recording': recording_dict
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error removing tag from recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/<int:recording_id>/tags/reorder', methods=['PUT'])
@login_required
def reorder_recording_tags(recording_id):
    """Reorder tags on a recording. Updates the order field for each RecordingTag."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        # Check if user has edit access to this recording
        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify(api_error('recording_modify_denied')), 403

        data = request.get_json()
        if not data or 'tag_ids' not in data:
            return jsonify(api_error('missing_tag_ids')), 400

        tag_ids = data.get('tag_ids', [])
        if not isinstance(tag_ids, list):
            return jsonify(api_error('invalid_tag_ids')), 400

        # Update order for each tag
        for order, tag_id in enumerate(tag_ids, 1):
            recording_tag = RecordingTag.query.filter_by(
                recording_id=recording_id,
                tag_id=tag_id
            ).first()
            if recording_tag:
                recording_tag.order = order

        db.session.commit()

        # Return updated recording
        recording_dict = recording.to_dict(viewer_user=current_user)
        enrich_recording_dict_with_user_status(recording_dict, recording, current_user)
        return jsonify({
            'success': True,
            'recording': recording_dict
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error reordering tags on recording {recording_id}: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


# --- Bulk Operations ---

@recordings_bp.route('/api/recordings/bulk', methods=['DELETE'])
@login_required
def bulk_delete_recordings():
    """Delete multiple recordings at once."""
    try:
        data = request.get_json()
        if not data or 'recording_ids' not in data:
            return jsonify(api_error('missing_recording_ids')), 400

        recording_ids = data.get('recording_ids', [])
        if not isinstance(recording_ids, list) or len(recording_ids) == 0:
            return jsonify(api_error('recording_ids_empty')), 400

        # Limit bulk operations to prevent abuse
        if len(recording_ids) > 100:
            return jsonify(api_error('bulk_delete_limit')), 400

        # Check deletion permissions
        if not _self.USERS_CAN_DELETE and not current_user.is_admin:
            return jsonify(api_error('recording_delete_admin_only')), 403

        deleted_ids = []
        errors = []

        for recording_id in recording_ids:
            try:
                recording = db.session.get(Recording, recording_id)
                if not recording or not has_recording_access(recording, current_user):
                    errors.append(f"Recording {recording_id} not found")
                    continue

                # Check ownership
                if recording.user_id != current_user.id:  # a recording without an owner is nobody's (#581)
                    errors.append(f"No permission for recording {recording_id}")
                    continue

                delete_recording_completely(recording, storage=get_storage_service())
                deleted_ids.append(recording_id)

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error deleting recording {recording_id}: {e}")
                errors.append(f"Error with recording {recording_id}")

        if deleted_ids:
            cleanup_orphaned_speakers_quietly()

        return jsonify({
            'success': True,
            'deleted_ids': deleted_ids,
            'deleted_count': len(deleted_ids),
            'errors': errors if errors else None
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in bulk delete: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/bulk-tags', methods=['POST'])
@login_required
def bulk_update_tags():
    """Add or remove a tag from multiple recordings."""
    try:
        data = request.get_json()
        if not data:
            return jsonify(api_error('missing_request_body')), 400

        recording_ids = data.get('recording_ids', [])
        tag_id = data.get('tag_id')
        action = data.get('action', 'add')  # 'add' or 'remove'

        if not recording_ids or not tag_id:
            return jsonify(api_error('missing_recording_ids_or_tag_id')), 400

        if action not in ['add', 'remove']:
            return jsonify(api_error('invalid_bulk_tag_action')), 400

        if len(recording_ids) > 100:
            return jsonify(api_error('bulk_update_limit')), 400

        # Verify tag exists and user has access
        tag = db.session.get(Tag, tag_id)
        if not tag:
            return jsonify(api_error('tag_not_found')), 404

        # Access check matching the single-tag endpoints: a group tag requires
        # membership in that group (previously group tags skipped this check
        # entirely, letting a non-member attach another group's tag); a
        # personal tag requires ownership.
        if tag.group_id:
            membership = GroupMembership.query.filter_by(
                group_id=tag.group_id, user_id=current_user.id).first()
            if not membership:
                return jsonify(api_error('tag_access_denied')), 403
        elif tag.user_id != current_user.id:
            return jsonify(api_error('tag_use_denied')), 403

        affected_ids = []

        for recording_id in recording_ids:
            try:
                recording = db.session.get(Recording, recording_id)
                if not recording:
                    continue

                # Check ownership or edit access
                if not has_recording_access(recording, current_user, require_edit=True):
                    continue

                # A group tag goes on another user's recording only from a
                # group admin, as on the single-tag route (#578)
                if action == 'add' and tag.group_id and recording.user_id != current_user.id \
                        and membership.role != 'admin':
                    continue

                if action == 'add':
                    # Check if tag already exists
                    existing = RecordingTag.query.filter_by(
                        recording_id=recording_id,
                        tag_id=tag_id
                    ).first()

                    if not existing:
                        # Get max order for this recording
                        max_order = db.session.query(db.func.max(RecordingTag.order)).filter_by(
                            recording_id=recording_id
                        ).scalar() or 0

                        new_tag = RecordingTag(
                            recording_id=recording_id,
                            tag_id=tag_id,
                            order=max_order + 1
                        )
                        db.session.add(new_tag)
                        affected_ids.append(recording_id)

                else:  # remove
                    recording_tag = RecordingTag.query.filter_by(
                        recording_id=recording_id,
                        tag_id=tag_id
                    ).first()

                    if recording_tag:
                        db.session.delete(recording_tag)
                        affected_ids.append(recording_id)

            except Exception as e:
                current_app.logger.error(f"Error updating tag for recording {recording_id}: {e}")

        if action == 'remove' and affected_ids:
            # Shares the tag made end with it (#570, #582)
            from src.services.auto_shares import drop_stale_auto_shares
            drop_stale_auto_shares(recording_ids=affected_ids)
        db.session.commit()

        return jsonify({
            'success': True,
            'affected_ids': affected_ids,
            'affected_count': len(affected_ids)
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in bulk tag update: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/bulk-reprocess', methods=['POST'])
@login_required
def bulk_reprocess():
    """Queue multiple recordings for reprocessing."""
    try:
        data = request.get_json()
        if not data:
            return jsonify(api_error('missing_request_body')), 400

        recording_ids = data.get('recording_ids', [])
        reprocess_type = data.get('type', 'summary')  # 'transcription' or 'summary'

        if not recording_ids:
            return jsonify(api_error('missing_recording_ids')), 400

        if reprocess_type not in ['transcription', 'summary']:
            return jsonify(api_error('invalid_reprocess_type')), 400

        if len(recording_ids) > 50:
            return jsonify(api_error('bulk_reprocess_limit')), 400

        queued_ids = []

        for recording_id in recording_ids:
            try:
                recording = db.session.get(Recording, recording_id)
                if not recording:
                    continue

                # Check ownership
                if recording.user_id != current_user.id:
                    continue

                # Only reprocess completed or failed recordings
                if recording.status not in ['COMPLETED', 'FAILED']:
                    continue

                # Same checks, clearing and settings as a single reprocess (#412).
                from src.services.reprocessing import (
                    queue_transcription_reprocess, queue_summary_reprocess, ReprocessError)
                try:
                    if reprocess_type == 'transcription':
                        queue_transcription_reprocess(recording, current_user)
                    else:
                        queue_summary_reprocess(recording, current_user)
                except ReprocessError:
                    continue

                queued_ids.append(recording_id)

            except Exception as e:
                current_app.logger.error(f"Error queueing reprocess for recording {recording_id}: {e}")

        db.session.commit()

        return jsonify({
            'success': True,
            'queued_ids': queued_ids,
            'queued_count': len(queued_ids)
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in bulk reprocess: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


@recordings_bp.route('/api/recordings/bulk-toggle', methods=['POST'])
@login_required
def bulk_toggle():
    """Toggle inbox or highlight for multiple recordings."""
    try:
        data = request.get_json()
        if not data:
            return jsonify(api_error('missing_request_body')), 400

        recording_ids = data.get('recording_ids', [])
        field = data.get('field')  # 'inbox', 'highlight' or 'archive'
        value = data.get('value')  # True or False

        if not recording_ids or field is None or value is None:
            return jsonify(api_error('missing_bulk_status_fields')), 400

        if field not in ['inbox', 'highlight', 'archive']:
            return jsonify(api_error('invalid_bulk_status_field')), 400

        if len(recording_ids) > 100:
            return jsonify(api_error('bulk_update_limit')), 400

        affected_ids = []

        for recording_id in recording_ids:
            try:
                recording = db.session.get(Recording, recording_id)
                if not recording:
                    continue

                # Authorization: only act on recordings the user actually has
                # access to. Without this, set_user_recording_status would
                # create a per-user status row for ANY recording id (treating
                # the caller as a "shared recipient"), leaking a cross-user
                # existence oracle and polluting the table. Mirrors the single
                # toggle_inbox / toggle_highlight endpoints.
                if not has_recording_access(recording, current_user):
                    continue

                # Use set_user_recording_status which handles both owners and shared users
                if field == 'inbox':
                    set_user_recording_status(recording, current_user, is_inbox=value)
                elif field == 'archive':
                    from src.services.recording_state import set_user_archived
                    set_user_archived(recording, current_user, value)
                else:
                    set_user_recording_status(recording, current_user, is_highlighted=value)

                affected_ids.append(recording_id)

            except Exception as e:
                current_app.logger.error(f"Error toggling {field} for recording {recording_id}: {e}")

        db.session.commit()

        return jsonify({
            'success': True,
            'affected_ids': affected_ids,
            'affected_count': len(affected_ids)
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error in bulk toggle: {e}", exc_info=True)
        return jsonify(api_error('unexpected_error')), 500


# --- Auto-deletion and Chunks Processing ---

@recordings_bp.route('/api/recordings/<int:recording_id>/toggle_deletion_exempt', methods=['POST'])
@login_required
def toggle_deletion_exempt(recording_id):
    """Toggle the deletion_exempt flag for a recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not (current_user.is_admin or has_recording_access(recording, current_user)):
            return jsonify(api_error('recording_not_found')), 404

        # Check ownership
        if recording.user_id != current_user.id and not current_user.is_admin:
            return jsonify(api_error('permission_denied')), 403

        # Toggle the flag
        recording.deletion_exempt = not recording.deletion_exempt
        db.session.commit()

        return jsonify({
            'success': True,
            'deletion_exempt': recording.deletion_exempt
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error toggling deletion exempt for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


@recordings_bp.route('/api/recording/<int:recording_id>/process_chunks', methods=['POST'])
@login_required
def process_recording_chunks_endpoint(recording_id):
    """Process chunks for a specific recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not has_recording_access(recording, current_user):
            return jsonify(api_error('recording_not_found')), 404

        if recording.user_id != current_user.id:
            return jsonify(api_error('permission_denied')), 403

        success = process_recording_chunks(recording_id)
        if success:
            return jsonify({'message': translate('server.apiMessage.chunksProcessed')})
        else:
            return jsonify(api_error('chunk_processing_failed')), 500

    except Exception as e:
        current_app.logger.error(f"Error in process chunks endpoint: {e}")
        return jsonify({'error': str(e)}), 500


# --- Inquire Mode API Endpoints ---


# Hot-applied System Settings (high priority) with environment fallback.
from src.utils.system_settings import apply_hot_settings
_SYSTEM_SETTING_MAP = {
    "ENABLE_AUTO_DELETION": ("enable_auto_deletion", "ENABLE_AUTO_DELETION", False),
    "DELETION_MODE": ("deletion_mode", "DELETION_MODE", "full_recording"),
    "USERS_CAN_DELETE": ("users_can_delete", "USERS_CAN_DELETE", True),
    "ENABLE_INTERNAL_SHARING": ("enable_internal_sharing", "ENABLE_INTERNAL_SHARING", False),
    "ENABLE_INCOGNITO_MODE": ("enable_incognito_mode", "ENABLE_INCOGNITO_MODE", False),
}
_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)
