"""
Sharing routes for public and internal recording shares.

This blueprint handles:
- Public sharing (shareable links)
- Internal sharing (user-to-user sharing)
- Share management (CRUD operations)
"""

import os
import re
import json
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file, current_app
from flask_login import login_required, current_user

from src.database import db
from src.models import Recording, Share, InternalShare, SharedRecordingState, User, TranscriptChunk, ShareAuditLog
from src.utils import md_to_html

# Configuration from environment
ENABLE_PUBLIC_SHARING = os.environ.get('ENABLE_PUBLIC_SHARING', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
SHOW_USERNAMES_IN_UI = os.environ.get('SHOW_USERNAMES_IN_UI', 'false').lower() == 'true'
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
READABLE_PUBLIC_LINKS = os.environ.get('READABLE_PUBLIC_LINKS', 'false').lower() == 'true'

# Create blueprint
shares_bp = Blueprint('shares', __name__)

# Import has_recording_access from app context
has_recording_access = None

def init_shares_helpers(_has_recording_access):
    """Initialize helper functions from app."""
    global has_recording_access
    has_recording_access = _has_recording_access


def process_transcription_for_template(transcription_str):
    """
    Process transcription JSON into a format ready for server-side rendering.

    Returns a dict with:
    - is_json: bool - whether transcription is valid JSON
    - has_speakers: bool - whether diarization data exists
    - segments: list - processed segments with speaker info and colors
    - speakers: list - unique speakers with colors
    - plain_text: str - plain text version for non-JSON or fallback
    """
    if not transcription_str:
        return {'is_json': False, 'has_speakers': False, 'segments': [], 'speakers': [], 'plain_text': ''}

    try:
        data = json.loads(transcription_str)
    except (json.JSONDecodeError, TypeError):
        # Plain text transcription
        return {
            'is_json': False,
            'has_speakers': False,
            'segments': [],
            'speakers': [],
            'plain_text': transcription_str
        }

    if not isinstance(data, list):
        return {
            'is_json': False,
            'has_speakers': False,
            'segments': [],
            'speakers': [],
            'plain_text': transcription_str
        }

    # Check if diarized (has speaker info)
    has_speakers = any(seg.get('speaker') for seg in data)

    # Get unique speakers and assign colors
    speakers = []
    speaker_colors = {}
    if has_speakers:
        unique_speakers = list(dict.fromkeys(seg.get('speaker') for seg in data if seg.get('speaker')))
        for i, speaker in enumerate(unique_speakers):
            color = f'speaker-color-{(i % 8) + 1}'
            speaker_colors[speaker] = color
            speakers.append({'name': speaker, 'color': color})

    # Process segments
    segments = []
    last_speaker = None
    for seg in data:
        speaker = seg.get('speaker', '')
        segment = {
            'text': seg.get('sentence', ''),
            'speaker': speaker,
            'start_time': seg.get('start_time') or seg.get('startTime', ''),
            'end_time': seg.get('end_time') or seg.get('endTime', ''),
            'color': speaker_colors.get(speaker, 'speaker-color-1'),
            'show_speaker': speaker != last_speaker
        }
        segments.append(segment)
        last_speaker = speaker

    # Build plain text version
    if has_speakers:
        plain_text = '\n'.join(f"[{seg['speaker']}]: {seg['text']}" for seg in segments)
    else:
        plain_text = '\n'.join(seg['text'] for seg in segments)

    return {
        'is_json': True,
        'has_speakers': has_speakers,
        'segments': segments,
        'speakers': speakers,
        'plain_text': plain_text
    }


# --- Public Sharing Routes ---

@shares_bp.route('/share/<string:public_id>', methods=['GET'])
def view_shared_recording(public_id):
    """View a publicly shared recording."""
    share = Share.query.filter_by(public_id=public_id).first_or_404()
    recording = share.recording

    # Process transcription for server-side rendering (only if READABLE_PUBLIC_LINKS is enabled)
    processed_transcript = None
    if READABLE_PUBLIC_LINKS:
        processed_transcript = process_transcription_for_template(recording.transcription)

    # Create a limited dictionary for the public view
    recording_data = {
        'id': recording.id,
        'public_id': share.public_id,
        'title': recording.title,
        'participants': recording.participants,
        'transcription': recording.transcription,
        'summary': md_to_html(recording.summary) if share.share_summary else None,
        'summary_raw': recording.summary if share.share_summary else None,
        'notes': md_to_html(recording.notes) if share.share_notes else None,
        'notes_raw': recording.notes if share.share_notes else None,
        'meeting_date': f"{recording.meeting_date.isoformat()}T00:00:00" if recording.meeting_date else None,
        'mime_type': recording.mime_type,
        'audio_deleted_at': recording.audio_deleted_at.isoformat() if recording.audio_deleted_at else None,
        'audio_duration': recording.get_audio_duration()
    }

    return render_template('share.html', recording=recording_data, transcript=processed_transcript, readable_mode=READABLE_PUBLIC_LINKS)


@shares_bp.route('/share/audio/<string:public_id>')
def get_shared_audio(public_id):
    """Serve audio file for a publicly shared recording."""
    try:
        share = Share.query.filter_by(public_id=public_id).first_or_404()
        recording = share.recording
        if not recording or not recording.audio_path:
            return jsonify({'error': 'Recording or audio file not found'}), 404
        if not os.path.exists(recording.audio_path):
            current_app.logger.error(f"Audio file missing from server: {recording.audio_path}")
            return jsonify({'error': 'Audio file missing from server'}), 404
        return send_file(recording.audio_path)
    except Exception as e:
        current_app.logger.error(f"Error serving shared audio for public_id {public_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred.'}), 500


@shares_bp.route('/api/recording/<int:recording_id>/share', methods=['GET'])
@login_required
def get_existing_share(recording_id):
    """Check if a share already exists for this recording."""
    recording = db.session.get(Recording, recording_id)
    if not recording or recording.user_id != current_user.id:
        return jsonify({'error': 'Recording not found or you do not have permission to view it.'}), 404

    existing_share = Share.query.filter_by(
        recording_id=recording.id,
        user_id=current_user.id
    ).order_by(Share.created_at.desc()).first()

    if existing_share:
        share_url = url_for('shares.view_shared_recording', public_id=existing_share.public_id, _external=True)
        return jsonify({
            'success': True,
            'exists': True,
            'share_url': share_url,
            'share': existing_share.to_dict()
        }), 200
    else:
        return jsonify({
            'success': True,
            'exists': False
        }), 200


@shares_bp.route('/api/recording/<int:recording_id>/share', methods=['POST'])
@login_required
def create_share(recording_id):
    """Create a public share link for a recording."""
    # Check if public sharing is globally enabled
    if not ENABLE_PUBLIC_SHARING:
        return jsonify({'error': 'Public sharing is not enabled on this server'}), 403

    # Check if user has permission to create public shares
    if not current_user.can_share_publicly:
        return jsonify({'error': 'You do not have permission to create public share links. Contact your administrator.'}), 403

    if not request.is_secure:
        return jsonify({'error': 'Sharing is only available over a secure (HTTPS) connection.'}), 403

    recording = db.session.get(Recording, recording_id)
    if not recording or recording.user_id != current_user.id:
        return jsonify({'error': 'Recording not found or you do not have permission to share it.'}), 404

    data = request.json
    share_summary = data.get('share_summary', True)
    share_notes = data.get('share_notes', True)
    force_new = data.get('force_new', False)

    # Check if ANY share already exists for this recording by this user
    existing_share = Share.query.filter_by(
        recording_id=recording.id,
        user_id=current_user.id
    ).order_by(Share.created_at.desc()).first()

    if existing_share and not force_new:
        # Update the share permissions if they've changed
        if existing_share.share_summary != share_summary or existing_share.share_notes != share_notes:
            existing_share.share_summary = share_summary
            existing_share.share_notes = share_notes
            db.session.commit()

        # Return existing share info
        share_url = url_for('shares.view_shared_recording', public_id=existing_share.public_id, _external=True)
        return jsonify({
            'success': True,
            'share_url': share_url,
            'share': existing_share.to_dict(),
            'existing': True,
            'message': 'Using existing share link for this recording'
        }), 200

    # Create new share
    share = Share(
        recording_id=recording.id,
        user_id=current_user.id,
        share_summary=share_summary,
        share_notes=share_notes
    )
    db.session.add(share)
    db.session.commit()

    share_url = url_for('shares.view_shared_recording', public_id=share.public_id, _external=True)

    return jsonify({
        'success': True,
        'share_url': share_url,
        'share': share.to_dict(),
        'existing': False
    }), 201


@shares_bp.route('/api/shares', methods=['GET'])
@login_required
def get_shares():
    """Get all public shares for the current user."""
    shares = Share.query.filter_by(user_id=current_user.id).order_by(Share.created_at.desc()).all()
    return jsonify([share.to_dict() for share in shares])


@shares_bp.route('/api/share/<int:share_id>', methods=['PUT'])
@login_required
def update_share(share_id):
    """Update a public share's settings."""
    share = Share.query.filter_by(id=share_id, user_id=current_user.id).first_or_404()
    data = request.json

    if 'share_summary' in data:
        share.share_summary = data['share_summary']
    if 'share_notes' in data:
        share.share_notes = data['share_notes']

    db.session.commit()
    return jsonify({'success': True, 'share': share.to_dict()})


@shares_bp.route('/api/share/<int:share_id>', methods=['DELETE'])
@login_required
def delete_share(share_id):
    """Delete a public share."""
    share = Share.query.filter_by(id=share_id, user_id=current_user.id).first_or_404()
    db.session.delete(share)
    db.session.commit()
    return jsonify({'success': True})


# --- Internal Sharing Routes ---

@shares_bp.route('/api/users/search', methods=['GET'])
@login_required
def search_users():
    """Search for users by username (for internal sharing)."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Internal sharing is not enabled'}), 403

    query = request.args.get('q', '').strip()

    # If SHOW_USERNAMES_IN_UI is enabled and no query, return all users for quick selection
    if SHOW_USERNAMES_IN_UI and len(query) < 2:
        users = User.query.filter(User.id != current_user.id).order_by(User.username).all()
    elif len(query) < 2:
        # If usernames are hidden and no query, return empty
        return jsonify([])
    else:
        if SHOW_USERNAMES_IN_UI:
            # If usernames are shown, allow partial match (autocomplete)
            users = User.query.filter(
                User.id != current_user.id,
                User.username.ilike(f'%{query}%')
            ).limit(10).all()
        else:
            # If usernames are hidden (privacy mode), require exact match only
            users = User.query.filter(
                User.id != current_user.id,
                User.username == query
            ).all()

    return jsonify([{
        'id': user.id,
        'username': user.username,
        'email': user.email if SHOW_USERNAMES_IN_UI else None
    } for user in users])


@shares_bp.route('/api/recordings/<int:recording_id>/share-internal', methods=['POST'])
@login_required
def share_recording_internal(recording_id):
    """Share a recording with another user internally."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Internal sharing is not enabled'}), 403

    try:
        data = request.json
        shared_with_user_id = data.get('user_id')
        can_edit = data.get('can_edit', False)
        can_reshare = data.get('can_reshare', False)

        if not shared_with_user_id:
            return jsonify({'error': 'User ID is required'}), 400

        # Check recording exists and user has permission to share it
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user, require_reshare=True):
            return jsonify({'error': 'You do not have permission to share this recording'}), 403

        # Check target user exists
        target_user = db.session.get(User, shared_with_user_id)
        if not target_user:
            return jsonify({'error': 'Target user not found'}), 404

        # Prevent sharing back to owner (circular share)
        if shared_with_user_id == recording.user_id:
            return jsonify({'error': 'Cannot share a recording with its owner'}), 400

        # Prevent sharing with self
        if shared_with_user_id == current_user.id:
            return jsonify({'error': 'Cannot share a recording with yourself'}), 400

        # Check if already shared
        existing_share = InternalShare.query.filter_by(
            recording_id=recording_id,
            shared_with_user_id=shared_with_user_id
        ).first()

        if existing_share:
            return jsonify({'error': 'Recording already shared with this user'}), 409

        # PERMISSION VALIDATION: Validate that current user can grant the requested permissions
        requested_permissions = {'can_edit': can_edit, 'can_reshare': can_reshare}
        is_valid, error_message = InternalShare.validate_reshare_permissions(
            recording, current_user, requested_permissions
        )

        if not is_valid:
            return jsonify({'error': error_message}), 403

        # Get current user's permissions for audit log
        actor_permissions = InternalShare.get_user_max_permissions(recording, current_user)

        # Create share
        share = InternalShare(
            recording_id=recording_id,
            owner_id=current_user.id,
            shared_with_user_id=shared_with_user_id,
            can_edit=can_edit,
            can_reshare=can_reshare
        )
        db.session.add(share)

        # Create or update SharedRecordingState for the recipient
        state = SharedRecordingState.query.filter_by(
            recording_id=recording_id,
            user_id=shared_with_user_id
        ).first()

        if not state:
            # Create new state if it doesn't exist
            state = SharedRecordingState(
                recording_id=recording_id,
                user_id=shared_with_user_id,
                is_inbox=True,  # New shares appear in inbox by default
                is_highlighted=False  # Not favorited by default
            )
            db.session.add(state)
        else:
            # Reset to inbox if it already exists (e.g., from previous share that was deleted)
            state.is_inbox = True

        db.session.commit()

        # AUDIT LOGGING: Log the share creation
        try:
            ShareAuditLog.log_share_created(
                recording_id=recording_id,
                actor_id=current_user.id,
                target_user_id=shared_with_user_id,
                permissions={'can_edit': can_edit, 'can_reshare': can_reshare},
                actor_permissions=actor_permissions,
                notes=f"Shared by {'owner' if recording.user_id == current_user.id else 'delegated user'}",
                ip_address=request.remote_addr
            )
            db.session.commit()
        except Exception as audit_error:
            # Don't fail the share if audit logging fails
            current_app.logger.error(f"Failed to log share creation: {audit_error}")

        return jsonify({
            'success': True,
            'share': share.to_dict()
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error sharing recording internally: {e}")
        return jsonify({'error': str(e)}), 500


@shares_bp.route('/api/recordings/<int:recording_id>/shares-internal', methods=['GET'])
@login_required
def get_internal_shares(recording_id):
    """Get list of users a recording is shared with, including owner."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Internal sharing is not enabled'}), 403

    # Check recording exists and user has permission to view shares
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_reshare=True):
        return jsonify({'error': 'You do not have permission to view shares for this recording'}), 403

    # Get all internal shares
    shares = InternalShare.query.filter_by(recording_id=recording_id).all()
    shares_list = [share.to_dict() for share in shares]

    # Add owner as first entry (owner always has full permissions)
    owner = db.session.get(User, recording.user_id)
    if owner:
        owner_entry = {
            'id': None,  # No share ID for owner
            'recording_id': recording_id,
            'owner_id': owner.id,
            'owner_username': owner.username,
            'user_id': owner.id,
            'username': owner.username,
            'can_edit': True,
            'can_reshare': True,
            'is_owner': True,  # Mark as owner
            'source_type': 'owner',
            'source_tag_id': None,
            'created_at': recording.created_at.isoformat() if recording.created_at else None
        }
        # Insert owner at the beginning
        shares_list.insert(0, owner_entry)

    return jsonify({'shares': shares_list})


@shares_bp.route('/api/internal-shares/<int:share_id>', methods=['DELETE'])
@login_required
def revoke_internal_share(share_id):
    """Revoke an internal share with cascade revocation."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Internal sharing is not enabled'}), 403

    share = db.session.get(InternalShare, share_id)
    if not share:
        return jsonify({'error': 'Share not found'}), 404

    # Only owner can revoke
    if share.owner_id != current_user.id:
        return jsonify({'error': 'You do not have permission to revoke this share'}), 403

    recording_id = share.recording_id
    revoked_user_id = share.shared_with_user_id
    revoked_count = 0

    try:
        # CASCADE REVOCATION: Find downstream shares created by the user losing access
        downstream_shares = InternalShare.find_downstream_shares(recording_id, revoked_user_id)

        # Recursively revoke downstream shares that don't have alternate paths
        for downstream in downstream_shares:
            # Check for alternate access paths (diamond pattern protection)
            has_alternate = InternalShare.has_alternate_access_path(
                recording_id,
                downstream.shared_with_user_id,
                excluding_grantor_id=revoked_user_id
            )

            if not has_alternate:
                # No alternate path - cascade revoke
                # Audit log cascade revocation
                try:
                    ShareAuditLog.log_share_revoked(
                        share_id=downstream.id,
                        recording_id=recording_id,
                        actor_id=current_user.id,
                        target_user_id=downstream.shared_with_user_id,
                        was_cascade=True,
                        notes=f"Cascaded from revoking user {revoked_user_id}",
                        ip_address=request.remote_addr
                    )
                except Exception as audit_error:
                    current_app.logger.error(f"Failed to log cascade revocation: {audit_error}")

                db.session.delete(downstream)
                revoked_count += 1

        # Audit log the primary revocation
        try:
            ShareAuditLog.log_share_revoked(
                share_id=share.id,
                recording_id=recording_id,
                actor_id=current_user.id,
                target_user_id=revoked_user_id,
                was_cascade=False,
                notes=f"Revoked by user {current_user.id}, cascaded to {revoked_count} downstream shares",
                ip_address=request.remote_addr
            )
        except Exception as audit_error:
            current_app.logger.error(f"Failed to log revocation: {audit_error}")

        # Delete the primary share
        db.session.delete(share)
        db.session.commit()

        return jsonify({
            'success': True,
            'revoked_count': revoked_count + 1,  # Include primary share
            'cascaded': revoked_count
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error revoking internal share: {e}")
        return jsonify({'error': str(e)}), 500


@shares_bp.route('/api/recordings/shared-with-me', methods=['GET'])
@login_required
def get_shared_with_me():
    """Get recordings that have been shared with the current user."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Internal sharing is not enabled'}), 403

    try:
        # Get shares where current user is the recipient
        shares = InternalShare.query.filter_by(shared_with_user_id=current_user.id).all()

        result = []
        for share in shares:
            recording = share.recording
            if recording and recording.status == 'COMPLETED':
                rec_data = recording.to_list_dict(viewer_user=current_user)
                # Mark as shared recording with owner info
                rec_data['is_shared'] = True
                rec_data['owner_username'] = share.owner.username if SHOW_USERNAMES_IN_UI else None
                # Don't show outgoing share counts for recordings you don't own
                rec_data['shared_with_count'] = 0
                rec_data['public_share_count'] = 0
                # Check if recording has group tags (among visible tags)
                visible_tags = recording.get_visible_tags(current_user)
                rec_data['has_group_tags'] = any(tag.is_group_tag for tag in visible_tags) if visible_tags else False
                rec_data['share_info'] = {
                    'share_id': share.id,
                    'owner_username': share.owner.username if SHOW_USERNAMES_IN_UI else None,
                    'can_edit': share.can_edit,
                    'can_reshare': share.can_reshare,
                    'shared_at': share.created_at.isoformat()
                }
                result.append(rec_data)

        return jsonify(result)

    except Exception as e:
        current_app.logger.error(f"Error fetching shared recordings: {e}")
        return jsonify({'error': str(e)}), 500


@shares_bp.route('/api/permissions/can-share-publicly', methods=['GET'])
@login_required
def can_share_publicly():
    """Check if the current user has permission to create public shares."""
    return jsonify({
        'can_share_publicly': current_user.can_share_publicly and ENABLE_PUBLIC_SHARING
    })
