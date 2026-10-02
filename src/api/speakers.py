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
from src.utils.ffmpeg_utils import extract_audio_segment, FFmpegError, FFmpegNotFoundError
from src.utils.ffprobe import get_codec_info, FFProbeError
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
        # Voice profile counts (samples, variants) from one query for all.
        from src.models import SpeakerVoiceSample
        from src.services.voice_profiles import voice_summary
        rows_by_speaker = {}
        for row in SpeakerVoiceSample.query.filter_by(user_id=current_user.id).all():
            rows_by_speaker.setdefault(row.speaker_id, []).append(row)
        out = []
        for speaker in speakers:
            item = speaker.to_dict()
            item['voice'] = voice_summary(speaker, rows_by_speaker.get(speaker.id, []))
            out.append(item)
        return jsonify(out)
    except Exception as e:
        current_app.logger.error(f"Error fetching speakers: {e}")
        return jsonify({'error': str(e)}), 500



@speakers_bp.route('/speakers/search', methods=['GET'])
@login_required
def search_speakers():
    """Search speakers by name for autocomplete functionality.

    With an empty ``q`` and ``top=1`` it returns the most-used speakers, so a
    name field can offer suggestions before anything is typed (#395).
    """
    try:
        query = request.args.get('q', '').strip()
        top = request.args.get('top', '').lower() in ('1', 'true')
        if not query and not top:
            return jsonify([])

        stmt = Speaker.query.filter_by(user_id=current_user.id)
        if query:
            stmt = stmt.filter(Speaker.name.ilike(f'%{query}%'))
        speakers = stmt.order_by(Speaker.use_count.desc(), Speaker.last_used.desc())\
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

        # Cascade the new name to every recording of this user
        from src.services.speaker_merge import rename_speaker_in_recordings, refresh_renamed_recordings
        chunks_updated, renamed = rename_speaker_in_recordings(current_user.id, old_name, new_name)
        recordings_updated = len(renamed)

        db.session.commit()
        refresh_renamed_recordings(renamed)

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
        # Bulk delete skips ORM cascades, so the voice samples go first.
        from src.models import SpeakerVoiceSample
        SpeakerVoiceSample.query.filter_by(user_id=current_user.id).delete()
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

        # Matching compares each voice with every voice variant of each
        # person, within the recording's embedding space. The threshold is
        # calibrated from the space's own samples once there are enough; a
        # ?threshold= query parameter still overrides it.
        from src.services.voice_profiles import suggestions_for_recording
        threshold = request.args.get('threshold', type=float)
        suggestions = suggestions_for_recording(recording, current_user.id, threshold=threshold)

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


@speakers_bp.route('/speakers/<int:speaker_id>/voice_samples', methods=['GET'])
@login_required
def list_voice_samples(speaker_id):
    """The samples a person's voice profile is built from, newest first."""
    from src.models import SpeakerVoiceSample
    from src.services.voice_profiles import voice_summary, effective_space, current_space_id
    speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
    if not speaker:
        return jsonify({'error': 'Speaker not found'}), 404
    rows = SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).all()
    titles = {}
    rec_ids = [r.recording_id for r in rows if r.recording_id]
    if rec_ids:
        for rec in Recording.query.filter(Recording.id.in_(rec_ids)).all():
            if has_recording_access(rec, current_user, require_edit=False):
                titles[rec.id] = rec.title
    current = effective_space(current_space_id())
    samples = []
    if not rows and speaker.average_embedding:
        # A profile from before samples existed: shown as one entry so it can
        # be seen and removed like any sample (removing it clears the profile).
        samples.append({'id': None, 'speaker_id': speaker.id, 'recording_id': None, 'label': None,
                        'source': 'legacy', 'weight': float(min(max(speaker.embedding_count or 1, 1), 5)),
                        'speech_seconds': None, 'space_id': None, 'recording_title': None,
                        'in_current_space': effective_space(None) == current,
                        'created_at': speaker.created_at.isoformat() if speaker.created_at else None})
    for r in sorted(rows, key=lambda r: r.updated_at or r.created_at, reverse=True):
        item = r.to_dict()
        item['recording_title'] = titles.get(r.recording_id)
        if r.recording_id not in titles:
            item['recording_id'] = None  # deleted, or no longer shared with this user
        item['in_current_space'] = effective_space(r.space_id) == current
        samples.append(item)
    return jsonify({'summary': voice_summary(speaker, rows), 'samples': samples})


