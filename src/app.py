# Speakr - Audio Transcription and Summarization App
import os
import sys
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash, Response, make_response
from urllib.parse import urlparse, urljoin, quote
from email.utils import encode_rfc2231
try:
    from flask import Markup
except ImportError:
    from markupsafe import Markup
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from openai import OpenAI # Keep using the OpenAI library
import json
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import select
from sqlalchemy.orm import joinedload
import threading
from dotenv import load_dotenv # Import load_dotenv
import httpx 
import re
import subprocess
import mimetypes
import markdown
import bleach

# Add common audio MIME type mappings that might be missing
mimetypes.add_type('audio/mp4', '.m4a')
mimetypes.add_type('audio/aac', '.aac')
mimetypes.add_type('audio/x-m4a', '.m4a')
mimetypes.add_type('audio/webm', '.webm')
mimetypes.add_type('audio/flac', '.flac')
mimetypes.add_type('audio/ogg', '.ogg')
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import pytz
from babel.dates import format_datetime
import ast
import logging
import secrets
import time
from src.audio_chunking import AudioChunkingService, ChunkProcessingError, ChunkingNotSupportedError

# Optional imports for embedding functionality
try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
    from sklearn.metrics.pairwise import cosine_similarity
    EMBEDDINGS_AVAILABLE = True
except ImportError as e:
    EMBEDDINGS_AVAILABLE = False
    # Create dummy classes to prevent import errors
    class SentenceTransformer:
        def __init__(self, *args, **kwargs):
            pass
        def encode(self, *args, **kwargs):
            return []
    
    np = None
    cosine_similarity = None

# Load environment variables from .env file
load_dotenv()

# Early check for Inquire Mode configuration (needed for startup message)
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'

# Auto-deletion and retention configuration
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
GLOBAL_RETENTION_DAYS = int(os.environ.get('GLOBAL_RETENTION_DAYS', '0'))  # 0 = disabled
DELETION_MODE = os.environ.get('DELETION_MODE', 'full_recording')  # 'audio_only' or 'full_recording'

# Permission-based deletion control
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'  # true = all users can delete, false = admin only

# Internal sharing configuration
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
SHOW_USERNAMES_IN_UI = os.environ.get('SHOW_USERNAMES_IN_UI', 'false').lower() == 'true'

# Public sharing configuration
ENABLE_PUBLIC_SHARING = os.environ.get('ENABLE_PUBLIC_SHARING', 'true').lower() == 'true'

# Log embedding status on startup
if ENABLE_INQUIRE_MODE and EMBEDDINGS_AVAILABLE:
    print("✅ Inquire Mode: Full semantic search enabled (embeddings available)")
elif ENABLE_INQUIRE_MODE and not EMBEDDINGS_AVAILABLE:
    print("⚠️  Inquire Mode: Basic text search only (embedding dependencies not available)")
    print("   To enable semantic search, install: pip install sentence-transformers==2.7.0 huggingface-hub>=0.19.0")
elif not ENABLE_INQUIRE_MODE:
    print("ℹ️  Inquire Mode: Disabled (set ENABLE_INQUIRE_MODE=true to enable)")

# Log auto-deletion status
if ENABLE_AUTO_DELETION:
    if GLOBAL_RETENTION_DAYS > 0:
        print(f"✅ Auto-deletion: Enabled (global retention: {GLOBAL_RETENTION_DAYS} days, mode: {DELETION_MODE})")
    else:
        print("⚠️  Auto-deletion: Enabled but no global retention period set (configure GLOBAL_RETENTION_DAYS)")
else:
    print("ℹ️  Auto-deletion: Disabled (set ENABLE_AUTO_DELETION=true to enable)")

# Log deletion permissions
if USERS_CAN_DELETE:
    print("ℹ️  User deletion: Enabled (all users can delete their recordings)")
else:
    print("🔒 User deletion: Restricted (only admins can delete recordings)")

# Log internal sharing status
if ENABLE_INTERNAL_SHARING:
    username_visibility = "visible" if SHOW_USERNAMES_IN_UI else "hidden"
    print(f"✅ Internal sharing: Enabled (usernames {username_visibility})")
else:
    print("ℹ️  Internal sharing: Disabled (set ENABLE_INTERNAL_SHARING=true to enable)")

# Log public sharing status
if ENABLE_PUBLIC_SHARING:
    print("✅ Public sharing: Enabled (users can create public share links)")
else:
    print("🔒 Public sharing: Disabled (public share links are not allowed)")

