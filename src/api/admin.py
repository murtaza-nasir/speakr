"""
Administrative functions and user management.

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
from src.services.retention import is_recording_exempt_from_deletion, get_retention_days_for_recording, process_auto_deletion
from src.services.embeddings import EMBEDDINGS_AVAILABLE, process_recording_chunks
from src.config.startup import get_file_monitor_functions

# Create blueprint
admin_bp = Blueprint('admin', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
GLOBAL_RETENTION_DAYS = int(os.environ.get('GLOBAL_RETENTION_DAYS', '0'))
DELETION_MODE = os.environ.get('DELETION_MODE', 'hard')

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_admin_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


def csrf_exempt(f):
    """Decorator placeholder for CSRF exemption - applied after initialization."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    wrapper._csrf_exempt = True
    return wrapper


# --- Routes ---

@admin_bp.route('/admin', methods=['GET'])
@login_required
def admin():
    # Check if user is admin OR group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_team_admin:
        flash('You do not have permission to access the admin page.', 'danger')
        return redirect(url_for('recordings.index'))

    # Redirect group admins to their dedicated management page
    if is_team_admin and not current_user.is_admin:
        return redirect(url_for('admin.group_management'))

    # Full admins only get here
    user_language = current_user.ui_language if current_user.is_authenticated and current_user.ui_language else 'en'
    return render_template('admin.html',
                         title='Admin Dashboard',
                         inquire_mode_enabled=ENABLE_INQUIRE_MODE,
                         global_retention_days=GLOBAL_RETENTION_DAYS,
                         is_group_admin_only=False,
                         user_language=user_language)


@admin_bp.route('/group-management', methods=['GET'])
@login_required
def group_management():
    """Dedicated group management page for group admins (non-full admins)."""
    # Check if user is a group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not is_team_admin:
        flash('You do not have permission to access group management.', 'danger')
        return redirect(url_for('recordings.index'))

    # If they're a full admin, redirect to main admin dashboard
    if current_user.is_admin:
        return redirect(url_for('admin.admin'))

    user_language = current_user.ui_language if current_user.is_authenticated and current_user.ui_language else 'en'
    return render_template('group-admin.html',
                         title='Group Management',
                         global_retention_days=GLOBAL_RETENTION_DAYS,
                         user_language=user_language)



@admin_bp.route('/admin/users', methods=['GET'])
@login_required
def admin_get_users():
    # Check if user is admin OR group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_team_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    users = User.query.all()
    user_data = []
    
    for user in users:
        # Get recordings count and storage used
        recordings_count = len(user.recordings)
        storage_used = sum(r.file_size for r in user.recordings if r.file_size) or 0
        
        user_data.append({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'is_admin': user.is_admin,
            'can_share_publicly': user.can_share_publicly,
            'recordings_count': recordings_count,
            'storage_used': storage_used
        })
    
    return jsonify(user_data)



@admin_bp.route('/admin/users', methods=['POST'])
@login_required
def admin_add_user():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Validate required fields
    required_fields = ['username', 'email', 'password']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    # Check if username or email already exists
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 400
    
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already exists'}), 400
    
    # Create new user
    hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
    new_user = User(
        username=data['username'],
        email=data['email'],
        password=hashed_password,
        is_admin=data.get('is_admin', False)
    )
    
    db.session.add(new_user)
    db.session.commit()
    
    return jsonify({
        'id': new_user.id,
        'username': new_user.username,
        'email': new_user.email,
        'is_admin': new_user.is_admin,
        'recordings_count': 0,
        'storage_used': 0
    }), 201



@admin_bp.route('/admin/users/<int:user_id>', methods=['PUT'])
@login_required
def admin_update_user(user_id):
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Update user fields
    if 'username' in data and data['username'] != user.username:
        # Check if username already exists
        if User.query.filter_by(username=data['username']).first():
            return jsonify({'error': 'Username already exists'}), 400
        user.username = data['username']
    
    if 'email' in data and data['email'] != user.email:
        # Check if email already exists
        if User.query.filter_by(email=data['email']).first():
            return jsonify({'error': 'Email already exists'}), 400
        user.email = data['email']
    
    if 'password' in data and data['password']:
        user.password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
    
    if 'is_admin' in data:
        user.is_admin = data['is_admin']

    if 'can_share_publicly' in data:
        user.can_share_publicly = data['can_share_publicly']

    db.session.commit()

    # Get recordings count and storage used
    recordings_count = len(user.recordings)
    storage_used = sum(r.file_size for r in user.recordings if r.file_size) or 0

    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_admin': user.is_admin,
        'can_share_publicly': user.can_share_publicly,
        'recordings_count': recordings_count,
        'storage_used': storage_used
    })



