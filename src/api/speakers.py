"""
Speaker identification and management.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import time
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *
from src.services.speaker_embedding_matcher import find_matching_speakers
from src.services.speaker_snippets import get_speaker_snippets, get_speaker_recordings_with_snippets
from src.services.speaker_merge import merge_speakers, preview_merge, can_merge_speakers

# Create blueprint
speakers_bp = Blueprint('speakers', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_speakers_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


# --- Routes ---

@speakers_bp.route('/speakers', methods=['GET'])
@login_required
def get_speakers():
    """Get all speakers for the current user, ordered by usage frequency and recency."""
    try:
        speakers = Speaker.query.filter_by(user_id=current_user.id)\
                               .order_by(Speaker.use_count.desc(), Speaker.last_used.desc())\
                               .all()
        return jsonify([speaker.to_dict() for speaker in speakers])
    except Exception as e:
        current_app.logger.error(f"Error fetching speakers: {e}")
        return jsonify({'error': str(e)}), 500



@speakers_bp.route('/speakers/search', methods=['GET'])
@login_required
def search_speakers():
    """Search speakers by name for autocomplete functionality."""
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify([])
        
        speakers = Speaker.query.filter_by(user_id=current_user.id)\
                               .filter(Speaker.name.ilike(f'%{query}%'))\
                               .order_by(Speaker.use_count.desc(), Speaker.last_used.desc())\
                               .limit(10)\
                               .all()
        
        return jsonify([speaker.to_dict() for speaker in speakers])
    except Exception as e:
        current_app.logger.error(f"Error searching speakers: {e}")
        return jsonify({'error': str(e)}), 500



@speakers_bp.route('/speakers', methods=['POST'])
@login_required
def create_speaker():
    """Create a new speaker or update existing one."""
    try:
        data = request.json
        name = data.get('name', '').strip()
        
        if not name:
            return jsonify({'error': 'Speaker name is required'}), 400
        
        # Check if speaker already exists for this user
        existing_speaker = Speaker.query.filter_by(user_id=current_user.id, name=name).first()
        
        if existing_speaker:
            # Update usage statistics
            existing_speaker.use_count += 1
            existing_speaker.last_used = datetime.utcnow()
            db.session.commit()
            return jsonify(existing_speaker.to_dict())
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
            return jsonify(speaker.to_dict()), 201
            
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating speaker: {e}")
        return jsonify({'error': str(e)}), 500



@speakers_bp.route('/speakers/<int:speaker_id>', methods=['PUT'])
@login_required
def update_speaker(speaker_id):
    """Update a speaker's name and cascade the change to all recordings."""
    try:
        speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
        if not speaker:
            return jsonify({'error': 'Speaker not found'}), 404

        data = request.json
        new_name = data.get('name', '').strip()

        if not new_name:
            return jsonify({'error': 'Speaker name cannot be empty'}), 400

        # Check if another speaker with this name already exists for this user
        existing_speaker = Speaker.query.filter_by(user_id=current_user.id, name=new_name).first()
        if existing_speaker and existing_speaker.id != speaker_id:
            return jsonify({'error': f'A speaker named "{new_name}" already exists'}), 400

        # Store old name for updating transcript chunks and recordings
        old_name = speaker.name

        # Update the speaker name
        speaker.name = new_name

        # Update all transcript chunks that reference this speaker's old name
        # This ensures the name change cascades to all recordings
        from src.models import TranscriptChunk
        chunks_updated = TranscriptChunk.query.filter_by(
            user_id=current_user.id,
            speaker_name=old_name
        ).update({'speaker_name': new_name})

        # Update Recording.participants field (comma-separated list of speakers)
        # AND update speaker names in the transcription JSON
        recordings_updated = 0
        user_recordings = Recording.query.filter_by(user_id=current_user.id).all()

        for recording in user_recordings:
            updated = False

            # Update participants field if it contains the old name
            if recording.participants and old_name in recording.participants:
                # Replace exact speaker name matches in participants list
                # Handle various formats: "Ross", "Ross, John", "John, Ross", etc.
                participants_list = [p.strip() for p in recording.participants.split(',')]
                if old_name in participants_list:
                    # Replace the old name with new name
                    participants_list = [new_name if p == old_name else p for p in participants_list]
                    recording.participants = ', '.join(participants_list)
                    updated = True

            # Update speaker names in the transcription JSON
            # This is what displays in the transcript view speaker badges
            if recording.transcription:
                try:
                    transcription_data = json.loads(recording.transcription)

                    # Handle JSON format (array of segments with speaker field)
                    if isinstance(transcription_data, list):
                        segments_updated = False
                        for segment in transcription_data:
                            if segment.get('speaker') == old_name:
                                segment['speaker'] = new_name
                                segments_updated = True

                        if segments_updated:
                            recording.transcription = json.dumps(transcription_data)
                            updated = True
                except (json.JSONDecodeError, TypeError):
                    # Not JSON or invalid format, skip
                    pass

            if updated:
                recordings_updated += 1

        db.session.commit()

        current_app.logger.info(
            f"Updated speaker {speaker_id} from '{old_name}' to '{new_name}': "
            f"{chunks_updated} transcript chunks, {recordings_updated} recordings"
        )

        return jsonify({
            'success': True,
            'speaker': speaker.to_dict(),
            'chunks_updated': chunks_updated,
            'recordings_updated': recordings_updated
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating speaker: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/<int:speaker_id>', methods=['DELETE'])
@login_required
def delete_speaker(speaker_id):
    """Delete a speaker."""
    try:
        speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
        if not speaker:
            return jsonify({'error': 'Speaker not found'}), 404

        db.session.delete(speaker)
        db.session.commit()
        return jsonify({'success': True})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting speaker: {e}")
        return jsonify({'error': str(e)}), 500



@speakers_bp.route('/speakers/delete_all', methods=['DELETE'])
@login_required
def delete_all_speakers():
    """Delete all speakers for the current user."""
    try:
        deleted_count = Speaker.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        return jsonify({'success': True, 'deleted_count': deleted_count})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting all speakers: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/suggestions/<int:recording_id>', methods=['GET'])
@login_required
def get_speaker_suggestions(recording_id):
    """
    Get speaker suggestions based on voice embeddings from a recording.

    For each speaker in the recording, returns matching speakers from the user's
    speaker database based on voice similarity.

    Returns:
        {
            'SPEAKER_00': [
                {'speaker_id': 5, 'name': 'John', 'similarity': 85.3, 'confidence': 0.92},
                ...
            ],
            'SPEAKER_01': [...],
            ...
        }
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user, require_edit=False):
            return jsonify({'error': 'You do not have permission to access this recording'}), 403

        # Get speaker embeddings from recording
        if not recording.speaker_embeddings:
            return jsonify({'suggestions': {}, 'message': 'No speaker embeddings available'}), 200

        try:
            embeddings_data = json.loads(recording.speaker_embeddings) if isinstance(recording.speaker_embeddings, str) else recording.speaker_embeddings
        except (json.JSONDecodeError, TypeError):
            return jsonify({'error': 'Invalid speaker embeddings data'}), 500

        # Get similarity threshold from query params (default 70%)
        threshold = float(request.args.get('threshold', 0.70))

        # Find matches for each speaker
        suggestions = {}
        for speaker_label, embedding in embeddings_data.items():
            if embedding and len(embedding) == 256:  # Validate embedding dimension
                matches = find_matching_speakers(embedding, current_user.id, threshold)
                suggestions[speaker_label] = matches
            else:
                suggestions[speaker_label] = []

        return jsonify({
            'success': True,
            'suggestions': suggestions,
            'recording_id': recording_id
        })

    except Exception as e:
        current_app.logger.error(f"Error getting speaker suggestions: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/<int:speaker_id>/snippets', methods=['GET'])
@login_required
def get_snippets(speaker_id):
    """
    Get representative speech snippets for a speaker.

    Returns recent quotes from recordings where this speaker appeared.
    """
    try:
        # Verify speaker belongs to user
        speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
        if not speaker:
            return jsonify({'error': 'Speaker not found'}), 404

        limit = int(request.args.get('limit', 5))
        snippets = get_speaker_snippets(speaker_id, limit)

        return jsonify({
            'success': True,
            'speaker_id': speaker_id,
            'speaker_name': speaker.name,
            'snippets': snippets
        })

    except Exception as e:
        current_app.logger.error(f"Error getting speaker snippets: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/<int:speaker_id>/recordings', methods=['GET'])
@login_required
def get_speaker_recordings(speaker_id):
    """
    Get list of recordings that contain snippets from this speaker.

    Returns recording metadata with snippet counts.
    """
    try:
        # Verify speaker belongs to user
        speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
        if not speaker:
            return jsonify({'error': 'Speaker not found'}), 404

        recordings = get_speaker_recordings_with_snippets(speaker_id)

        return jsonify({
            'success': True,
            'speaker_id': speaker_id,
            'speaker_name': speaker.name,
            'recordings': recordings
        })

    except Exception as e:
        current_app.logger.error(f"Error getting speaker recordings: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/<int:speaker_id>/clear_embeddings', methods=['POST'])
@login_required
def clear_speaker_embeddings(speaker_id):
    """
    Clear all voice embeddings for a speaker.

    This removes all voice recognition data but keeps the speaker name and metadata.
    Useful for resetting voice profiles or removing outdated/incorrect voice data.
    """
    try:
        # Verify speaker belongs to user
        speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
        if not speaker:
            return jsonify({'error': 'Speaker not found'}), 404

        # Clear all embeddings
        speaker.voice_embeddings = None
        speaker.embedding_count = 0
        speaker.confidence_score = None

        db.session.commit()

        current_app.logger.info(f"Cleared voice embeddings for speaker {speaker_id} ({speaker.name})")

        return jsonify({
            'success': True,
            'message': f'Voice profile cleared for {speaker.name}',
            'speaker': {
                'id': speaker.id,
                'name': speaker.name,
                'embedding_count': 0
            }
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error clearing speaker embeddings: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/snippet-audio/<int:recording_id>', methods=['GET'])
@login_required
def get_snippet_audio(recording_id):
    """
    Serve a short audio snippet from a recording.

    Query parameters:
        start: Start time in seconds (float)
        duration: Duration in seconds (float, max 5.0)

    Returns:
        Audio file segment in the original format
    """
    import subprocess
    import tempfile
    import os
    from pathlib import Path

    try:
        # Get query parameters
        start_time = float(request.args.get('start', 0))
        duration = min(float(request.args.get('duration', 4.0)), 5.0)  # Max 5 seconds

        # Get the recording
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user, require_edit=False):
            return jsonify({'error': 'You do not have permission to access this recording'}), 403

        if recording.audio_deleted_at:
            return jsonify({'error': 'Audio file has been deleted'}), 410

        if not recording.audio_path or not os.path.exists(recording.audio_path):
            return jsonify({'error': 'Audio file not found'}), 404

        # Create temporary file for the snippet
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp_file:
            output_path = tmp_file.name

        try:
            # Use FFmpeg to extract the audio segment
            ffmpeg_cmd = [
                'ffmpeg',
                '-i', recording.audio_path,
                '-ss', str(start_time),
                '-t', str(duration),
                '-c', 'copy',
                '-y',
                output_path
            ]

            subprocess.run(ffmpeg_cmd, check=True, capture_output=True)

            # Send the file
            response = send_file(
                output_path,
                mimetype='audio/mpeg',
                as_attachment=False,
                download_name=f'snippet_{recording_id}_{start_time:.1f}s.mp3'
            )

            # Clean up temporary file after sending
            @response.call_on_close
            def cleanup():
                try:
                    os.unlink(output_path)
                except:
                    pass

            return response

        except subprocess.CalledProcessError as e:
            current_app.logger.error(f"FFmpeg error extracting snippet: {e}")
            try:
                os.unlink(output_path)
            except:
                pass
            return jsonify({'error': 'Failed to extract audio snippet'}), 500

    except ValueError:
        return jsonify({'error': 'Invalid start time or duration'}), 400
    except Exception as e:
        current_app.logger.error(f"Error serving audio snippet: {e}")
        return jsonify({'error': str(e)}), 500


@speakers_bp.route('/speakers/merge', methods=['POST'])
@login_required
def merge_speaker_profiles():
    """
    Merge multiple speaker profiles into one.

    Request body:
        {
            'target_id': 5,  # Speaker to keep
            'source_ids': [6, 7, 8],  # Speakers to merge into target
            'preview': false  # Optional: if true, just preview without executing
        }

    Returns merged speaker data or preview statistics.
    """
    try:
        data = request.json
        target_id = data.get('target_id')
        source_ids = data.get('source_ids', [])
        preview = data.get('preview', False)

        if not target_id:
            return jsonify({'error': 'target_id is required'}), 400

        if not source_ids or not isinstance(source_ids, list):
            return jsonify({'error': 'source_ids must be a non-empty list'}), 400

        # Validate speakers can be merged
        can_merge, error_msg = can_merge_speakers([target_id] + source_ids, current_user.id)
        if not can_merge:
            return jsonify({'error': error_msg}), 400

        if preview:
            # Just return preview statistics
            preview_data = preview_merge(target_id, source_ids, current_user.id)
            return jsonify({
                'success': True,
                'preview': preview_data
            })
        else:
            # Execute the merge
            merged_speaker = merge_speakers(target_id, source_ids, current_user.id)
            return jsonify({
                'success': True,
                'message': f'Successfully merged {len(source_ids)} speaker(s) into {merged_speaker.name}',
                'speaker': merged_speaker.to_dict()
            })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error merging speakers: {e}")
        return jsonify({'error': str(e)}), 500



