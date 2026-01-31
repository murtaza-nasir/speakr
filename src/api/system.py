"""
System info and configuration.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *
from src.config.version import get_version
from src.services.llm import TEXT_MODEL_BASE_URL, TEXT_MODEL_NAME
from src.config.app_config import ASR_BASE_URL, USE_NEW_TRANSCRIPTION_ARCHITECTURE
from src.services.token_tracking import token_tracker
from src.services.transcription import TranscriptionCapability

# Create blueprint
system_bp = Blueprint('system', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
DELETION_MODE = os.environ.get('DELETION_MODE', 'full_recording')  # 'audio_only' or 'full_recording'
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
ENABLE_CHUNKING = os.environ.get('ENABLE_CHUNKING', 'true').lower() == 'true'
SHOW_USERNAMES_IN_UI = os.environ.get('SHOW_USERNAMES_IN_UI', 'false').lower() == 'true'
ENABLE_INCOGNITO_MODE = os.environ.get('ENABLE_INCOGNITO_MODE', 'false').lower() == 'true'
INCOGNITO_MODE_DEFAULT = os.environ.get('INCOGNITO_MODE_DEFAULT', 'false').lower() == 'true'

# Import chunking service (will be set from app)
chunking_service = None

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_system_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter, chunking_service
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')
    chunking_service = kwargs.get('chunking_service')


def csrf_exempt(f):
    """Decorator placeholder for CSRF exemption - applied after initialization."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    wrapper._csrf_exempt = True
    return wrapper


# --- Routes ---

@system_bp.route('/api/user/preferences', methods=['POST'])
@login_required
def save_user_preferences():
    """Save user preferences including UI language"""
    data = request.json
    
    if 'language' in data:
        current_user.ui_language = data['language']
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Preferences saved successfully',
        'ui_language': current_user.ui_language
    })


@system_bp.route('/api/user/token-budget', methods=['GET'])
@login_required
def get_user_token_budget():
    """Get current user's token budget status."""
    try:
        user = current_user

        # If user has no budget, return null to indicate unlimited
        if not user.monthly_token_budget:
            return jsonify({
                'has_budget': False,
                'budget': None,
                'usage': 0,
                'percentage': 0
            })

        # Get current usage
        current_usage = token_tracker.get_monthly_usage(user.id)
        percentage = (current_usage / user.monthly_token_budget) * 100

        return jsonify({
            'has_budget': True,
            'budget': user.monthly_token_budget,
            'usage': current_usage,
            'percentage': round(percentage, 1)
        })
    except Exception as e:
        current_app.logger.error(f"Error getting token budget for user {current_user.id}: {e}")
        return jsonify({'error': str(e)}), 500


# --- System Info API Endpoint ---


@system_bp.route('/api/system/info', methods=['GET'])
def get_system_info():
    """Get system information including version and model details."""
    try:
        # Use the same version detection logic as startup
        version = get_version()

        # Get transcription connector info
        transcription_info = {
            'connector': 'unknown',
            'model': None,
            'supports_diarization': USE_ASR_ENDPOINT,  # Backwards compatible default
            'supports_speaker_embeddings': False,
        }

        if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
            try:
                from src.services.transcription import get_registry
                registry = get_registry()
                connector = registry.get_active_connector()
                if connector:
                    transcription_info = {
                        'connector': registry.get_active_connector_name(),
                        'model': getattr(connector, 'model', None),  # Model name if available
                        'supports_diarization': connector.supports_diarization,
                        'supports_speaker_embeddings': connector.supports(TranscriptionCapability.SPEAKER_EMBEDDINGS),
                    }
            except Exception as e:
                current_app.logger.warning(f"Could not get connector info: {e}")

        # Determine ASR status from connector (new arch) or env var (legacy)
        is_asr_connector = transcription_info.get('connector') == 'asr_endpoint'
        asr_enabled = is_asr_connector or USE_ASR_ENDPOINT

        return jsonify({
            'version': version,
            'llm_endpoint': TEXT_MODEL_BASE_URL,
            'llm_model': TEXT_MODEL_NAME,
            'whisper_endpoint': os.environ.get('TRANSCRIPTION_BASE_URL', 'https://api.openai.com/v1'),
            'asr_enabled': asr_enabled,
            'asr_endpoint': ASR_BASE_URL if asr_enabled else None,
            'transcription': transcription_info,
        })
    except Exception as e:
        current_app.logger.error(f"Error getting system info: {e}")
        return jsonify({'error': 'Unable to retrieve system information'}), 500