@admin_bp.route('/admin/users/<int:user_id>', methods=['DELETE'])
@login_required
def admin_delete_user(user_id):
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Prevent deleting self
    if user_id == current_user.id:
        return jsonify({'error': 'Cannot delete your own account'}), 400
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Delete user's recordings and audio files
    total_chunks = 0
    if ENABLE_INQUIRE_MODE:
        total_chunks = TranscriptChunk.query.filter_by(user_id=user_id).count()
        if total_chunks > 0:
            current_app.logger.info(f"Deleting {total_chunks} transcript chunks with embeddings for user {user_id}")
    
    for recording in user.recordings:
        try:
            if recording.audio_path and os.path.exists(recording.audio_path):
                os.remove(recording.audio_path)
        except Exception as e:
            current_app.logger.error(f"Error deleting audio file {recording.audio_path}: {e}")
    
    # Delete user (cascade will handle all related data including chunks/embeddings)
    db.session.delete(user)
    db.session.commit()
    
    if ENABLE_INQUIRE_MODE and total_chunks > 0:
        current_app.logger.info(f"Successfully deleted {total_chunks} embeddings and chunks for user {user_id}")
    
    return jsonify({'success': True})



@admin_bp.route('/admin/users/<int:user_id>/toggle-admin', methods=['POST'])
@login_required
def admin_toggle_admin(user_id):
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Prevent changing own admin status
    if user_id == current_user.id:
        return jsonify({'error': 'Cannot change your own admin status'}), 400
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Toggle admin status
    user.is_admin = not user.is_admin
    db.session.commit()
    
    return jsonify({'success': True, 'is_admin': user.is_admin})



@admin_bp.route('/admin/stats', methods=['GET'])
@login_required
def admin_get_stats():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Get total users
    total_users = User.query.count()
    
    # Get total recordings
    total_recordings = Recording.query.count()
    
    # Get recordings by status
    completed_recordings = Recording.query.filter_by(status='COMPLETED').count()
    processing_recordings = Recording.query.filter(Recording.status.in_(['PROCESSING', 'SUMMARIZING'])).count()
    pending_recordings = Recording.query.filter_by(status='PENDING').count()
    failed_recordings = Recording.query.filter_by(status='FAILED').count()
    
    # Get total storage used
    total_storage = db.session.query(db.func.sum(Recording.file_size)).scalar() or 0
    
    # Get top users by storage
    top_users_query = db.session.query(
        User.id,
        User.username,
        db.func.count(Recording.id).label('recordings_count'),
        db.func.sum(Recording.file_size).label('storage_used')
    ).join(Recording, User.id == Recording.user_id, isouter=True) \
     .group_by(User.id) \
     .order_by(db.func.sum(Recording.file_size).desc()) \
     .limit(5)
    
    top_users = []
    for user_id, username, recordings_count, storage_used in top_users_query:
        top_users.append({
            'id': user_id,
            'username': username,
            'recordings_count': recordings_count or 0,
            'storage_used': storage_used or 0
        })
    
    # Get total queries (chat requests)
    # This is a placeholder - you would need to track this in your database
    total_queries = 0
    
    return jsonify({
        'total_users': total_users,
        'total_recordings': total_recordings,
        'completed_recordings': completed_recordings,
        'processing_recordings': processing_recordings,
        'pending_recordings': pending_recordings,
        'failed_recordings': failed_recordings,
        'total_storage': total_storage,
        'top_users': top_users,
        'total_queries': total_queries
    })

# --- Transcript Template Routes ---


@admin_bp.route('/admin/settings', methods=['GET'])
@login_required
def admin_get_settings():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    settings = SystemSetting.query.all()
    return jsonify([setting.to_dict() for setting in settings])