@speakers_bp.route('/speakers/<int:speaker_id>/voice_samples/<int:sample_id>', methods=['DELETE'])
@login_required
def delete_voice_sample(speaker_id, sample_id):
    """Remove one sample; the person's voice variants are rebuilt without it."""
    from src.models import SpeakerVoiceSample
    from src.services import voice_profiles as vp
    speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
    if not speaker:
        return jsonify({'error': 'Speaker not found'}), 404
    sample = SpeakerVoiceSample.query.filter_by(id=sample_id, speaker_id=speaker.id).first()
    if not sample:
        return jsonify({'error': 'Sample not found'}), 404
    try:
        db.session.delete(sample)
        db.session.flush()
        vp.refresh_speaker_summary(speaker)
        vp._calibration_cache.clear()
        db.session.commit()
        return jsonify({'success': True, 'summary': vp.voice_summary(speaker),
                        'speaker': speaker.to_dict()})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error removing voice sample {sample_id}: {e}")
        return jsonify({'error': 'Could not remove the sample'}), 500


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

        # Clear all embeddings. NOTE: the matchable vector lives in the
        # `average_embedding` column (and the history in `embeddings_history`)
        # — there is NO `voice_embeddings` column. The previous code set a
        # phantom `voice_embeddings` attribute that never persisted, so the
        # real average_embedding survived and voice matching kept working
        # after a "clear". Null out the actual columns.
        from src.models import SpeakerVoiceSample
        SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).delete()
        speaker.average_embedding = None
        speaker.embeddings_history = None
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

        from src.services.storage import get_storage_service
        storage = get_storage_service()
        if not recording.audio_path or not storage.exists(recording.audio_path):
            return jsonify({'error': 'Audio file not found'}), 404

        try:
            with storage.materialize(recording.audio_path) as materialized:
                # Detect audio codec to pick the right output container for stream copy
                codec_to_container = {
                    'mp3': ('.mp3', 'audio/mpeg'),
                    'aac': ('.m4a', 'audio/mp4'),
                    'opus': ('.ogg', 'audio/ogg'),
                    'vorbis': ('.ogg', 'audio/ogg'),
                    'flac': ('.flac', 'audio/flac'),
                    'pcm_s16le': ('.wav', 'audio/wav'),
                    'pcm_s24le': ('.wav', 'audio/wav'),
                    'pcm_s32le': ('.wav', 'audio/wav'),
                    'pcm_f32le': ('.wav', 'audio/wav'),
                }
                snippet_ext = '.mp3'
                snippet_mime = 'audio/mpeg'
                try:
                    codec_info = get_codec_info(materialized.local_path, timeout=10)
                    audio_codec = codec_info.get('audio_codec')
                    if audio_codec and audio_codec in codec_to_container:
                        snippet_ext, snippet_mime = codec_to_container[audio_codec]
                except FFProbeError:
                    pass  # Fall back to mp3

                # Create temporary file for the snippet
                with tempfile.NamedTemporaryFile(delete=False, suffix=snippet_ext) as tmp_file:
                    output_path = tmp_file.name

                # Use centralized FFmpeg utility to extract the audio segment
                extract_audio_segment(
                    materialized.local_path,
                    output_path,
                    start_time,
                    duration
                )

            # Send the file
            response = send_file(
                output_path,
                mimetype=snippet_mime,
                as_attachment=False,
                download_name=f'snippet_{recording_id}_{start_time:.1f}s{snippet_ext}'
            )

            # Clean up temporary file after sending
            @response.call_on_close
            def cleanup():
                try:
                    os.unlink(output_path)
                except:
                    pass

            return response

        except FFmpegNotFoundError as e:
            current_app.logger.error(f"FFmpeg not found: {e}")
            try:
                os.unlink(output_path)
            except:
                pass
            return jsonify({'error': 'FFmpeg not found on server'}), 500
        except FFmpegError as e:
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


