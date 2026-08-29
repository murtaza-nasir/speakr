# Speakr - Audio Transcription and Summarization App
import os
import sys
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash, Response, make_response
from urllib.parse import urlparse, urljoin, quote
from email.utils import encode_rfc2231
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

# Video retention - when enabled, video files keep their video stream for playback
VIDEO_RETENTION = os.environ.get('VIDEO_RETENTION', 'false').lower() == 'true'

# Log embedding status on startup. Two paths can power Inquire mode: a local
# sentence-transformers model (full image) or an OpenAI-compatible HTTP
# provider (works in the lite image too, as long as scikit-learn is present
# for cosine similarity, which it is in both images).
_inquire_uses_api = bool(os.environ.get('EMBEDDING_BASE_URL', '').strip())
try:
    from sklearn.metrics.pairwise import cosine_similarity as _has_sklearn  # noqa: F401
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False

if ENABLE_INQUIRE_MODE and EMBEDDINGS_AVAILABLE:
    print("✅ Inquire Mode: Full semantic search enabled (local embedding model)")
elif ENABLE_INQUIRE_MODE and _inquire_uses_api and _SKLEARN_AVAILABLE:
    print("✅ Inquire Mode: Full semantic search enabled (API embedding provider)")
elif ENABLE_INQUIRE_MODE and _inquire_uses_api and not _SKLEARN_AVAILABLE:
    print("⚠️  Inquire Mode: Basic text search only (scikit-learn missing, cannot rank API embeddings)")
elif ENABLE_INQUIRE_MODE and not EMBEDDINGS_AVAILABLE:
    print("⚠️  Inquire Mode: Basic text search only (embedding dependencies not available)")
    print("   To enable semantic search, install sentence-transformers locally OR set EMBEDDING_BASE_URL to use an OpenAI-compatible API.")
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

# Log video retention status
if VIDEO_RETENTION:
    print("✅ Video retention: Enabled (video files preserve video stream for playback)")
else:
    print("ℹ️  Video retention: Disabled (video uploads extract audio only)")

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
# RATELIMIT_ENABLED (default true) lets the test suite and any deployment that
# fronts its own rate limiting turn Flask-Limiter off. The per-endpoint auth
# limits (login/register/password-reset) are applied via the rate_limit
# decorator in src/api/auth.py and src/api/tokens.py.
_RATELIMIT_ENABLED = os.environ.get('RATELIMIT_ENABLED', 'true').lower() != 'false'
limiter = Limiter(
    get_remote_address,
    app=None,  # Defer initialization
    default_limits=["5000 per day", "1000 per hour"],
    enabled=_RATELIMIT_ENABLED,
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

# Static files ship with no Cache-Control by default, which lets browsers
# apply heuristic freshness (often days/weeks for rarely-changed files).
# After an image upgrade that served users a mix of new templates and stale
# JS/locale JSON — a blank UI with "Translation not found" spam (issue #357).
# max-age=0 forces a cheap conditional revalidation (304 unless changed).
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# SQLite concurrency settings for multi-worker job queue
if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {
            'timeout': 30,  # Wait up to 30 seconds for locked database
            'check_same_thread': False  # Allow multi-threaded access
        },
        'pool_pre_ping': True  # Verify connections before use
    }
# MAX_CONTENT_LENGTH will be set dynamically after database initialization
# Set a secret key for session management and CSRF protection.

# The old code fell back to a hardcoded constant when SECRET_KEY was unset.
# That key signs the session cookie AND itsdangerous password-reset tokens,
# so a publicly-known value lets anyone forge a session cookie for any
# user_id (instant admin takeover) and mint valid reset tokens. resolve_secret_key
# refuses the known-bad default and, when nothing is configured, auto-generates
# a strong per-deployment key and persists it — so a self-hoster who never sets
# the env var is secure by default instead of silently exploitable. (The key
# only signs sessions/tokens; it does not encrypt stored data, so a lost key
# means a one-time re-login, never data loss.)
from src.utils.security import resolve_secret_key as _resolve_secret_key

try:
    _secret_key, _secret_key_action = _resolve_secret_key(
        os.environ.get('SECRET_KEY'),
        app.config['SQLALCHEMY_DATABASE_URI'],
        key_file=os.environ.get('SECRET_KEY_FILE') or None,
    )