# --- Tag API Endpoints ---


@system_bp.route('/api/config', methods=['GET'])
def get_config():
    """Get application configuration settings for the frontend."""
    try:
        # Get configurable file size limit
        max_file_size_mb = SystemSetting.get_setting('max_file_size_mb', 250)

        # Get chunking configuration (supports both legacy and new formats)
        chunking_info = {}
        if ENABLE_CHUNKING and chunking_service:
            mode, limit_value = chunking_service.parse_chunk_limit()
            chunking_info = {
                'chunking_enabled': True,
                'chunking_mode': mode,  # 'size' or 'duration'
                'chunking_limit': limit_value,  # Value in MB or seconds
                'chunking_limit_display': f"{limit_value}{'MB' if mode == 'size' else 's'}"
            }
        else:
            chunking_info = {
                'chunking_enabled': False,
                'chunking_mode': 'size',
                'chunking_limit': 20,
                'chunking_limit_display': '20MB'
            }

        # Check if current user can delete (for authenticated requests)
        can_delete = True  # Default to true for unauthenticated config requests
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                can_delete = USERS_CAN_DELETE or current_user.is_admin
        except:
            pass  # If not authenticated, use default

        # Calculate if archive toggle should be shown (only when audio-only deletion mode is active)
        enable_archive_toggle = ENABLE_AUTO_DELETION and DELETION_MODE == 'audio_only'

        # Get connector capabilities (new architecture)
        # Defaults to USE_ASR_ENDPOINT for backwards compatibility
        connector_supports_diarization = USE_ASR_ENDPOINT
        connector_supports_speaker_count = USE_ASR_ENDPOINT  # ASR endpoint supports min/max speakers
        is_asr_connector = False
        if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
            try:
                from src.services.transcription import get_registry
                registry = get_registry()
                connector = registry.get_active_connector()
                if connector:
                    connector_supports_diarization = connector.supports_diarization
                    connector_supports_speaker_count = connector.supports_speaker_count_control
                    is_asr_connector = registry.get_active_connector_name() == 'asr_endpoint'
            except Exception as e:
                current_app.logger.warning(f"Could not get connector capabilities: {e}")

        # Derive ASR status from connector or legacy env var
        asr_enabled = is_asr_connector or USE_ASR_ENDPOINT

        return jsonify({
            'max_file_size_mb': max_file_size_mb,
            'recording_disclaimer': SystemSetting.get_setting('recording_disclaimer', ''),
            'use_asr_endpoint': asr_enabled,  # Derived from connector or legacy env var
            'connector_supports_diarization': connector_supports_diarization,  # Connector capability
            'connector_supports_speaker_count': connector_supports_speaker_count,  # Min/max speakers
            'enable_internal_sharing': ENABLE_INTERNAL_SHARING,
            'enable_archive_toggle': enable_archive_toggle,
            'show_usernames_in_ui': SHOW_USERNAMES_IN_UI,
            'can_delete_recordings': can_delete,
            'users_can_delete_enabled': USERS_CAN_DELETE,
            'enable_incognito_mode': ENABLE_INCOGNITO_MODE,
            'incognito_mode_default': INCOGNITO_MODE_DEFAULT,
            'enable_folders': SystemSetting.get_setting('enable_folders', 'false') == 'true',
            **chunking_info
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching configuration: {e}")
        return jsonify({'error': str(e)}), 500




@system_bp.route('/api/csrf-token', methods=['GET'])
@csrf_exempt  # Exempt this endpoint from CSRF protection since it's providing tokens
def get_csrf_token():
    """Get a fresh CSRF token for the frontend."""
    try:
        from flask_wtf.csrf import generate_csrf
        token = generate_csrf()
        current_app.logger.info("Fresh CSRF token generated successfully")
        return jsonify({'csrf_token': token})
    except Exception as e:
        current_app.logger.error(f"Error generating CSRF token: {e}")
        return jsonify({'error': str(e)}), 500

# --- Flask Routes ---


@system_bp.route('/api/permissions/can-delete', methods=['GET'])
@login_required
def check_deletion_permission():
    """Check if the current user can delete recordings."""
    try:
        can_delete = USERS_CAN_DELETE or current_user.is_admin
        return jsonify({
            'can_delete': can_delete,
            'is_admin': current_user.is_admin,
            'users_can_delete_enabled': USERS_CAN_DELETE
        })
    except Exception as e:
        current_app.logger.error(f"Error checking deletion permissions: {e}")
        return jsonify({'error': str(e)}), 500