# Configure logging
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(log_level)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# Get the root logger and clear any existing handlers to avoid duplicates
root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.setLevel(log_level)
root_logger.addHandler(handler)

# Silence noisy markdown extension debug logs
markdown_logger = logging.getLogger('MARKDOWN')
markdown_logger.setLevel(logging.WARNING)

# --- Initialize Markdown Once (Performance Optimization) ---
# Create a single reusable Markdown instance to avoid reinitializing extensions on every call
_markdown_instance = markdown.Markdown(extensions=[
    'fenced_code',      # Fenced code blocks
    'tables',           # Table support
    'attr_list',        # Attribute lists
    'def_list',         # Definition lists
    'footnotes',        # Footnotes
    'abbr',             # Abbreviations
    'codehilite',       # Syntax highlighting for code blocks
    'smarty'            # Smart quotes, dashes, etc.
])

# --- Rate Limiting Setup (will be configured after app creation) ---
# TEMPORARILY INCREASED FOR TESTING - REVERT FOR PRODUCTION!
limiter = Limiter(
    get_remote_address,
    app=None,  # Defer initialization
    default_limits=["5000 per day", "1000 per hour"]  # Increased from 200/day, 50/hour for testing
)

# --- Utility Functions ---
# Utility functions (JSON parsing, markdown, datetime, security) have been extracted
# to src/utils/ and imported at the top of this file

def has_recording_access(recording, user, require_edit=False, require_reshare=False):
    """
    Check if a user has access to a recording.

    Args:
        recording: Recording object to check access for
        user: User object (typically current_user)
        require_edit: If True, check for edit permission (default: False for view-only)
        require_reshare: If True, check for reshare permission (default: False)

    Returns:
        Boolean indicating if user has the required access level
    """
    # Owner always has full access
    if recording.user_id == user.id:
        return True

    # If internal sharing is not enabled, only owner has access
    if not ENABLE_INTERNAL_SHARING:
        return False

    # Check for shared access
    share = InternalShare.query.filter_by(
        recording_id=recording.id,
        shared_with_user_id=user.id
    ).first()

    if not share:
        return False

    # If edit permission is required, check for it
    if require_edit:
        # First check if share directly grants edit permission
        if share.can_edit:
            pass  # Has direct edit permission
        else:
            # Check if user is a group admin for any group tag on this recording
            # This grants edit permission even if share.can_edit is False
            is_group_admin_for_recording = db.session.query(GroupMembership).join(
                Tag, Tag.group_id == GroupMembership.group_id
            ).join(
                RecordingTag, RecordingTag.tag_id == Tag.id
            ).filter(
                RecordingTag.recording_id == recording.id,
                GroupMembership.user_id == user.id,
                GroupMembership.role == 'admin',
                Tag.group_id.isnot(None),
                db.or_(Tag.auto_share_on_apply == True, Tag.share_with_group_lead == True)
            ).first()

            if not is_group_admin_for_recording:
                return False

    # If reshare permission is required, check for it
    if require_reshare and not share.can_reshare:
        return False

    # User has at least view access
    return True


def get_user_recording_status(recording, user):
    """
    Get the inbox and highlighted status for a recording from a user's perspective.

    For owners: Returns status from Recording model
    For shared recipients: Returns status from SharedRecordingState (creates default if not exists)

    Args:
        recording: Recording object
        user: User object (typically current_user)

    Returns:
        Tuple of (is_inbox, is_highlighted)
    """
    # Owner uses the Recording model's global fields
    if recording.user_id == user.id:
        return (recording.is_inbox, recording.is_highlighted)

    # Shared recipient uses SharedRecordingState
    state = SharedRecordingState.query.filter_by(
        recording_id=recording.id,
        user_id=user.id
    ).first()

    if state:
        return (state.is_inbox, state.is_highlighted)
    else:
        # Return defaults if no state exists yet (inbox=True, highlighted=False)
        return (True, False)