except ValueError as _sk_exc:
    raise RuntimeError(str(_sk_exc))
app.config['SECRET_KEY'] = _secret_key
if _secret_key_action == 'generated':
    app.logger.warning(
        "SECRET_KEY was not set; generated a strong random key and persisted "
        "it under the instance directory. It is included in a normal data-volume "
        "backup; set SECRET_KEY explicitly for multi-host or key-rotation setups."
    )
elif _secret_key_action == 'ephemeral':
    app.logger.error(
        "SECRET_KEY not set and an auto-generated key could not be persisted. "
        "Using an ephemeral key: sessions will not survive a restart. Set "
        "SECRET_KEY to fix this."
    )

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
# Default is False so http://localhost / LAN deployments continue to
# work out of the box. Production HTTPS deployments should set
# SESSION_COOKIE_SECURE=true so the session cookie is only sent over
# TLS. Default-on would silently log users out on a fresh HTTP install,
# which is a worse failure mode for the most common self-hosted setup.
app.config['SESSION_COOKIE_SECURE'] = (
    os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
)
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Still protect against XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF protection

# --- Security response headers ---
# Set at the app level so a deployment WITHOUT a hardening reverse proxy is
# still protected (previously the app set only X-Robots-Tag, leaving direct /
# plain-proxy deployments with no clickjacking, MIME-sniffing, or CSP defense).
# All are `setdefault`-applied in add_security_headers so an operator's proxy
# can still override them.
#
# CSP note: the frontend loads Vue's full build (in-DOM template compiler, so
# script execution needs 'unsafe-eval') and bootstraps from inline <script>
# blocks in every template ('unsafe-inline'), so the script-src cannot be
# locked down without a frontend build step that precompiles templates and
# adds per-response nonces. The policy still meaningfully constrains object-src,
# base-uri, form-action, frame-ancestors, and the connect/img/media/font
# origins. Override the whole policy via CONTENT_SECURITY_POLICY, or disable
# the block with SECURITY_HEADERS_ENABLED=false.
SECURITY_HEADERS_ENABLED = os.environ.get('SECURITY_HEADERS_ENABLED', 'true').lower() == 'true'
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob: data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)
CONTENT_SECURITY_POLICY = os.environ.get('CONTENT_SECURITY_POLICY', _DEFAULT_CSP).strip()
# HSTS is only meaningful (and only emitted) over HTTPS. Configurable so
# operators terminating TLS elsewhere can tune or disable it.
HSTS_HEADER_VALUE = os.environ.get(
    'HSTS_HEADER', 'max-age=63072000; includeSubDomains'
).strip()
if app.config['SESSION_COOKIE_SECURE']:
    app.logger.info("Session cookies marked Secure (HTTPS-only)")
else:
    app.logger.info(
        "Session cookies are NOT marked Secure. Set "
        "SESSION_COOKIE_SECURE=true for production HTTPS deployments."
    )

# Import database instance from extracted module
from src.database import db
db.init_app(app)

# Import all models from extracted modules
from src.models import (
    User, Speaker, Recording, TranscriptChunk, Share, InternalShare,
    SharedRecordingState, Group, GroupMembership, Tag, RecordingTag,
    Event, TranscriptTemplate, InquireSession, SystemSetting, PushSubscription,
    APIToken, NamingTemplate, Folder, SpeakerSnippet, ShareAuditLog,
    ProcessingJob, TokenUsage, TranscriptionUsage
)

