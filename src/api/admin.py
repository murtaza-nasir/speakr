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
from src.services.token_tracking import token_tracker
from src.services.transcription_tracking import transcription_tracker
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

        # Get current month token usage
        current_usage = token_tracker.get_monthly_usage(user.id)
        usage_percentage = (current_usage / user.monthly_token_budget * 100) if user.monthly_token_budget else 0

        # Get current month transcription usage
        current_transcription_usage = transcription_tracker.get_monthly_usage(user.id)
        transcription_usage_percentage = (current_transcription_usage / user.monthly_transcription_budget * 100) if user.monthly_transcription_budget else 0

        user_data.append({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'is_admin': user.is_admin,
            'can_share_publicly': user.can_share_publicly,
            'recordings_count': recordings_count,
            'storage_used': storage_used,
            'monthly_token_budget': user.monthly_token_budget,
            'current_token_usage': current_usage,
            'token_usage_percentage': round(usage_percentage, 1),
            'monthly_transcription_budget': user.monthly_transcription_budget,
            'monthly_transcription_budget_minutes': (user.monthly_transcription_budget // 60) if user.monthly_transcription_budget else None,
            'current_transcription_usage': current_transcription_usage,
            'current_transcription_usage_minutes': current_transcription_usage // 60,
            'transcription_usage_percentage': round(transcription_usage_percentage, 1)
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
        is_admin=data.get('is_admin', False),
        monthly_token_budget=data.get('monthly_token_budget'),
        monthly_transcription_budget=data.get('monthly_transcription_budget')
    )

    db.session.add(new_user)
    db.session.commit()

    return jsonify({
        'id': new_user.id,
        'username': new_user.username,
        'email': new_user.email,
        'is_admin': new_user.is_admin,
        'recordings_count': 0,
        'storage_used': 0,
        'monthly_token_budget': new_user.monthly_token_budget,
        'current_token_usage': 0,
        'token_usage_percentage': 0,
        'monthly_transcription_budget': new_user.monthly_transcription_budget,
        'monthly_transcription_budget_minutes': (new_user.monthly_transcription_budget // 60) if new_user.monthly_transcription_budget else None,
        'current_transcription_usage': 0,
        'current_transcription_usage_minutes': 0,
        'transcription_usage_percentage': 0
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

    if 'monthly_token_budget' in data:
        # Allow setting to None (unlimited) or a positive integer
        budget = data['monthly_token_budget']
        if budget is None or budget == '' or budget == 0:
            user.monthly_token_budget = None
        else:
            user.monthly_token_budget = int(budget)

    if 'monthly_transcription_budget' in data:
        # Allow setting to None (unlimited) or a positive integer (in seconds)
        budget = data['monthly_transcription_budget']
        if budget is None or budget == '' or budget == 0:
            user.monthly_transcription_budget = None
        else:
            user.monthly_transcription_budget = int(budget)

    db.session.commit()

    # Get recordings count and storage used
    recordings_count = len(user.recordings)
    storage_used = sum(r.file_size for r in user.recordings if r.file_size) or 0

    # Get current month token usage
    current_usage = token_tracker.get_monthly_usage(user.id)
    usage_percentage = (current_usage / user.monthly_token_budget * 100) if user.monthly_token_budget else 0

    # Get current month transcription usage
    current_transcription_usage = transcription_tracker.get_monthly_usage(user.id)
    transcription_usage_percentage = (current_transcription_usage / user.monthly_transcription_budget * 100) if user.monthly_transcription_budget else 0

    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_admin': user.is_admin,
        'can_share_publicly': user.can_share_publicly,
        'recordings_count': recordings_count,
        'storage_used': storage_used,
        'monthly_token_budget': user.monthly_token_budget,
        'current_token_usage': current_usage,
        'token_usage_percentage': round(usage_percentage, 1),
        'monthly_transcription_budget': user.monthly_transcription_budget,
        'monthly_transcription_budget_minutes': (user.monthly_transcription_budget // 60) if user.monthly_transcription_budget else None,
        'current_transcription_usage': current_transcription_usage,
        'current_transcription_usage_minutes': current_transcription_usage // 60,
        'transcription_usage_percentage': round(transcription_usage_percentage, 1)
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

    # Explicitly delete related records that might not cascade properly
    ProcessingJob.query.filter_by(user_id=user_id).delete()
    InternalShare.query.filter(
        (InternalShare.owner_id == user_id) | (InternalShare.shared_with_user_id == user_id)
    ).delete()

    # Delete user (cascade will handle remaining related data including chunks/embeddings)
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


# --- Token Usage Stats ---

@admin_bp.route('/admin/token-stats', methods=['GET'])
@login_required
def admin_get_token_stats():
    """Get overall token usage statistics."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        from src.models import TokenUsage
        from sqlalchemy import func, extract
        from datetime import date

        # Get today's usage (already split into llm_/embedding_ buckets)
        today_usage = token_tracker.get_today_usage()

        # Get current month usage for all users (also bucketed by the tracker)
        monthly_stats = token_tracker.get_monthly_stats(months=1)
        current_month = monthly_stats[-1] if monthly_stats else {
            'tokens': 0, 'cost': 0,
            'llm_tokens': 0, 'llm_cost': 0,
            'embedding_tokens': 0, 'embedding_cost': 0,
        }

        # Get per-user stats for current month
        user_stats = token_tracker.get_user_stats()

        # Per-operation breakdown for the current month so the UI can show
        # how the total splits across summarization, chat, embedding, etc.
        today = date.today()
        op_rows = db.session.query(
            TokenUsage.operation_type,
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.sum(TokenUsage.cost).label('cost'),
            func.sum(TokenUsage.request_count).label('requests'),
        ).filter(
            extract('year', TokenUsage.date) == today.year,
            extract('month', TokenUsage.date) == today.month,
        ).group_by(TokenUsage.operation_type).all()
        by_operation = [
            {
                'operation_type': r.operation_type,
                'tokens': int(r.tokens or 0),
                'cost': float(r.cost or 0.0),
                'requests': int(r.requests or 0),
                'is_embedding': token_tracker.is_embedding_op(r.operation_type),
            }
            for r in op_rows
        ]

        return jsonify({
            'today': today_usage,
            'current_month': {
                'tokens': current_month.get('tokens', 0),
                'cost': current_month.get('cost', 0),
                'llm_tokens': current_month.get('llm_tokens', 0),
                'llm_cost': current_month.get('llm_cost', 0),
                'embedding_tokens': current_month.get('embedding_tokens', 0),
                'embedding_cost': current_month.get('embedding_cost', 0),
                'by_operation': by_operation,
            },
            'user_count_with_usage': len([u for u in user_stats if u['current_usage'] > 0]),
            'users_over_80_percent': len([u for u in user_stats if u['percentage'] >= 80]),
            'users_at_100_percent': len([u for u in user_stats if u['percentage'] >= 100])
        })

    except Exception as e:
        current_app.logger.error(f"Error getting token stats: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/token-stats/daily', methods=['GET'])
@login_required
def admin_get_daily_token_stats():
    """Get daily token usage for charts (last 30 days)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        days = request.args.get('days', 30, type=int)
        user_id = request.args.get('user_id', type=int)

        daily_stats = token_tracker.get_daily_stats(days=days, user_id=user_id)

        return jsonify({
            'stats': daily_stats,
            'days': days
        })

    except Exception as e:
        current_app.logger.error(f"Error getting daily token stats: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/token-stats/monthly', methods=['GET'])
@login_required
def admin_get_monthly_token_stats():
    """Get monthly token usage for charts (last 12 months)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        months = request.args.get('months', 12, type=int)

        monthly_stats = token_tracker.get_monthly_stats(months=months)

        return jsonify({
            'stats': monthly_stats,
            'months': months
        })

    except Exception as e:
        current_app.logger.error(f"Error getting monthly token stats: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/token-stats/users', methods=['GET'])
@login_required
def admin_get_user_token_stats():
    """Get per-user token usage for current month."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        user_stats = token_tracker.get_user_stats()

        return jsonify({
            'users': user_stats
        })

    except Exception as e:
        current_app.logger.error(f"Error getting user token stats: {e}")
        return jsonify({'error': str(e)}), 500


# --- Transcription Usage Stats ---


@admin_bp.route('/admin/transcription-stats', methods=['GET'])
@login_required
def admin_get_transcription_stats():
    """Get overall transcription usage statistics."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        # Get today's usage
        today_usage = transcription_tracker.get_today_usage()

        # Get current month usage for all users
        monthly_stats = transcription_tracker.get_monthly_stats(months=1)
        current_month = monthly_stats[-1] if monthly_stats else {'seconds': 0, 'minutes': 0, 'cost': 0}

        # Get per-user stats for current month
        user_stats = transcription_tracker.get_user_stats()

        # Calculate totals
        total_monthly_seconds = current_month.get('seconds', 0)
        total_monthly_cost = current_month.get('cost', 0)

        return jsonify({
            'today': today_usage,
            'current_month': {
                'seconds': total_monthly_seconds,
                'minutes': total_monthly_seconds // 60,
                'cost': total_monthly_cost
            },
            'user_count_with_usage': len([u for u in user_stats if u['current_usage_seconds'] > 0]),
            'users_over_80_percent': len([u for u in user_stats if u['percentage'] >= 80]),
            'users_at_100_percent': len([u for u in user_stats if u['percentage'] >= 100])
        })

    except Exception as e:
        current_app.logger.error(f"Error getting transcription stats: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/transcription-stats/daily', methods=['GET'])
@login_required
def admin_get_daily_transcription_stats():
    """Get daily transcription usage for charts (last 30 days)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        days = request.args.get('days', 30, type=int)
        user_id = request.args.get('user_id', type=int)

        daily_stats = transcription_tracker.get_daily_stats(days=days, user_id=user_id)

        return jsonify({
            'stats': daily_stats,
            'days': days
        })

    except Exception as e:
        current_app.logger.error(f"Error getting daily transcription stats: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/transcription-stats/monthly', methods=['GET'])
@login_required
def admin_get_monthly_transcription_stats():
    """Get monthly transcription usage for charts (last 12 months)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        months = request.args.get('months', 12, type=int)

        monthly_stats = transcription_tracker.get_monthly_stats(months=months)

        return jsonify({
            'stats': monthly_stats,
            'months': months
        })

    except Exception as e:
        current_app.logger.error(f"Error getting monthly transcription stats: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/transcription-stats/users', methods=['GET'])
@login_required
def admin_get_user_transcription_stats():
    """Get per-user transcription usage for current month."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        user_stats = transcription_tracker.get_user_stats()

        return jsonify({
            'users': user_stats
        })

    except Exception as e:
        current_app.logger.error(f"Error getting user transcription stats: {e}")
        return jsonify({'error': str(e)}), 500


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

        # Update Flask's MAX_CONTENT_LENGTH immediately when file size limit changes
        if key == 'max_file_size_mb' and value:
            try:
                new_limit = int(value) * 1024 * 1024
                current_app.config['MAX_CONTENT_LENGTH'] = new_limit
                current_app.logger.info(f"Updated MAX_CONTENT_LENGTH to {value}MB")
            except (ValueError, TypeError):
                pass

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



@admin_bp.route('/admin/auto-process/trigger', methods=['POST'])
@login_required
def admin_trigger_auto_process():
    """Trigger an immediate auto-process scan cycle."""
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        from src.file_monitor import file_monitor
        if not file_monitor or not file_monitor.running:
            return jsonify({'error': 'Auto-processing is not running'}), 400

        import threading
        def run_scan():
            try:
                file_monitor._update_user_cache()
                if file_monitor.mode == 'admin_only':
                    file_monitor._scan_admin_directory()
                elif file_monitor.mode == 'user_directories':
                    file_monitor._scan_user_directories()
                elif file_monitor.mode == 'single_user':
                    file_monitor._scan_single_user_directory()
            except Exception as e:
                file_monitor.logger.error(f"Error during triggered scan: {e}", exc_info=True)

        thread = threading.Thread(target=run_scan, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': 'Scan triggered'})

    except Exception as e:
        current_app.logger.error(f"Error triggering auto-process scan: {e}")
        return jsonify({'error': str(e)}), 500



@admin_bp.route('/admin/transcription/discover-models', methods=['GET'])
@login_required
def admin_discover_transcription_models():
    """Probe the active transcription connector's /v1/models endpoint.

    Used by the admin UI to populate a "models available on this provider"
    list when curating which models users can pick from. Returns a list of
    {id, label, owned_by} dicts. Empty list if the connector does not
    implement list_models() or the upstream is unreachable.
    """
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        from src.services.transcription import get_registry
        registry = get_registry()
        connector = registry.get_active_connector()
        if not connector:
            return jsonify({'error': 'No active transcription connector'}), 503

        connector_name = registry.get_active_connector_name()
        try:
            models = connector.list_models()
        except NotImplementedError:
            return jsonify({
                'connector': connector_name,
                'supported': False,
                'models': [],
                'message': f'{connector_name} does not expose a model discovery endpoint.',
            })

        return jsonify({
            'connector': connector_name,
            'supported': True,
            'models': models or [],
        })

    except Exception as e:
        current_app.logger.error(f"Error discovering transcription models: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/transcription/visible-models', methods=['GET'])
@login_required
def admin_get_visible_models():
    """Return the admin-curated list of models exposed in the user dropdown.

    Falls back to the TRANSCRIPTION_MODELS_AVAILABLE env var when no DB
    setting has been saved yet. The shape matches what /api/config returns
    so the admin UI can pre-populate its form.
    """
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        from src.models import SystemSetting
        from src.config.app_config import TRANSCRIPTION_MODEL_OPTIONS
        import json as _json

        raw = SystemSetting.get_setting('transcription_models_visible_json', None)
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    return jsonify({
                        'source': 'database',
                        'options': parsed,
                        'default_model': SystemSetting.get_setting('transcription_default_model', None),
                    })
            except (ValueError, TypeError):
                pass

        return jsonify({
            'source': 'env' if TRANSCRIPTION_MODEL_OPTIONS else 'unset',
            'options': TRANSCRIPTION_MODEL_OPTIONS,
            'default_model': None,
        })

    except Exception as e:
        current_app.logger.error(f"Error reading visible transcription models: {e}")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/transcription/visible-models', methods=['POST'])
@login_required
def admin_save_visible_models():
    """Save the admin-curated list of user-facing transcription models.

    Body: { "options": [{"value": "...", "label": "..."}, ...],
            "default_model": "..." | null }
    """
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        from src.models import SystemSetting
        import json as _json

        data = request.json or {}
        options = data.get('options', [])
        if not isinstance(options, list):
            return jsonify({'error': 'options must be a list'}), 400

        # Normalise each option into {value, label}.
        normalised = []
        for opt in options:
            if isinstance(opt, str):
                normalised.append({'value': opt, 'label': opt})
            elif isinstance(opt, dict) and opt.get('value'):
                normalised.append({
                    'value': str(opt['value']).strip(),
                    'label': str(opt.get('label') or opt['value']).strip(),
                })

        SystemSetting.set_setting(
            key='transcription_models_visible_json',
            value=_json.dumps(normalised),
            description='Admin-curated list of transcription models exposed in user dropdowns. Overrides TRANSCRIPTION_MODELS_AVAILABLE env var when set.',
            setting_type='string',
        )

        default_model = (data.get('default_model') or '').strip()
        if default_model:
            SystemSetting.set_setting(
                key='transcription_default_model',
                value=default_model,
                description='Admin-selected default transcription model. Used when no per-upload, tag, or folder override is set.',
                setting_type='string',
            )
        else:
            existing = SystemSetting.query.filter_by(key='transcription_default_model').first()
            if existing:
                db.session.delete(existing)
                db.session.commit()

        return jsonify({
            'success': True,
            'options': normalised,
            'default_model': default_model or None,
        })

    except Exception as e:
        current_app.logger.error(f"Error saving visible transcription models: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/admin/inquire/process-recordings', methods=['POST'])
@login_required
def admin_process_recordings_for_inquire():
    """Process recordings for inquire mode (chunk and embed them).

    Default behaviour processes only recordings that do not yet have chunks.
    Pass ``force=true`` in the JSON body to re-embed every completed
    recording regardless of existing chunks. This is the migration path after
    swapping ``EMBEDDING_MODEL`` or ``EMBEDDING_BASE_URL``: the old vectors
    are deleted and replaced with vectors from the new configuration.

    Body fields:
        force (bool, optional, default false): re-embed all recordings
        batch_size (int, optional, default 10): commit cadence
        max_recordings (int, optional): cap the number processed in one call
    """
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        data = request.json or {}
        batch_size = int(data.get('batch_size', 10) or 10)
        max_recordings = data.get('max_recordings', None)
        force = bool(data.get('force', False))

        if force:
            # Re-embed every recording that has a usable transcription, plus
            # any recording that already has chunks stored. The chunks-already-
            # stored case matters because a recording can be in a non-COMPLETED
            # state (PROCESSING, SUMMARIZING, FAILED) while still holding stale
            # vectors from the previous embedding configuration. Filtering on
            # status alone leaves those ghost chunks behind and produces the
            # "X.shape[1] == new while Y.shape[1] == old" warnings on every
            # subsequent search until they are cleaned up.
            recordings_with_chunks_subq = db.session.query(
                TranscriptChunk.recording_id
            ).distinct()
            recordings_needing_processing = Recording.query.filter(
                db.or_(
                    db.and_(
                        Recording.status == 'COMPLETED',
                        Recording.transcription.isnot(None),
                        Recording.transcription != '',
                    ),
                    Recording.id.in_(recordings_with_chunks_subq),
                )
            ).all()
        else:
            # Only process recordings that have no chunks yet.
            recordings_with_chunks = db.session.query(TranscriptChunk.recording_id).distinct()
            recordings_needing_processing = Recording.query.filter(
                Recording.status == 'COMPLETED',
                Recording.transcription.isnot(None),
                Recording.transcription != '',
                ~Recording.id.in_(recordings_with_chunks)
            ).all()

        if max_recordings:
            recordings_needing_processing = recordings_needing_processing[:int(max_recordings)]

        total_to_process = len(recordings_needing_processing)

        if total_to_process == 0:
            return jsonify({
                'success': True,
                'message': (
                    'No recordings to re-embed.' if force
                    else 'All recordings are already processed for inquire mode.'
                ),
                'processed': 0,
                'total': 0,
                'force': force,
            })

        processed = 0
        failed = []

        # Number of times to retry each recording after the first pass. The
        # underlying _api_embed already retries transient errors; this layer
        # catches whole-pipeline failures (DB lock contention, partial
        # provider responses, anything that produced a False return).
        retry_passes = max(0, int(data.get('retry_passes', 2)))

        def _attempt_recording(recording, attempt_label):
            """Run process_recording_chunks for one recording.

            Returns ``(success: bool, error: Optional[str])``. Any exception
            is caught and converted to an error string so a single bad row
            cannot abort the whole sweep.
            """
            try:
                ok = process_recording_chunks(recording.id)
            except Exception as exc:
                current_app.logger.error(
                    f"Admin API: Failed to process recording {recording.id} "
                    f"({attempt_label}): {exc}"
                )
                return False, str(exc)
            if ok:
                current_app.logger.info(
                    f"Admin API: {'Re-embedded' if force else 'Processed chunks for'} "
                    f"recording ({attempt_label}): {recording.title} ({recording.id})"
                )
                return True, None
            return False, 'Processing returned false'

        # First pass: try every recording once.
        retry_queue = []
        for recording in recordings_needing_processing:
            ok, err = _attempt_recording(recording, 'pass 1')
            if ok:
                processed += 1
            else:
                retry_queue.append((recording, err))

            if processed % batch_size == 0:
                db.session.commit()

        # Subsequent passes: retry any recording that did not succeed.
        # Backoff between passes lets a temporarily overloaded provider
        # recover before we hammer it again.
        for pass_num in range(2, retry_passes + 2):
            if not retry_queue:
                break
            import time as _t
            _t.sleep(min(15, 2 ** (pass_num - 1)))
            current_app.logger.info(
                f"Admin API: retry pass {pass_num} on {len(retry_queue)} recordings"
            )
            still_failing = []
            for recording, prev_err in retry_queue:
                ok, err = _attempt_recording(recording, f'pass {pass_num}')
                if ok:
                    processed += 1
                else:
                    still_failing.append((recording, err or prev_err))
            retry_queue = still_failing

        # Anything still in retry_queue after the last pass is recorded as failed.
        for recording, err in retry_queue:
            failed.append({
                'id': recording.id,
                'title': recording.title,
                'reason': err or 'Processing returned false',
                'attempts': retry_passes + 1,
            })

        db.session.commit()

        # On a successful force re-embed, refresh the stored identifier so the
        # mismatch warning clears.
        if force and processed > 0 and not failed:
            try:
                from src.models import SystemSetting
                from src.services.embeddings import EMBEDDING_IDENTIFIER
                SystemSetting.set_setting(
                    key='embedding_identifier',
                    value=EMBEDDING_IDENTIFIER,
                    description='Identifier of the embedding provider and model that produced the stored chunk vectors. Used to detect dimensionality and semantic-space mismatches at startup.',
                    setting_type='string',
                )
            except Exception as e:
                current_app.logger.warning(f"Failed to refresh embedding_identifier after re-embed: {e}")

        verb = 'Re-embedded' if force else 'Processed'
        return jsonify({
            'success': True,
            'message': f'{verb} {processed} out of {total_to_process} recordings.',
            'processed': processed,
            'total': total_to_process,
            'failed': failed,
            'force': force,
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
        from src.services.embeddings import (
            EMBEDDING_MODEL, EMBEDDING_BASE_URL, EMBEDDING_IDENTIFIER,
            USE_API_EMBEDDINGS, EMBEDDING_DIMENSIONS,
        )
        from src.models import SystemSetting, TokenUsage
        from sqlalchemy import func

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

        # Count recordings that still need processing (have transcription but no chunks)
        need_processing = db.session.query(func.count(Recording.id)).filter(
            Recording.status == 'COMPLETED',
            Recording.transcription.isnot(None),
            Recording.transcription != '',
            ~Recording.id.in_(db.session.query(TranscriptChunk.recording_id).distinct())
        ).scalar() or 0

        # Total chunks across the system
        total_chunks = TranscriptChunk.query.count()

        # Detect embedded chunks that pre-date the current configuration. The
        # stored identifier is what produced the existing vectors; if it does
        # not match the current EMBEDDING_IDENTIFIER, those chunks are stale
        # and Inquire mode will return wrong results until they are re-embedded.
        stored_identifier = SystemSetting.get_setting('embedding_identifier', None)
        if stored_identifier is None:
            legacy = SystemSetting.get_setting('embedding_model_name', None)
            stored_identifier = f"local::{legacy}" if legacy else None

        identifier_mismatch = bool(
            stored_identifier and stored_identifier != EMBEDDING_IDENTIFIER and total_chunks > 0
        )

        # Embedding-API usage aggregates (lifetime totals across all users).
        usage_rows = TokenUsage.query.filter_by(operation_type='embedding').all()
        embedding_usage = {
            'total_tokens': sum(u.total_tokens for u in usage_rows),
            'total_cost': sum((u.cost or 0.0) for u in usage_rows),
            'request_count': sum(u.request_count for u in usage_rows),
        }

        return jsonify({
            'total_completed_recordings': total_completed,
            'recordings_with_transcriptions': recordings_with_transcriptions,
            'processed_for_inquire': processed_recordings,
            'need_processing': need_processing,
            'total_chunks': total_chunks,
            'embeddings_available': EMBEDDINGS_AVAILABLE,
            # Embedding configuration surfaced to the UI
            'embedding_model': EMBEDDING_MODEL,
            'embedding_provider': 'api' if USE_API_EMBEDDINGS else 'local',
            'embedding_base_url': EMBEDDING_BASE_URL or None,
            'embedding_dimensions_override': EMBEDDING_DIMENSIONS,
            'embedding_identifier': EMBEDDING_IDENTIFIER,
            'embedding_identifier_stored': stored_identifier,
            'embedding_identifier_mismatch': identifier_mismatch,
            # Embedding-API usage (only meaningful in API mode; local mode = 0)
            'embedding_usage': embedding_usage,
        })

    except Exception as e:
        current_app.logger.error(f"Error getting inquire status: {e}")
        return jsonify({'error': str(e)}), 500

# --- Group Management API (Admin Only) ---