@admin_bp.route('/admin/settings', methods=['POST'])
@login_required
def admin_update_setting():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    key = data.get('key')
    value = data.get('value')
    description = data.get('description')
    setting_type = data.get('setting_type', 'string')
    
    if not key:
        return jsonify({'error': 'Setting key is required'}), 400
    
    # Validate setting type
    valid_types = ['string', 'integer', 'boolean', 'float']
    if setting_type not in valid_types:
        return jsonify({'error': f'Invalid setting type. Must be one of: {", ".join(valid_types)}'}), 400
    
    # Validate value based on type
    if setting_type == 'integer':
        try:
            int(value) if value is not None and value != '' else None
        except (ValueError, TypeError):
            return jsonify({'error': 'Value must be a valid integer'}), 400
    elif setting_type == 'float':
        try:
            float(value) if value is not None and value != '' else None
        except (ValueError, TypeError):
            return jsonify({'error': 'Value must be a valid number'}), 400
    elif setting_type == 'boolean':
        if value not in ['true', 'false', '1', '0', 'yes', 'no', True, False, 1, 0]:
            return jsonify({'error': 'Value must be a valid boolean (true/false, 1/0, yes/no)'}), 400
    
    try:
        setting = SystemSetting.set_setting(key, value, description, setting_type)
        return jsonify(setting.to_dict())
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating setting {key}: {e}")
        return jsonify({'error': str(e)}), 500

# --- Configuration API ---