# Import utility functions from extracted modules
from src.utils import (
    auto_close_json, safe_json_loads, preprocess_json_escapes, extract_json_object,
    md_to_html, sanitize_html, password_check,
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

# Import background task functions
from src.tasks.processing import (
    generate_title_task, generate_summary_only_task, extract_events_from_transcript,
    extract_audio_from_video, transcribe_audio_task, transcribe_with_connector,
    transcribe_chunks_with_connector, transcribe_incognito
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

# Exempt frequently-polled status endpoints from rate limiting
@limiter.request_filter
def exempt_status_endpoints():
    """Exempt status polling endpoints from rate limiting."""
    from flask import request
    # Exempt status endpoints that are polled frequently during processing
    if '/status' in request.path and request.method == 'GET':
        return True
    if request.path.endswith('/batch-status') and request.method == 'POST':
        return True
    # Exempt job queue status polling (polled every 5-30 seconds during processing)
    if request.path == '/api/recordings/job-queue-status' and request.method == 'GET':
        return True
    # Exempt server-side recording-session endpoints (#287). These are the
    # RECOMMENDED path for multi-hour recordings, which stream one chunk every
    # few seconds — at the default 5s cadence a recording emits ~720 chunks/hour,
    # and the global IP limit (1000/hour, 5000/day) is shared with all the
    # user's other requests, so a long session would otherwise start getting
    # 429'd partway through and lose its streaming guarantee. The endpoints are
    # login-protected, ownership-checked, and already bounded per user by
    # RECORDING_SESSION_MAX_BYTES_PER_USER / RECORDING_SESSION_MAX_CHUNK_BYTES,
    # so IP-based rate limiting is the wrong control here.
    if request.path.startswith('/upload/session'):
        return True
    return False

csrf = CSRFProtect(app)

# Disable Flask-WTF's automatic CSRF check so we can run our own
# token-aware version below. The replacement still calls csrf.protect()
# for browser/session requests; the bypass only fires when a valid
# API token is presented via header (never query string).
#
# GHSA-x4q4-3ww4-h329 (Irina Iarlykanova): the previous
# ``csrf_exempt_for_api_tokens`` hook called ``csrf.exempt(view_func)``
# from a request handler. That mutates Flask-WTF's process-global
# ``_exempt_views`` set permanently, so any cross-origin request with
# ``?token=anything`` would silently disable CSRF on the targeted
# endpoint for the rest of the worker's lifetime, allowing follow-up
# CSRF attacks. Replaced with a per-request decision below.
app.config['WTF_CSRF_CHECK_DEFAULT'] = False


@app.before_request
def csrf_token_aware_check():
    """Per-request CSRF check that allows valid header-only API tokens
    to bypass CSRF, without ever mutating Flask-WTF's exemption set.

    The check honours all the registration-time exemptions Flask-WTF
    already knows about (``csrf.exempt(blueprint)`` and
    ``csrf.exempt(view_func)`` called at module load time), and only
    skips CSRF for the specific request currently being handled.

    Tokens are accepted only from the Authorization, X-API-Token, and
    API-Token headers. The query-string token is intentionally NOT
    honoured here because a Simple Cross-Origin Request can carry one
    without triggering CORS preflight, which is exactly the attack
    vector in GHSA-x4q4-3ww4-h329.
    """
    if not app.config.get('WTF_CSRF_ENABLED', True):
        return
    if request.method not in app.config.get(
        'WTF_CSRF_METHODS', {'POST', 'PUT', 'PATCH', 'DELETE'}
    ):
        return
    if not request.endpoint:
        return

    # Blueprint-level registration-time exemptions (e.g. api_v1_bp).
    # Note that csrf._exempt_blueprints holds Blueprint OBJECTS, so we
    # resolve the request's blueprint name through app.blueprints, the
    # same way Flask-WTF's own protect() hook does.
    if app.blueprints.get(request.blueprint) in csrf._exempt_blueprints:
        return

    # View-level registration-time exemptions (e.g. share_target).
    # Flask-WTF identifies exempt views by f"{__module__}.{__name__}";
    # match that exactly so any view registered via csrf.exempt() at
    # import time is honoured here.
    view = app.view_functions.get(request.endpoint)
    if view is not None:
        dest = f"{view.__module__}.{view.__name__}"
        if dest in csrf._exempt_views:
            return

    # If a valid header API token is presented, this request is from a
    # programmatic client and CSRF does not apply. The token is validated
    # against the DB by load_user_from_token_headers_only().
    from src.utils.token_auth import load_user_from_token_headers_only
    if load_user_from_token_headers_only() is not None:
        return

    # Otherwise, fall through to the standard CSRF check. If validation
    # fails, csrf.protect() raises a CSRFError which Flask-WTF's error
    # handler turns into a 400 response.
    csrf.protect()


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(_error):
    """Keep oversized requests JSON-shaped even when parsing fails early."""
    limit_mb = float(app.config['MAX_CONTENT_LENGTH']) / (1024 * 1024)
    return jsonify({
        'error': f'File too large. Maximum size is {limit_mb:.0f} MB.',
        'max_size_mb': limit_mb,
        'effective_limit_mb': limit_mb,
        'audio_only_mode': False,
    }), 413


# Add context processor to make 'now' available to all templates
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

# Expose the app version to templates so the service worker registration URL
# (and therefore its cache namespace) changes on every release, forcing the
# browser to install the new worker and refetch the app shell (issue #357).
_app_version = get_version()

@app.context_processor
def inject_app_version():
    return {'app_version': _app_version}

@app.context_processor
def inject_help_center_link():
    """Optional custom help link in the user menu (#371).

    Admins hosting their own FAQ/tutorial page set HELP_CENTER_URL (and
    optionally HELP_CENTER_LABEL) to surface it prominently for users.
    """
    return {
        'help_center_url': os.environ.get('HELP_CENTER_URL', '').strip(),
        'help_center_label': os.environ.get('HELP_CENTER_LABEL', '').strip(),
    }

@app.context_processor
def inject_group_admin_status():
    """Inject is_group_admin flag and the admin-group list into all templates.

    user_admin_groups feeds the shared tag/folder modals (group assignment
    dropdown), which render on the account page AND the main app now that
    tags/folders can be created inline from the upload dialog.
    """
    from flask_login import current_user
    from src.models.organization import GroupMembership

    user_admin_groups = []
    if current_user.is_authenticated:
        memberships = GroupMembership.query.filter_by(
            user_id=current_user.id,
            role='admin'
        ).all()
        user_admin_groups = [
            {'id': m.group.id, 'name': m.group.name}
            for m in memberships if m.group
        ]

    return {
        'is_group_admin': bool(user_admin_groups),
        'user_admin_groups': user_admin_groups,
    }

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


@login_manager.request_loader
def load_user_from_request(request):
    """
    Load user from API token in the request.

    This enables token-based authentication for API access
    (e.g., from curl, n8n, Zapier, etc.).
    """
    from src.utils.token_auth import load_user_from_token
    return load_user_from_token()


@login_manager.unauthorized_handler
def handle_unauthorized():
    """API requests get a JSON 401; browser page loads keep the login redirect.

    Flask-Login's default is a 302 to /login for everything. Nearly every
    HTTP client follows redirects and treats the resulting 200 text/html
    login page as success, so integrations with a missing/invalid/expired
    token "pass" their connection checks and fail later with confusing
    JSON parse errors — or worse, report an upload as successful after the
    redirect silently dropped the multipart body (issue #333).

    A request is API-shaped when it targets an /api/ path or presented an
    API token in any of the forms token_auth accepts; a token client
    deserves a 401 whichever path it hits.
    """
    presented_token = bool(
        request.headers.get('Authorization', '').startswith('Bearer ')
        or request.headers.get('X-API-Token')
        or request.headers.get('API-Token')
        or request.args.get('token')
    )
    if request.path.startswith('/api/') or presented_token:
        response = jsonify({'error': 'Authentication required: missing, invalid, expired, or revoked API token or session'})
        response.status_code = 401
        response.headers['WWW-Authenticate'] = 'Bearer'
        return response

    # Browser page load: replicate Flask-Login's default behavior
    # (flash the login message, redirect to the login view with ?next=).
    from flask_login.utils import login_url as _login_url
    if login_manager.login_message:
        flash(login_manager.login_message, category=login_manager.login_message_category)
    return redirect(_login_url(url_for(login_manager.login_view), request.url))


# --- Embedding and Chunking Utilities ---

from src.api.auth import auth_bp, init_auth_extensions
from src.api.tokens import tokens_bp, init_tokens_helpers
from src.api.shares import shares_bp, init_shares_helpers
from src.api.recordings import recordings_bp, init_recordings_helpers
from src.api.tags import tags_bp, init_tags_helpers
from src.api.folders import folders_bp, init_folders_helpers
from src.api.groups import groups_bp, init_groups_helpers
from src.api.admin import admin_bp, init_admin_helpers
from src.api.speakers import speakers_bp, init_speakers_helpers
from src.api.inquire import inquire_bp, init_inquire_helpers
from src.api.templates import templates_bp, init_templates_helpers
from src.api.naming_templates import naming_templates_bp
from src.api.export_templates import export_templates_bp
from src.api.initial_prompt_templates import initial_prompt_templates_bp
from src.api.events import events_bp, init_events_helpers
from src.api.system import system_bp, init_system_helpers
from src.api.push_notifications import push_bp
from src.api.api_v1 import api_v1_bp, init_api_v1_helpers
from src.api.recording_sessions import recording_sessions_bp
from src.api.webhooks import webhooks_bp

# Database initialization (extracted to src/init_db.py)
from src.init_db import initialize_database
with app.app_context():
    initialize_database(app)

# Application configuration (extracted to src/config/app_config.py)
from src.config.app_config import initialize_config
client, chunking_service, version = initialize_config(app)

# Initialize blueprint helpers (inject extensions and utility functions)
init_auth_extensions(bcrypt, csrf, limiter)
init_tokens_helpers(bcrypt, csrf, limiter)
init_shares_helpers(has_recording_access)
init_recordings_helpers(has_recording_access=has_recording_access, get_user_recording_status=get_user_recording_status, set_user_recording_status=set_user_recording_status, enrich_recording_dict_with_user_status=enrich_recording_dict_with_user_status, bcrypt=bcrypt, csrf=csrf, limiter=limiter, chunking_service=chunking_service)
init_tags_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_folders_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_groups_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_admin_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_speakers_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_inquire_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_templates_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_events_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter)
init_system_helpers(has_recording_access=has_recording_access, bcrypt=bcrypt, csrf=csrf, limiter=limiter, chunking_service=chunking_service)
init_api_v1_helpers(has_recording_access=has_recording_access, get_user_recording_status=get_user_recording_status, set_user_recording_status=set_user_recording_status, enrich_recording_dict_with_user_status=enrich_recording_dict_with_user_status, bcrypt=bcrypt, csrf=csrf, limiter=limiter, chunking_service=chunking_service)

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(tokens_bp)
app.register_blueprint(shares_bp)
app.register_blueprint(recordings_bp)
app.register_blueprint(tags_bp)
app.register_blueprint(folders_bp)
app.register_blueprint(groups_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(speakers_bp)
app.register_blueprint(inquire_bp)
app.register_blueprint(templates_bp)
app.register_blueprint(naming_templates_bp)
app.register_blueprint(export_templates_bp)
app.register_blueprint(initial_prompt_templates_bp)
app.register_blueprint(events_bp)
app.register_blueprint(system_bp)
app.register_blueprint(push_bp)
app.register_blueprint(api_v1_bp)
csrf.exempt(api_v1_bp)  # API v1 uses token auth, not CSRF

# Recording sessions (#287 c/d): the streaming client POSTs binary chunk
# bodies via fetch with the session cookie. CSRF tokens are easy to attach
# from JS, so we keep the protection on by default — but each individual
# chunk request is a fetch call from our own SPA, which already carries
# the token, so no exemption is needed here.
app.register_blueprint(recording_sessions_bp)

# Webhooks (#275): under /api/v1, CSRF-exempt so API-token clients can
# manage their endpoints programmatically. SPA callers still attach the
# X-CSRFToken header for cookie-auth requests.
app.register_blueprint(webhooks_bp)
csrf.exempt(webhooks_bp)

# PWA Web Share Target (issue #285): the native share sheet cannot round-trip
# a CSRF token, so the share-target endpoint is exempted. Authentication still
# happens via the session cookie carried by the browser; @login_required
# bounces unauthenticated visitors to /login.
from src.api.recordings import share_target as _share_target_view
csrf.exempt(_share_target_view)

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


@app.after_request
def add_security_headers(response):
    """Set baseline security headers so the app is safe even without a
    hardening reverse proxy. Uses setdefault so a proxy that already sets a
    header wins (no duplicates). See the SECURITY_HEADERS config block above.
    """
    if not SECURITY_HEADERS_ENABLED:
        return response
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Deny sensors the app never uses; allow mic + screen capture (recording).
    response.headers.setdefault(
        'Permissions-Policy',
        'geolocation=(), camera=(), microphone=(self), display-capture=(self), interest-cohort=()'
    )
    if CONTENT_SECURITY_POLICY:
        response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    # HSTS only over HTTPS (request.is_secure reflects X-Forwarded-Proto via ProxyFix).
    if HSTS_HEADER_VALUE and request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', HSTS_HEADER_VALUE)
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