def set_user_recording_status(recording, user, is_inbox=None, is_highlighted=None):
    """
    Set the inbox and/or highlighted status for a recording from a user's perspective.

    For owners: Updates Recording model
    For shared recipients: Updates or creates SharedRecordingState

    Args:
        recording: Recording object
        user: User object (typically current_user)
        is_inbox: Boolean or None (None means don't change)
        is_highlighted: Boolean or None (None means don't change)

    Returns:
        Tuple of (is_inbox, is_highlighted) after update
    """
    # Owner updates the Recording model's global fields
    if recording.user_id == user.id:
        if is_inbox is not None:
            recording.is_inbox = is_inbox
        if is_highlighted is not None:
            recording.is_highlighted = is_highlighted
        db.session.commit()
        return (recording.is_inbox, recording.is_highlighted)

    # Shared recipient uses SharedRecordingState
    state = SharedRecordingState.query.filter_by(
        recording_id=recording.id,
        user_id=user.id
    ).first()

    if not state:
        # Create new state with defaults
        state = SharedRecordingState(
            recording_id=recording.id,
            user_id=user.id,
            is_inbox=True,
            is_highlighted=False
        )
        db.session.add(state)

    # Update the requested fields
    if is_inbox is not None:
        state.is_inbox = is_inbox
    if is_highlighted is not None:
        state.is_highlighted = is_highlighted

    db.session.commit()
    return (state.is_inbox, state.is_highlighted)


def enrich_recording_dict_with_user_status(recording_dict, recording, user):
    """
    Enrich a recording dictionary with per-user status (inbox, highlighted).

    This should be called after recording.to_dict() or recording.to_list_dict()
    to replace the owner's status with the current user's per-user status.

    Args:
        recording_dict: Dictionary from recording.to_dict() or recording.to_list_dict()
        recording: Recording object
        user: User object (typically current_user)

    Returns:
        The enriched recording_dict (modified in place, but also returned for convenience)
    """
    user_inbox, user_highlighted = get_user_recording_status(recording, user)
    recording_dict['is_inbox'] = user_inbox
    recording_dict['is_highlighted'] = user_highlighted
    return recording_dict


app = Flask(__name__, 
            template_folder='../templates',
            static_folder='../static')
# Use environment variables or default paths for Docker compatibility
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:////data/instance/transcriptions.db')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/data/uploads')
# MAX_CONTENT_LENGTH will be set dynamically after database initialization
# Set a secret key for session management and CSRF protection
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-dev-key-change-in-production')

# Apply ProxyFix to handle headers from a reverse proxy (like Nginx or Caddy)
# This is crucial for request.is_secure to work correctly behind an SSL-terminating proxy.
trusted_proxy_hops = int(os.environ.get('TRUSTED_PROXY_HOPS', '1'))
app.wsgi_app = ProxyFix(
    app.wsgi_app, 
    x_for=trusted_proxy_hops, 
    x_proto=trusted_proxy_hops, 
    x_host=trusted_proxy_hops, 
    x_prefix=trusted_proxy_hops
)

# --- Secure Session Cookie Configuration ---
# For local network usage, disable secure cookies to allow HTTP connections
# Only enable secure cookies in production when HTTPS is actually being used
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP for local network usage
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Still protect against XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF protection

# Import database instance from extracted module
from src.database import db
db.init_app(app)

# Import all models from extracted modules
from src.models import (
    User, Speaker, Recording, TranscriptChunk, Share, InternalShare,
    SharedRecordingState, Group, GroupMembership, Tag, RecordingTag,
    Event, TranscriptTemplate, InquireSession, SystemSetting
)

# Import utility functions from extracted modules
from src.utils import (
    auto_close_json, safe_json_loads, preprocess_json_escapes, extract_json_object,
    md_to_html, sanitize_html, local_datetime_filter, password_check,
    add_column_if_not_exists, is_safe_url
)

# Import service layer functions
from src.services.embeddings import (
    get_embedding_model, chunk_transcription, generate_embeddings,
    serialize_embedding, deserialize_embedding, get_accessible_recording_ids,
    process_recording_chunks, basic_text_search_chunks, semantic_search_chunks
)
from src.services.llm import (
    is_gpt5_model, is_using_openai_api, call_llm_completion, format_api_error_message
)
from src.services.document import process_markdown_to_docx
from src.services.retention import (
    is_recording_exempt_from_deletion, get_retention_days_for_recording, process_auto_deletion
)
from src.services.calendar import generate_ics_content, escape_ical_text
from src.services.speaker import (
    update_speaker_usage, identify_speakers_from_text, identify_unidentified_speakers_from_text
)

# Import background task functions
from src.tasks.processing import (
    generate_title_task, generate_summary_only_task, extract_events_from_transcript,
    extract_audio_from_video, transcribe_audio_asr, transcribe_audio_task,
    transcribe_single_file, transcribe_with_chunking
)

# Import configuration helpers
from src.config.version import get_version

# Initialize Flask-Login and other extensions
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'
bcrypt = Bcrypt()
bcrypt.init_app(app)
limiter.init_app(app)  # Initialize the limiter (uses in-memory storage by default)