@admin_bp.route('/admin/auto-deletion/run', methods=['POST'])
@login_required
@csrf_exempt  # Exempt since already protected by admin authentication
def run_auto_deletion():
    """Admin endpoint to manually trigger auto-deletion process."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    try:
        stats = process_auto_deletion()
        return jsonify(stats)
    except Exception as e:
        current_app.logger.error(f"Error running auto-deletion: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/auto-deletion/stats', methods=['GET'])
@login_required
def get_auto_deletion_stats():
    """Get statistics about recordings eligible for auto-deletion."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    try:
        stats = {
            'enabled': ENABLE_AUTO_DELETION,
            'global_retention_days': GLOBAL_RETENTION_DAYS,
            'deletion_mode': DELETION_MODE,
            'eligible_count': 0,
            'exempted_count': 0,
            'no_retention_count': 0,
            'archived_count': 0
        }

        if ENABLE_AUTO_DELETION:
            # Check ALL completed recordings (per-recording retention)
            all_recordings = Recording.query.filter(
                Recording.status == 'COMPLETED'
            ).all()

            eligible = 0
            exempted = 0
            no_retention = 0
            current_time = datetime.utcnow()

            for recording in all_recordings:
                # Check if exempt from deletion entirely
                if is_recording_exempt_from_deletion(recording):
                    exempted += 1
                    continue

                # Get the effective retention period for this recording
                retention_days = get_retention_days_for_recording(recording)

                if not retention_days:
                    no_retention += 1
                    continue

                # Calculate cutoff for this specific recording
                cutoff_date = current_time - timedelta(days=retention_days)

                # Check if past retention period
                if recording.created_at < cutoff_date:
                    eligible += 1

            stats['eligible_count'] = eligible
            stats['exempted_count'] = exempted
            stats['no_retention_count'] = no_retention

        # Count already archived recordings
        stats['archived_count'] = Recording.query.filter(
            Recording.audio_deleted_at.is_not(None)
        ).count()

        return jsonify(stats)
    except Exception as e:
        current_app.logger.error(f"Error fetching auto-deletion stats: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/auto-deletion/preview', methods=['GET'])
@login_required
def preview_auto_deletion():
    """Preview what would be deleted without actually deleting (dry-run)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    try:
        if not ENABLE_AUTO_DELETION:
            return jsonify({'error': 'Auto-deletion is not enabled'}), 400

        # Check ALL completed recordings (per-recording retention)
        all_recordings = Recording.query.filter(
            Recording.status == 'COMPLETED'
        ).all()

        preview_data = {
            'total_checked': len(all_recordings),
            'would_delete': [],
            'would_exempt': [],
            'no_retention': [],
            'deletion_mode': DELETION_MODE,
            'global_retention_days': GLOBAL_RETENTION_DAYS,
            'supports_per_recording_retention': True
        }

        current_time = datetime.utcnow()

        for recording in all_recordings:
            rec_data = {
                'id': recording.id,
                'title': recording.title,
                'created_at': recording.created_at.isoformat(),
                'age_days': (current_time - recording.created_at).days,
                'tags': [tag.tag.name for tag in recording.tag_associations]
            }

            # Check if exempt from deletion entirely
            if is_recording_exempt_from_deletion(recording):
                rec_data['exempt_reason'] = []
                if recording.deletion_exempt:
                    rec_data['exempt_reason'].append('manually_exempted')
                for tag_assoc in recording.tag_associations:
                    if tag_assoc.tag.protect_from_deletion:
                        rec_data['exempt_reason'].append(f'tag:{tag_assoc.tag.name}')
                preview_data['would_exempt'].append(rec_data)
                continue

            # Get the effective retention period for this recording
            retention_days = get_retention_days_for_recording(recording)

            if not retention_days:
                rec_data['reason'] = 'no_retention_policy'
                preview_data['no_retention'].append(rec_data)
                continue

            rec_data['retention_days'] = retention_days

            # Calculate cutoff for this specific recording
            cutoff_date = current_time - timedelta(days=retention_days)

            # Check if past retention period
            if recording.created_at < cutoff_date:
                rec_data['days_past_retention'] = (current_time - cutoff_date).days
                preview_data['would_delete'].append(rec_data)

        return jsonify(preview_data)
    except Exception as e:
        current_app.logger.error(f"Error previewing auto-deletion: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/api/admin/migrate_recordings', methods=['POST'])
@login_required
def migrate_existing_recordings_api():
    """API endpoint to migrate existing recordings for inquire mode (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized. Admin access required.'}), 403
    
    try:
        # Count recordings that need processing
        completed_recordings = Recording.query.filter_by(status='COMPLETED').all()
        recordings_needing_processing = []
        
        for recording in completed_recordings:
            if recording.transcription:  # Has transcription
                chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                if chunk_count == 0:  # No chunks yet
                    recordings_needing_processing.append(recording)
        
        if len(recordings_needing_processing) == 0:
            return jsonify({
                'success': True,
                'message': 'All recordings are already processed for inquire mode',
                'processed': 0,
                'total': len(completed_recordings)
            })
        
        # Process in small batches to avoid timeout
        batch_size = min(5, len(recordings_needing_processing))  # Process max 5 at a time
        processed = 0
        errors = 0
        
        for i in range(min(batch_size, len(recordings_needing_processing))):
            recording = recordings_needing_processing[i]
            try:
                success = process_recording_chunks(recording.id)
                if success:
                    processed += 1
                else:
                    errors += 1
            except Exception as e:
                current_app.logger.error(f"Error processing recording {recording.id} for migration: {e}")
                errors += 1
        
        remaining = max(0, len(recordings_needing_processing) - batch_size)
        
        return jsonify({
            'success': True,
            'message': f'Processed {processed} recordings. {remaining} remaining.',
            'processed': processed,
            'errors': errors,
            'remaining': remaining,
            'total': len(recordings_needing_processing)
        })
        
    except Exception as e:
        current_app.logger.error(f"Error in migration API: {e}")
        return jsonify({'error': str(e)}), 500


# --- Auto-Processing File Monitor Integration ---


@admin_bp.route('/admin/auto-process/status', methods=['GET'])
@login_required
def admin_get_auto_process_status():
    """Get the status of the automated file processing system."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        _, _, get_file_monitor_status = get_file_monitor_functions()
        status = get_file_monitor_status()
        
        # Add configuration info
        config = {
            'enabled': os.environ.get('ENABLE_AUTO_PROCESSING', 'false').lower() == 'true',
            'watch_directory': os.environ.get('AUTO_PROCESS_WATCH_DIR', '/data/auto-process'),
            'check_interval': int(os.environ.get('AUTO_PROCESS_CHECK_INTERVAL', '30')),
            'mode': os.environ.get('AUTO_PROCESS_MODE', 'admin_only'),
            'default_username': os.environ.get('AUTO_PROCESS_DEFAULT_USERNAME')
        }
        
        return jsonify({
            'status': status,
            'config': config
        })
        
    except Exception as e:
        current_app.logger.error(f"Error getting auto-process status: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/auto-process/start', methods=['POST'])
@login_required
def admin_start_auto_process():
    """Start the automated file processing system."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        start_file_monitor, _, _ = get_file_monitor_functions()
        start_file_monitor()
        return jsonify({'success': True, 'message': 'Auto-processing started'})
    except Exception as e:
        current_app.logger.error(f"Error starting auto-process: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/auto-process/stop', methods=['POST'])
@login_required
def admin_stop_auto_process():
    """Stop the automated file processing system."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        _, stop_file_monitor, _ = get_file_monitor_functions()
        stop_file_monitor()
        return jsonify({'success': True, 'message': 'Auto-processing stopped'})
    except Exception as e:
        current_app.logger.error(f"Error stopping auto-process: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/auto-process/config', methods=['POST'])
@login_required
def admin_update_auto_process_config():
    """Update auto-processing configuration (requires restart)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No configuration data provided'}), 400
        
        # This endpoint would typically update environment variables or config files
        # For now, we'll just return the current config and note that restart is required
        return jsonify({
            'success': True, 
            'message': 'Configuration updated. Restart required to apply changes.',
            'note': 'Environment variables need to be updated manually and application restarted.'
        })
        
    except Exception as e:
        current_app.logger.error(f"Error updating auto-process config: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/inquire/process-recordings', methods=['POST'])
@login_required
def admin_process_recordings_for_inquire():
    """Process all remaining recordings for inquire mode (chunk and embed them)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        # Get optional parameters from request
        data = request.json or {}
        batch_size = data.get('batch_size', 10)
        max_recordings = data.get('max_recordings', None)
        
        # Find recordings that need processing
        completed_recordings = Recording.query.filter_by(status='COMPLETED').all()
        recordings_needing_processing = []
        
        for recording in completed_recordings:
            if recording.transcription:  # Has transcription
                chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                if chunk_count == 0:  # No chunks yet
                    recordings_needing_processing.append(recording)
                    if max_recordings and len(recordings_needing_processing) >= max_recordings:
                        break
        
        total_to_process = len(recordings_needing_processing)
        
        if total_to_process == 0:
            return jsonify({
                'success': True,
                'message': 'All recordings are already processed for inquire mode.',
                'processed': 0,
                'total': 0
            })
        
        # Process recordings in batches
        processed = 0
        failed = []
        
        for recording in recordings_needing_processing:
            try:
                success = process_recording_chunks(recording.id)
                if success:
                    processed += 1
                    current_app.logger.info(f"Admin API: Processed chunks for recording: {recording.title} ({recording.id})")
                else:
                    failed.append({'id': recording.id, 'title': recording.title, 'reason': 'Processing returned false'})
            except Exception as e:
                current_app.logger.error(f"Admin API: Failed to process recording {recording.id}: {e}")
                failed.append({'id': recording.id, 'title': recording.title, 'reason': str(e)})
            
            # Commit after each batch
            if processed % batch_size == 0:
                db.session.commit()
        
        # Final commit
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Processed {processed} out of {total_to_process} recordings.',
            'processed': processed,
            'total': total_to_process,
            'failed': failed
        })
        
    except Exception as e:
        current_app.logger.error(f"Error in admin process recordings endpoint: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/inquire/status', methods=['GET'])
@login_required  
def admin_inquire_status():
    """Get the status of recordings for inquire mode."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        # Count total completed recordings
        total_completed = Recording.query.filter_by(status='COMPLETED').count()
        
        # Count recordings with transcriptions
        recordings_with_transcriptions = Recording.query.filter(
            Recording.status == 'COMPLETED',
            Recording.transcription.isnot(None),
            Recording.transcription != ''
        ).count()
        
        # Count recordings that have been processed for inquire mode
        processed_recordings = db.session.query(Recording.id).join(
            TranscriptChunk, Recording.id == TranscriptChunk.recording_id
        ).distinct().count()
        
        # Count recordings that still need processing
        completed_recordings = Recording.query.filter_by(status='COMPLETED').all()
        need_processing = 0
        
        for recording in completed_recordings:
            if recording.transcription:  # Has transcription
                chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                if chunk_count == 0:  # No chunks yet
                    need_processing += 1
        
        # Get total chunks and embeddings count
        total_chunks = TranscriptChunk.query.count()
        
        return jsonify({
            'total_completed_recordings': total_completed,
            'recordings_with_transcriptions': recordings_with_transcriptions,
            'processed_for_inquire': processed_recordings,
            'need_processing': need_processing,
            'total_chunks': total_chunks,
            'embeddings_available': EMBEDDINGS_AVAILABLE
        })

    except Exception as e:
        current_app.logger.error(f"Error getting inquire status: {e}")
        return jsonify({'error': str(e)}), 500

# --- Group Management API (Admin Only) ---