csrf = CSRFProtect(app)

# Add context processor to make 'now' available to all templates
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

@app.context_processor
def inject_group_admin_status():
    """Inject is_group_admin flag into all templates."""
    from flask_login import current_user
    from src.models.organization import GroupMembership

    is_group_admin = False
    if current_user.is_authenticated:
        is_group_admin = GroupMembership.query.filter_by(
            user_id=current_user.id,
            role='admin'
        ).first() is not None

    return {'is_group_admin': is_group_admin}

# --- Timezone Formatting Filter ---
@app.template_filter('localdatetime')
def local_datetime_filter(dt):
    """Format a UTC datetime object to the user's local timezone."""
    if dt is None:
        return ""
    
    # Get timezone from .env, default to UTC
    user_tz_name = os.environ.get('TIMEZONE', 'UTC')
    try:
        user_tz = pytz.timezone(user_tz_name)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.utc
        app.logger.warning(f"Invalid TIMEZONE '{user_tz_name}' in .env. Defaulting to UTC.")

    # If the datetime object is naive, assume it's UTC
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)

    # Convert to the user's timezone
    local_dt = dt.astimezone(user_tz)
    
    # Format it nicely
    return format_datetime(local_dt, format='medium', locale='en_US')

# Ensure upload and instance directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Ensure upload and instance directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# Assuming the instance folder is handled correctly by Flask or created by setup.sh
# os.makedirs(os.path.dirname(app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '/')), exist_ok=True)


# --- User loader for Flask-Login ---
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- Embedding and Chunking Utilities ---

from src.api.auth import auth_bp, init_auth_extensions
from src.api.shares import shares_bp, init_shares_helpers
from src.api.recordings import recordings_bp, init_recordings_helpers
from src.api.tags import tags_bp, init_tags_helpers
from src.api.groups import groups_bp, init_groups_helpers
from src.api.admin import admin_bp, init_admin_helpers
from src.api.speakers import speakers_bp, init_speakers_helpers
from src.api.inquire import inquire_bp, init_inquire_helpers
from src.api.templates import templates_bp, init_templates_helpers
from src.api.events import events_bp, init_events_helpers
from src.api.system import system_bp, init_system_helpers

# Database initialization (extracted to src/init_db.py)
from src.init_db import initialize_database
with app.app_context():
    initialize_database(app)

# Application configuration (extracted to src/config/app_config.py)
from src.config.app_config import initialize_config
client, chunking_service, version = initialize_config(app)

# Initialize blueprint helpers (inject extensions and utility functions)
init_auth_extensions(bcrypt, csrf, limiter)
init_shares_helpers(has_recording_access)
init_recordings_helpers(has_recording_access=has_recording_access, get_user_recording_status=get_user_recording_status, set_user_recording_status=set_user_recording_status, enrich_recording_dict_with_user_status=enrich_recording_dict_with_user_status, bcrypt=bcrypt, csrf=csrf, limiter=limiter, chunking_service=chunking_service)
init_tags_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_groups_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_admin_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_speakers_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_inquire_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_templates_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_events_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_system_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter, chunking_service=chunking_service)

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(shares_bp)
app.register_blueprint(recordings_bp)
app.register_blueprint(tags_bp)
app.register_blueprint(groups_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(speakers_bp)
app.register_blueprint(inquire_bp)
app.register_blueprint(templates_bp)
app.register_blueprint(events_bp)
app.register_blueprint(system_bp)

# File monitor and scheduler initialization functions below

# Startup functions (extracted to src/config/startup.py)
from src.config.startup import initialize_file_monitor, get_file_monitor_functions, initialize_auto_deletion_scheduler, run_startup_tasks

# Run startup tasks
run_startup_tasks(app)

# --- No-Crawl System: HTTP Headers ---
@app.after_request
def add_no_crawl_headers(response):
    """
    Add HTTP headers to discourage search engine crawling and indexing.
    This provides defense-in-depth alongside robots.txt and meta tags.
    """
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet, noimageindex'
    return response

# --- No-Crawl System: Serve robots.txt ---
@app.route('/robots.txt')
def robots_txt():
    """Serve robots.txt to instruct crawlers not to index the site."""
    return send_file(os.path.join(app.static_folder, 'robots.txt'), mimetype='text/plain')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    args = parser.parse_args()

    # Consider using waitress or gunicorn for production
    # waitress-serve --host 0.0.0.0 --port 8899 app:app
    # For development:
    app.run(host='0.0.0.0', port=8899, debug=args.debug)
