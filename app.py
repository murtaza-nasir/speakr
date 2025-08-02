# Speakr - Audio Transcription and Summarization App
import os
import sys
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash, Response
from urllib.parse import urlparse, urljoin
try:
    from flask import Markup
except ImportError:
    from markupsafe import Markup
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from openai import OpenAI # Keep using the OpenAI library
import json
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import select
import threading
from dotenv import load_dotenv # Import load_dotenv
import httpx 
import re
import subprocess
import mimetypes
import markdown
import bleach
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
from audio_chunking import AudioChunkingService, ChunkProcessingError, ChunkingNotSupportedError

# Load environment variables from .env file
load_dotenv()

# Configure logging
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(log_level)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# Get the root logger
root_logger = logging.getLogger()
root_logger.setLevel(log_level)
root_logger.addHandler(handler)

# Also configure Flask's logger
app_logger = logging.getLogger('werkzeug')
app_logger.setLevel(log_level)
app_logger.addHandler(handler)

# --- Rate Limiting Setup ---
limiter = Limiter(
    get_remote_address,
    app=None,  # Defer initialization
    default_limits=["200 per day", "50 per hour"]
)

def auto_close_json(json_string):
    """
    Attempts to close an incomplete JSON string by appending necessary brackets and braces.
    This is a simplified parser and may not handle all edge cases, but is
    designed to fix unterminated strings from API responses.
    """
    if not isinstance(json_string, str):
        return json_string

    stack = []
    in_string = False
    escape_next = False

    for char in json_string:
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        if char == '"':
            # We don't handle escaped quotes inside strings perfectly,
            # but this is a simple heuristic.
            if not escape_next:
                in_string = not in_string

        if not in_string:
            if char == '{':
                stack.append('}')
            elif char == '[':
                stack.append(']')
            elif char == '}':
                if stack and stack[-1] == '}':
                    stack.pop()
            elif char == ']':
                if stack and stack[-1] == ']':
                    stack.pop()

    # If we are inside a string at the end, close it.
    if in_string:
        json_string += '"'
    
    # Close any remaining open structures
    while stack:
        json_string += stack.pop()

    return json_string

def safe_json_loads(json_string, fallback_value=None):
    """
    Safely parse JSON with preprocessing to handle common LLM JSON formatting issues.
    
    Args:
        json_string (str): The JSON string to parse
        fallback_value: Value to return if parsing fails (default: None)
    
    Returns:
        Parsed JSON object or fallback_value if parsing fails
    """
    if not json_string or not isinstance(json_string, str):
        app.logger.warning(f"Invalid JSON input: {type(json_string)} - {json_string}")
        return fallback_value
    
    # Step 1: Clean the input string
    cleaned_json = json_string.strip()
    
    # Step 2: Extract JSON from markdown code blocks if present
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', cleaned_json, re.DOTALL)
    if json_match:
        cleaned_json = json_match.group(1).strip()
    
    # Step 3: Try multiple parsing strategies
    parsing_strategies = [
        # Strategy 1: Direct parsing (for well-formed JSON)
        lambda x: json.loads(x),
        
        # Strategy 2: Fix common escape issues
        lambda x: json.loads(preprocess_json_escapes(x)),
        
        # Strategy 3: Use ast.literal_eval as fallback for simple cases
        lambda x: ast.literal_eval(x) if x.startswith(('{', '[')) else None,
        
        # Strategy 4: Extract JSON object/array using regex
        lambda x: json.loads(extract_json_object(x)),
        
        # Strategy 5: Auto-close incomplete JSON and parse
        lambda x: json.loads(auto_close_json(x)),
    ]
    
    for i, strategy in enumerate(parsing_strategies):
        try:
            result = strategy(cleaned_json)
            if result is not None:
                if i > 0:  # Log if we had to use a fallback strategy
                    app.logger.info(f"JSON parsed successfully using strategy {i+1}")
                return result
        except (json.JSONDecodeError, ValueError, SyntaxError) as e:
            if i == 0:  # Only log the first failure to avoid spam
                app.logger.debug(f"JSON parsing strategy {i+1} failed: {e}")
            continue
    
    # All strategies failed
    app.logger.error(f"All JSON parsing strategies failed for: {cleaned_json[:200]}...")
    return fallback_value

def preprocess_json_escapes(json_string):
    """
    Preprocess JSON string to fix common escape issues from LLM responses.
    Uses a more sophisticated approach to handle nested quotes properly.
    """
    if not json_string:
        return json_string
    
    result = []
    i = 0
    in_string = False
    escape_next = False
    expecting_value = False  # Track if we're expecting a value (after :)
    
    while i < len(json_string):
        char = json_string[i]
        
        if escape_next:
            # This character is escaped, add it as-is
            result.append(char)
            escape_next = False
        elif char == '\\':
            # This is an escape character
            result.append(char)
            escape_next = True
        elif char == ':' and not in_string:
            # We found a colon, next string will be a value
            result.append(char)
            expecting_value = True
        elif char == ',' and not in_string:
            # We found a comma, reset expecting_value
            result.append(char)
            expecting_value = False
        elif char == '"':
            if not in_string:
                # Starting a string
                in_string = True
                result.append(char)
            else:
                # We're in a string, check if this quote should be escaped
                # Look ahead to see if this is the end of the string value
                j = i + 1
                while j < len(json_string) and json_string[j].isspace():
                    j += 1
                
                # For keys (not expecting_value), only end on colon
                # For values (expecting_value), end on comma, closing brace, or closing bracket
                if expecting_value:
                    end_chars = ',}]'
                else:
                    end_chars = ':'
                
                if j < len(json_string) and json_string[j] in end_chars:
                    # This is the end of the string
                    in_string = False
                    result.append(char)
                    if not expecting_value:
                        # We just finished a key, next will be expecting value
                        expecting_value = True
                else:
                    # This is an inner quote that should be escaped
                    result.append('\\"')
        else:
            result.append(char)
        
        i += 1
    
    return ''.join(result)

def extract_json_object(text):
    """
    Extract the first complete JSON object or array from text using regex.
    """
    # Look for JSON object
    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
    if obj_match:
        return obj_match.group(0)
    
    # Look for JSON array
    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
    if arr_match:
        return arr_match.group(0)
    
    # Return original if no JSON structure found
    return text

# Initialize Flask-Bcrypt
bcrypt = Bcrypt()

# Helper function to convert markdown to HTML
def sanitize_html(text):
    """
    Sanitize HTML content to prevent XSS attacks and code execution.
    This function removes dangerous content while preserving safe formatting.
    """
    if not text:
        return ""
    
    # First, remove any template syntax that could be dangerous
    # Remove Jinja2/Flask template syntax
    text = re.sub(r'\{\{.*?\}\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\{%.*?%\}', '', text, flags=re.DOTALL)
    
    # Remove other template-like syntax
    text = re.sub(r'<%.*?%>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\?.*?\?>', '', text, flags=re.DOTALL)
    
    # Define allowed tags and attributes for safe HTML
    allowed_tags = [
        'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'a', 'img', 'table', 'thead', 
        'tbody', 'tr', 'th', 'td', 'dl', 'dt', 'dd', 'div', 'span', 'hr', 'sup', 'sub'
    ]
    
    allowed_attributes = {
        'a': ['href', 'title'],
        'img': ['src', 'alt', 'title', 'width', 'height'],
        'code': ['class'],  # For syntax highlighting
        'pre': ['class'],   # For syntax highlighting
        'div': ['class'],   # For code blocks
        'span': ['class'],  # For syntax highlighting
        'th': ['align'],
        'td': ['align'],
        'table': ['class']
    }
    
    # Sanitize the HTML to remove dangerous content
    sanitized_html = bleach.clean(
        text,
        tags=allowed_tags,
        attributes=allowed_attributes,
        protocols=['http', 'https', 'mailto'],
        strip=True  # Strip disallowed tags instead of escaping them
    )
    
    return sanitized_html

def md_to_html(text):
    if not text:
        return ""
    
    # Pre-process the text to ensure proper list formatting
    def fix_list_spacing(text):
        lines = text.split('\n')
        result = []
        in_list = False
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # Check if this line is a list item (starts with -, *, +, or number.)
            is_list_item = (
                stripped.startswith(('- ', '* ', '+ ')) or
                (stripped and stripped[0].isdigit() and '. ' in stripped[:10])
            )
            
            # Check if previous line was a list item
            prev_line = lines[i-1].strip() if i > 0 else ""
            prev_was_list = (
                prev_line.startswith(('- ', '* ', '+ ')) or
                (prev_line and prev_line[0].isdigit() and '. ' in prev_line[:10])
            )
            
            # If we're starting a new list or continuing a list, ensure proper spacing
            if is_list_item:
                if not in_list and result and result[-1].strip():
                    # Starting a new list - add blank line before
                    result.append('')
                in_list = True
            elif in_list and stripped and not is_list_item:
                # Ending a list - add blank line after the list
                if result and result[-1].strip():
                    result.append('')
                in_list = False
            
            result.append(line)
        
        return '\n'.join(result)
    
    # Fix list spacing
    processed_text = fix_list_spacing(text)
    
    # Convert markdown to HTML with extensions for tables, code highlighting, etc.
    html = markdown.markdown(processed_text, extensions=[
        'fenced_code',      # Fenced code blocks
        'tables',           # Table support
        'attr_list',        # Attribute lists
        'def_list',         # Definition lists
        'footnotes',        # Footnotes
        'abbr',             # Abbreviations
        'codehilite',       # Syntax highlighting for code blocks
        'smarty'            # Smart quotes, dashes, etc.
    ])
    
    # Apply sanitization to the generated HTML
    return sanitize_html(html)

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

app = Flask(__name__)
# Use environment variables or default paths for Docker compatibility
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:////data/instance/transcriptions.db')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/data/uploads')
# MAX_CONTENT_LENGTH will be set dynamically after database initialization
# Set a secret key for session management and CSRF protection
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-dev-key-change-in-production')

# Apply ProxyFix to handle headers from a reverse proxy (like Nginx or Caddy)
# This is crucial for request.is_secure to work correctly behind an SSL-terminating proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# --- Secure Session Cookie Configuration ---
# For local network usage, disable secure cookies to allow HTTP connections
# Only enable secure cookies in production when HTTPS is actually being used
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP for local network usage
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Still protect against XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF protection

db = SQLAlchemy()
db.init_app(app)

# Initialize Flask-Login and other extensions
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'
bcrypt.init_app(app)
limiter.init_app(app)  # Initialize the limiter
csrf = CSRFProtect(app)

# Add context processor to make 'now' available to all templates
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

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

# --- Database Models ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    recordings = db.relationship('Recording', backref='owner', lazy=True)
    transcription_language = db.Column(db.String(10), nullable=True) # For ISO 639-1 codes
    output_language = db.Column(db.String(50), nullable=True) # For full language names like "Spanish"
    summary_prompt = db.Column(db.Text, nullable=True)
    name = db.Column(db.String(100), nullable=True)
    job_title = db.Column(db.String(100), nullable=True)
    company = db.Column(db.String(100), nullable=True)
    diarize = db.Column(db.Boolean, default=False)
    
    def __repr__(self):
        return f"User('{self.username}', '{self.email}')"
class Speaker(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, default=datetime.utcnow)
    use_count = db.Column(db.Integer, default=1)
    
    # Relationship to user
    user = db.relationship('User', backref=db.backref('speakers', lazy=True, cascade='all, delete-orphan'))
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'created_at': self.created_at,
            'last_used': self.last_used,
            'use_count': self.use_count
        }

class SystemSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    setting_type = db.Column(db.String(50), nullable=False, default='string')  # string, integer, boolean, float
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'setting_type': self.setting_type,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }
    
    @staticmethod
    def get_setting(key, default_value=None):
        """Get a system setting value by key, with optional default."""
        setting = SystemSetting.query.filter_by(key=key).first()
        if setting:
            # Convert value based on type
            if setting.setting_type == 'integer':
                try:
                    return int(setting.value) if setting.value is not None else default_value
                except (ValueError, TypeError):
                    return default_value
            elif setting.setting_type == 'boolean':
                return setting.value.lower() in ('true', '1', 'yes') if setting.value else default_value
            elif setting.setting_type == 'float':
                try:
                    return float(setting.value) if setting.value is not None else default_value
                except (ValueError, TypeError):
                    return default_value
            else:  # string
                return setting.value if setting.value is not None else default_value
        return default_value
    
    @staticmethod
    def set_setting(key, value, description=None, setting_type='string'):
        """Set a system setting value."""
        setting = SystemSetting.query.filter_by(key=key).first()
        if setting:
            setting.value = str(value) if value is not None else None
            setting.updated_at = datetime.utcnow()
            if description:
                setting.description = description
            if setting_type:
                setting.setting_type = setting_type
        else:
            setting = SystemSetting(
                key=key,
                value=str(value) if value is not None else None,
                description=description,
                setting_type=setting_type
            )
            db.session.add(setting)
        db.session.commit()
        return setting

class Share(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(16))
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    share_summary = db.Column(db.Boolean, default=True)
    share_notes = db.Column(db.Boolean, default=True)
    
    user = db.relationship('User', backref=db.backref('shares', lazy=True, cascade='all, delete-orphan'))
    recording = db.relationship('Recording', backref=db.backref('shares', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'public_id': self.public_id,
            'recording_id': self.recording_id,
            'created_at': local_datetime_filter(self.created_at),
            'share_summary': self.share_summary,
            'share_notes': self.share_notes,
            'recording_title': self.recording.title if self.recording else "N/A"
        }

class Recording(db.Model):
    # Add user_id foreign key to associate recordings with users
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    id = db.Column(db.Integer, primary_key=True)
    # Title will now often be AI-generated, maybe start with filename?
    title = db.Column(db.String(200), nullable=True) # Allow Null initially
    participants = db.Column(db.String(500))
    notes = db.Column(db.Text)
    transcription = db.Column(db.Text, nullable=True)
    summary = db.Column(db.Text, nullable=True) # <-- ADDED: Summary field
    status = db.Column(db.String(50), default='PENDING') # PENDING, PROCESSING, SUMMARIZING, COMPLETED, FAILED
    audio_path = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    meeting_date = db.Column(db.Date, nullable=True) # <-- ADDED: Meeting Date field
    file_size = db.Column(db.Integer)  # Store file size in bytes
    original_filename = db.Column(db.String(500), nullable=True) # Store the original uploaded filename
    is_inbox = db.Column(db.Boolean, default=True)  # New recordings are marked as inbox by default
    is_highlighted = db.Column(db.Boolean, default=False)  # Recordings can be highlighted by the user
    mime_type = db.Column(db.String(100), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    processing_time_seconds = db.Column(db.Integer, nullable=True)
    processing_source = db.Column(db.String(50), default='upload')  # upload, auto_process, recording
    error_message = db.Column(db.Text, nullable=True)  # Store detailed error messages

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'participants': self.participants,
            'notes': self.notes,
            'notes_html': md_to_html(self.notes) if self.notes else "",
            'transcription': self.transcription,
            'summary': self.summary,
            'summary_html': md_to_html(self.summary) if self.summary else "",
            'status': self.status,
            'created_at': local_datetime_filter(self.created_at),
            'completed_at': local_datetime_filter(self.completed_at),
            'processing_time_seconds': self.processing_time_seconds,
            'meeting_date': self.meeting_date.isoformat() if self.meeting_date else None, # <-- ADDED: Include meeting_date
            'file_size': self.file_size,
            'original_filename': self.original_filename, # <-- ADDED: Include original filename
            'user_id': self.user_id,
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'mime_type': self.mime_type
        }

# --- Forms for Authentication ---
# --- Custom Password Validator ---
def password_check(form, field):
    password = field.data
    if len(password) < 8:
        raise ValidationError('Password must be at least 8 characters long.')
    if not re.search(r'[A-Z]', password):
        raise ValidationError('Password must contain at least one uppercase letter.')
    if not re.search(r'[a-z]', password):
        raise ValidationError('Password must contain at least one lowercase letter.')
    if not re.search(r'[0-9]', password):
        raise ValidationError('Password must contain at least one number.')
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        raise ValidationError('Password must contain at least one special character.')

# --- Share Routes ---
@app.route('/share/<string:public_id>', methods=['GET'])
def view_shared_recording(public_id):
    share = Share.query.filter_by(public_id=public_id).first_or_404()
    recording = share.recording
    
    # Create a limited dictionary for the public view
    recording_data = {
        'id': recording.id,
        'public_id': share.public_id,
        'title': recording.title,
        'participants': recording.participants,
        'transcription': recording.transcription,
        'summary': md_to_html(recording.summary) if share.share_summary else None,
        'notes': md_to_html(recording.notes) if share.share_notes else None,
        'meeting_date': recording.meeting_date.isoformat() if recording.meeting_date else None,
        'mime_type': recording.mime_type
    }
    
    return render_template('share.html', recording=recording_data)

@app.route('/api/recording/<int:recording_id>/share', methods=['POST'])
@login_required
def create_share(recording_id):
    if not request.is_secure:
        return jsonify({'error': 'Sharing is only available over a secure (HTTPS) connection.'}), 403
        
    recording = db.session.get(Recording, recording_id)
    if not recording or recording.user_id != current_user.id:
        return jsonify({'error': 'Recording not found or you do not have permission to share it.'}), 404
        
    data = request.json
    share_summary = data.get('share_summary', True)
    share_notes = data.get('share_notes', True)
    
    share = Share(
        recording_id=recording.id,
        user_id=current_user.id,
        share_summary=share_summary,
        share_notes=share_notes
    )
    db.session.add(share)
    db.session.commit()
    
    share_url = url_for('view_shared_recording', public_id=share.public_id, _external=True)
    
    return jsonify({'success': True, 'share_url': share_url, 'share': share.to_dict()}), 201

@app.route('/api/shares', methods=['GET'])
@login_required
def get_shares():
    shares = Share.query.filter_by(user_id=current_user.id).order_by(Share.created_at.desc()).all()
    return jsonify([share.to_dict() for share in shares])

@app.route('/api/share/<int:share_id>', methods=['PUT'])
@login_required
def update_share(share_id):
    share = Share.query.filter_by(id=share_id, user_id=current_user.id).first_or_404()
    data = request.json
    
    if 'share_summary' in data:
        share.share_summary = data['share_summary']
    if 'share_notes' in data:
        share.share_notes = data['share_notes']
        
    db.session.commit()
    return jsonify({'success': True, 'share': share.to_dict()})

@app.route('/api/share/<int:share_id>', methods=['DELETE'])
@login_required
def delete_share(share_id):
    share = Share.query.filter_by(id=share_id, user_id=current_user.id).first_or_404()
    db.session.delete(share)
    db.session.commit()
    return jsonify({'success': True})

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), password_check])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Sign Up')
    
    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError('That username is already taken. Please choose a different one.')
    
    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError('That email is already registered. Please use a different one.')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')

# Function to check and add columns if they don't exist
def add_column_if_not_exists(engine, table_name, column_name, column_type):
    """Add a column to a table if it doesn't exist."""
    from sqlalchemy import text
    
    try:
        # Check if column exists
        with engine.connect() as conn:
            # For SQLite, we can query the pragma table_info using text()
            result = conn.execute(text(f"PRAGMA table_info({table_name})"))
            columns = [row[1] for row in result]
            
            if column_name not in columns:
                app.logger.info(f"Adding column {column_name} to {table_name}")
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
                return True
            return False
    except Exception as e:
        app.logger.error(f"Error checking/adding column {column_name} to {table_name}: {e}")
        return False

with app.app_context():
    db.create_all()
    
    # Check and add new columns if they don't exist
    engine = db.engine
    try:
        # Add is_inbox column with default value of 1 (True)
        if add_column_if_not_exists(engine, 'recording', 'is_inbox', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added is_inbox column to recording table")
        
        # Add is_highlighted column with default value of 0 (False)
        if add_column_if_not_exists(engine, 'recording', 'is_highlighted', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added is_highlighted column to recording table")

        # Add language preference columns to User table
        if add_column_if_not_exists(engine, 'user', 'transcription_language', 'VARCHAR(10)'):
            app.logger.info("Added transcription_language column to user table")
        if add_column_if_not_exists(engine, 'user', 'output_language', 'VARCHAR(50)'):
            app.logger.info("Added output_language column to user table")
        if add_column_if_not_exists(engine, 'user', 'summary_prompt', 'TEXT'):
            app.logger.info("Added summary_prompt column to user table")
        if add_column_if_not_exists(engine, 'user', 'name', 'VARCHAR(100)'):
            app.logger.info("Added name column to user table")
        if add_column_if_not_exists(engine, 'user', 'job_title', 'VARCHAR(100)'):
            app.logger.info("Added job_title column to user table")
        if add_column_if_not_exists(engine, 'user', 'company', 'VARCHAR(100)'):
            app.logger.info("Added company column to user table")
        if add_column_if_not_exists(engine, 'user', 'diarize', 'BOOLEAN'):
            app.logger.info("Added diarize column to user table")
        if add_column_if_not_exists(engine, 'recording', 'mime_type', 'VARCHAR(100)'):
            app.logger.info("Added mime_type column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'completed_at', 'DATETIME'):
            app.logger.info("Added completed_at column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'processing_time_seconds', 'INTEGER'):
            app.logger.info("Added processing_time_seconds column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'processing_source', 'VARCHAR(50) DEFAULT "upload"'):
            app.logger.info("Added processing_source column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'error_message', 'TEXT'):
            app.logger.info("Added error_message column to recording table")
        
        # Initialize default system settings
        if not SystemSetting.query.filter_by(key='transcript_length_limit').first():
            SystemSetting.set_setting(
                key='transcript_length_limit',
                value='30000',
                description='Maximum number of characters to send from transcript to LLM for summarization and chat. Use -1 for no limit.',
                setting_type='integer'
            )
            app.logger.info("Initialized default transcript_length_limit setting")
            
        if not SystemSetting.query.filter_by(key='max_file_size_mb').first():
            SystemSetting.set_setting(
                key='max_file_size_mb',
                value='250',
                description='Maximum file size allowed for audio uploads in megabytes (MB).',
                setting_type='integer'
            )
            app.logger.info("Initialized default max_file_size_mb setting")
            
    except Exception as e:
        app.logger.error(f"Error during database migration: {e}")

# --- API client setup for OpenRouter ---
# Use environment variables from .env
TEXT_MODEL_API_KEY = os.environ.get("TEXT_MODEL_API_KEY")
TEXT_MODEL_BASE_URL = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
TEXT_MODEL_NAME = os.environ.get("TEXT_MODEL_NAME", "openai/gpt-3.5-turbo") # Default if not set

http_client_no_proxy = httpx.Client(verify=True) # verify=True is default, but good to be explicit

if not TEXT_MODEL_API_KEY:
    app.logger.warning("TEXT_MODEL_API_KEY not found. Title/Summary generation DISABLED.")
else:
    try:
        # ---> Pass the custom httpx_client <---
        client = OpenAI(
            api_key=TEXT_MODEL_API_KEY,
            base_url=TEXT_MODEL_BASE_URL,
            http_client=http_client_no_proxy # Pass the proxy-disabled client
        )
        app.logger.info(f"OpenRouter client initialized. Using model: {TEXT_MODEL_NAME}")
    except Exception as client_init_e:
         app.logger.error(f"Failed to initialize OpenRouter client: {client_init_e}", exc_info=True)

# Store details for the transcription client (potentially different)
transcription_api_key = os.environ.get("TRANSCRIPTION_API_KEY", "cant-be-empty")
transcription_base_url = os.environ.get("TRANSCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")

# ASR endpoint configuration
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
ASR_BASE_URL = os.environ.get('ASR_BASE_URL')
ASR_DIARIZE = os.environ.get('ASR_DIARIZE', 'true').lower() == 'true'
ASR_MIN_SPEAKERS = os.environ.get('ASR_MIN_SPEAKERS')
ASR_MAX_SPEAKERS = os.environ.get('ASR_MAX_SPEAKERS')

# Audio chunking configuration for large files with OpenAI Whisper API
ENABLE_CHUNKING = os.environ.get('ENABLE_CHUNKING', 'true').lower() == 'true'
CHUNK_SIZE_MB = int(os.environ.get('CHUNK_SIZE_MB', '20'))  # 20MB default for safety margin
CHUNK_OVERLAP_SECONDS = int(os.environ.get('CHUNK_OVERLAP_SECONDS', '3'))  # 3 seconds overlap

# Initialize chunking service
chunking_service = AudioChunkingService(
    max_chunk_size_mb=CHUNK_SIZE_MB,
    overlap_seconds=CHUNK_OVERLAP_SECONDS
) if ENABLE_CHUNKING else None

app.logger.info(f"Using OpenRouter model for summaries: {TEXT_MODEL_NAME}")
app.logger.info(f"Using Whisper API at: {transcription_base_url}")
if USE_ASR_ENDPOINT:
    app.logger.info(f"ASR endpoint is enabled at: {ASR_BASE_URL}")

# --- Background Transcription & Summarization Task ---
def format_api_error_message(error_str):
    """
    Formats API error messages to be more user-friendly.
    Specifically handles token limit errors with helpful suggestions.
    """
    error_lower = error_str.lower()
    
    # Check for token limit errors
    if 'maximum context length' in error_lower and 'tokens' in error_lower:
        return "[Summary generation failed: The transcription is too long for AI processing. Request your admin to try using a different LLM with a larger context size, or set a limit for the transcript_length_limit in the system settings.]"
    
    # Check for other common API errors
    if 'rate limit' in error_lower:
        return "[Summary generation failed: API rate limit exceeded. Please try again in a few minutes.]"
    
    if 'insufficient funds' in error_lower or 'quota exceeded' in error_lower:
        return "[Summary generation failed: API quota exceeded. Please contact support.]"
    
    if 'timeout' in error_lower:
        return "[Summary generation failed: Request timed out. Please try again.]"
    
    # For other errors, show a generic message
    return f"[Summary generation failed: {error_str}]"

def generate_summary_task(app_context, recording_id, start_time):
    """Generates title and summary for a recording."""
    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            app.logger.error(f"Error: Recording {recording_id} not found for summary generation.")
            return

        if client is None:
            app.logger.warning(f"Skipping summary for {recording_id}: OpenRouter client not configured.")
            recording.summary = "[Summary skipped: OpenRouter client not configured]"
            recording.status = 'COMPLETED'
            db.session.commit()
            return

        recording.status = 'SUMMARIZING'
        db.session.commit()
        app.logger.info(f"Requesting title and summary from OpenRouter for recording {recording_id} using model {TEXT_MODEL_NAME}...")

        if not recording.transcription or len(recording.transcription.strip()) < 10:
            app.logger.warning(f"Transcription for recording {recording_id} is too short or empty. Skipping summarization.")
            recording.status = 'COMPLETED'
            recording.summary = "[Summary skipped due to short transcription]"
            db.session.commit()
            return

        user_summary_prompt = None
        user_output_language = None
        if recording.owner:
            user_summary_prompt = recording.owner.summary_prompt
            user_output_language = recording.owner.output_language

        default_summary_prompt = """Analyze the following audio transcription and generate a concise title and a detailed, well-structured summary in Markdown format.

Transcription:
\"\"\"
{transcription}
\"\"\"

Respond STRICTLY with a JSON object containing two keys: "title" and "summary".

1.  **title**: A short, descriptive title (max 6-8 words). Avoid introductory phrases like "Discussion on" or "Meeting about".
2.  **summary**: A detailed summary in Markdown format. It should include the following sections:
    *   **Key Issues Discussed**: A bulleted list of the main topics.
    *   **Key Decisions Made**: A bulleted list of any decisions reached.
    *   **Action Items**: A bulleted list of tasks assigned, including who is responsible if mentioned.

{language_directive}

Example Format:
{{
  "title": "Q3 Strategy and Project Phoenix",
  "summary": "### Key Issues Discussed\\n- Review of Q2 performance and its impact on Q3 planning.\\n- Feasibility and timeline for Project Phoenix.\\n- Budget constraints and resource allocation for new initiatives.\\n\\n### Key Decisions Made\\n- Project Phoenix is approved to proceed with a revised timeline.\\n- The marketing budget will be reallocated to support the new launch.\\n\\n### Action Items\\n- **Alice**: Finalize the Project Phoenix roadmap by next Friday.\\n- **Bob**: Present the revised budget to the finance committee."
}}

JSON Response:"""

        language_directive = f"Please provide the title and summary in {user_output_language}." if user_output_language else ""
        
        # Get configurable transcript length limit
        transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
        if transcript_limit == -1:
            # No limit
            transcript_text = recording.transcription
        else:
            transcript_text = recording.transcription[:transcript_limit]
        
        if user_summary_prompt:
            prompt_text = f"""Analyze the following audio transcription and generate a concise title and a summary according to the following instructions.
            
Transcription:
\"\"\"
{transcript_text} 
\"\"\"

Generate a response STRICTLY as a JSON object with two keys: "title" and "summary". The summary should be markdown, not JSON. 

For the "title": create a short, descriptive title (max 6 words, no introductory phrases like "brief", "discussion on", "Meeting about").
For the "summary": {user_summary_prompt}. 

{language_directive}

JSON Response:"""
        else:
            prompt_text = default_summary_prompt.format(transcription=transcript_text, language_directive=language_directive)
        
        system_message_content = "You are an AI assistant that generates titles and summaries for meeting transcripts. Respond only with the requested JSON object."
        if user_output_language:
            system_message_content += f" Ensure your response (both title and summary) is in {user_output_language}."

        try:
            completion = client.chat.completions.create(
                model=TEXT_MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_message_content},
                    {"role": "user", "content": prompt_text}
                ],
                temperature=0.5,
                max_tokens=int(os.environ.get("SUMMARY_MAX_TOKENS", "3000")),
                response_format={"type": "json_object"}
            )
            
            response_content = completion.choices[0].message.content
            json_match = re.search(r'```(?:json)?(.*?)```|(.+)', response_content, re.DOTALL)
            sanitized_response = json_match.group(1) if json_match.group(1) else json_match.group(2)
            sanitized_response = sanitized_response.strip()
            summary_data = safe_json_loads(sanitized_response, {})
            
            raw_title = summary_data.get("title")
            raw_summary = summary_data.get("summary")

            recording.title = raw_title.strip() if isinstance(raw_title, str) else "[Title not generated]"
            recording.summary = raw_summary.strip() if isinstance(raw_summary, str) else "[Summary not generated]"
            recording.status = 'COMPLETED'
            recording.completed_at = datetime.utcnow()
            # This is now calculated at the end of the transcription task
            # recording.processing_time_seconds = (recording.completed_at - recording.created_at).total_seconds()
            app.logger.info(f"Title and summary processed successfully for recording {recording_id}.")

        except Exception as summary_e:
            app.logger.error(f"Error calling OpenRouter API for summary ({recording_id}): {str(summary_e)}")
            recording.summary = format_api_error_message(str(summary_e))
            recording.status = 'COMPLETED'
            recording.completed_at = datetime.utcnow()
            # This is now calculated at the end of the transcription task
            # recording.processing_time_seconds = (recording.completed_at - recording.created_at).total_seconds()

        end_time = datetime.utcnow()
        recording.processing_time_seconds = (end_time - start_time).total_seconds()
        app.logger.info(f"Recording {recording_id} processing time: {recording.processing_time_seconds} seconds.")
        db.session.commit()

def transcribe_audio_asr(app_context, recording_id, filepath, original_filename, start_time, mime_type=None, language=None, diarize=False, min_speakers=None, max_speakers=None):
    """Transcribes audio using the ASR webservice."""
    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            app.logger.error(f"Error: Recording {recording_id} not found for ASR transcription.")
            return

        try:
            app.logger.info(f"Starting ASR transcription for recording {recording_id}...")
            recording.status = 'PROCESSING'
            db.session.commit()

            with open(filepath, 'rb') as audio_file:
                url = f"{ASR_BASE_URL}/asr"
                params = {
                    'encode': True,
                    'task': 'transcribe',
                    'output': 'json'
                }
                if language:
                    params['language'] = language
                if diarize:
                    params['diarize'] = diarize
                if min_speakers:
                    params['min_speakers'] = min_speakers
                if max_speakers:
                    params['max_speakers'] = max_speakers

                # Use the stored mime_type, or guess it, with a fallback.
                content_type = mime_type or mimetypes.guess_type(original_filename)[0] or 'application/octet-stream'
                app.logger.info(f"Using MIME type {content_type} for ASR upload.")
                files = {'audio_file': (original_filename, audio_file, content_type)}
                
                with httpx.Client() as client:
                    response = client.post(url, params=params, files=files, timeout=None)
                    response.raise_for_status()
                    
                    # Parse the JSON response from ASR
                    asr_response_data = response.json()
                    
                    # Debug logging for ASR response
                    app.logger.info(f"ASR response keys: {list(asr_response_data.keys())}")
                    
                    # Log the complete raw JSON response (truncated for readability)
                    import json as json_module
                    raw_json_str = json_module.dumps(asr_response_data, indent=2)
                    if len(raw_json_str) > 5000:
                        app.logger.info(f"Raw ASR response (first 5000 chars): {raw_json_str[:5000]}...")
                    else:
                        app.logger.info(f"Raw ASR response: {raw_json_str}")
                    
                    if 'segments' in asr_response_data:
                        app.logger.info(f"Number of segments: {len(asr_response_data['segments'])}")
                        
                        # Collect all unique speakers from the response
                        all_speakers = set()
                        segments_with_speakers = 0
                        segments_without_speakers = 0
                        
                        for segment in asr_response_data['segments']:
                            if 'speaker' in segment and segment['speaker'] is not None:
                                all_speakers.add(segment['speaker'])
                                segments_with_speakers += 1
                            else:
                                segments_without_speakers += 1
                        
                        app.logger.info(f"Unique speakers found in raw response: {sorted(list(all_speakers))}")
                        app.logger.info(f"Segments with speakers: {segments_with_speakers}, without speakers: {segments_without_speakers}")
                        
                        # Log first few segments for debugging
                        for i, segment in enumerate(asr_response_data['segments'][:5]):
                            segment_keys = list(segment.keys())
                            app.logger.info(f"Segment {i} keys: {segment_keys}")
                            app.logger.info(f"Segment {i}: speaker='{segment.get('speaker')}', text='{segment.get('text', '')[:50]}...'")
                    
                    # Simplify the JSON data
                    simplified_segments = []
                    if 'segments' in asr_response_data and isinstance(asr_response_data['segments'], list):
                        last_known_speaker = None
                        
                        for i, segment in enumerate(asr_response_data['segments']):
                            speaker = segment.get('speaker')
                            text = segment.get('text', '').strip()
                            
                            # If segment doesn't have a speaker, use the previous segment's speaker
                            if speaker is None:
                                if last_known_speaker is not None:
                                    speaker = last_known_speaker
                                    app.logger.info(f"Assigned speaker '{speaker}' to segment {i} from previous segment")
                                else:
                                    speaker = 'UNKNOWN_SPEAKER'
                                    app.logger.warning(f"No previous speaker available for segment {i}, using UNKNOWN_SPEAKER")
                            else:
                                # Update the last known speaker when we have a valid one
                                last_known_speaker = speaker
                            
                            simplified_segments.append({
                                'speaker': speaker,
                                'sentence': text,
                                'start_time': segment.get('start'),
                                'end_time': segment.get('end')
                            })
                    
                    # Log final simplified segments count
                    app.logger.info(f"Created {len(simplified_segments)} simplified segments")
                    null_speaker_count = sum(1 for seg in simplified_segments if seg['speaker'] is None)
                    if null_speaker_count > 0:
                        app.logger.warning(f"Found {null_speaker_count} segments with null speakers in final output")
                    
                    # Store the simplified JSON as a string
                    recording.transcription = json.dumps(simplified_segments)
            
            app.logger.info(f"ASR transcription completed for recording {recording_id}.")
            generate_summary_task(app_context, recording_id, start_time)

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"ASR processing FAILED for recording {recording_id}: {str(e)}", exc_info=True)
            recording = db.session.get(Recording, recording_id)
            if recording:
                recording.status = 'FAILED'
                recording.transcription = f"ASR processing failed: {str(e)}"
                db.session.commit()

def transcribe_audio_task(app_context, recording_id, filepath, filename_for_asr, start_time):
    """Runs the transcription and summarization in a background thread."""
    if USE_ASR_ENDPOINT:
        with app_context:
            recording = db.session.get(Recording, recording_id)
            # Environment variable ASR_DIARIZE overrides user setting
            if 'ASR_DIARIZE' in os.environ:
                diarize_setting = ASR_DIARIZE
            else:
                diarize_setting = recording.owner.diarize if recording.owner else False
            user_transcription_language = recording.owner.transcription_language if recording.owner else None
        transcribe_audio_asr(app_context, recording_id, filepath, filename_for_asr, start_time, mime_type=recording.mime_type, language=user_transcription_language, diarize=diarize_setting)
        
        # After ASR task completes, calculate processing time
        with app_context:
            recording = db.session.get(Recording, recording_id)
            if recording.status in ['COMPLETED', 'FAILED']:
                end_time = datetime.utcnow()
                recording.processing_time_seconds = (end_time - start_time).total_seconds()
                db.session.commit()
        return

    with app_context: # Need app context for db operations in thread
        recording = db.session.get(Recording, recording_id)
        if not recording:
            app.logger.error(f"Error: Recording {recording_id} not found for transcription.")
            return

        try:
            app.logger.info(f"Starting transcription for recording {recording_id} ({filename_for_asr})...")
            recording.status = 'PROCESSING'
            db.session.commit()

            # Check if chunking is needed for large files
            needs_chunking = (chunking_service and 
                            ENABLE_CHUNKING and 
                            chunking_service.needs_chunking(filepath, USE_ASR_ENDPOINT))
            
            if needs_chunking:
                app.logger.info(f"File {filepath} is large ({os.path.getsize(filepath)/1024/1024:.1f}MB), using chunking for transcription")
                transcription_text = transcribe_with_chunking(app_context, recording_id, filepath, filename_for_asr)
            else:
                # --- Standard transcription for smaller files ---
                transcription_text = transcribe_single_file(filepath, recording)
            
            recording.transcription = transcription_text
            app.logger.info(f"Transcription completed for recording {recording_id}. Text length: {len(recording.transcription)}")
            generate_summary_task(app_context, recording_id, start_time)

        except Exception as e:
            db.session.rollback() # Rollback if any step failed critically
            app.logger.error(f"Processing FAILED for recording {recording_id}: {str(e)}", exc_info=True)
            # Retrieve recording again in case session was rolled back
            recording = db.session.get(Recording, recording_id)
            if recording:
                 # Ensure status reflects failure even after rollback/retrieve attempt
                if recording.status not in ['COMPLETED', 'FAILED']: # Avoid overwriting final state
                    recording.status = 'FAILED'
                if not recording.transcription: # If transcription itself failed
                     recording.transcription = f"Processing failed: {str(e)}"
                # Add error note to summary if appropriate stage was reached
                if recording.status == 'SUMMARIZING' and not recording.summary:
                     recording.summary = f"[Processing failed during summarization: {str(e)}]"
                
                end_time = datetime.utcnow()
                recording.processing_time_seconds = (end_time - start_time).total_seconds()
                db.session.commit()

def transcribe_single_file(filepath, recording):
    """Transcribe a single audio file using OpenAI Whisper API."""
    with open(filepath, 'rb') as audio_file:
        transcription_client = OpenAI(
            api_key=transcription_api_key,
            base_url=transcription_base_url,
            http_client=http_client_no_proxy
        )
        whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")
        
        user_transcription_language = None
        if recording and recording.owner:
            user_transcription_language = recording.owner.transcription_language
        
        transcription_language = user_transcription_language

        transcription_params = {
            "model": whisper_model,
            "file": audio_file
        }

        if transcription_language:
            transcription_params["language"] = transcription_language
            app.logger.info(f"Using transcription language: {transcription_language}")
        else:
            app.logger.info("Transcription language not set, using auto-detection or service default.")

        transcript = transcription_client.audio.transcriptions.create(**transcription_params)
        return transcript.text

def transcribe_with_chunking(app_context, recording_id, filepath, filename_for_asr):
    """Transcribe a large audio file using chunking."""
    import tempfile
    
    with app_context:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")
    
    # Create temporary directory for chunks
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Create chunks
            app.logger.info(f"Creating chunks for large file: {filepath}")
            chunks = chunking_service.create_chunks(filepath, temp_dir)
            
            if not chunks:
                raise ChunkProcessingError("No chunks were created from the audio file")
            
            app.logger.info(f"Created {len(chunks)} chunks, processing each with Whisper API...")
            
            # Process each chunk with proper timeout and retry handling
            chunk_results = []
            
            # Create HTTP client with proper timeouts
            timeout_config = httpx.Timeout(
                connect=30.0,    # 30 seconds to establish connection
                read=300.0,      # 5 minutes to read response (for large audio files)
                write=60.0,      # 1 minute to write request
                pool=10.0        # 10 seconds to get connection from pool
            )
            
            http_client_with_timeout = httpx.Client(
                verify=True,
                timeout=timeout_config,
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2)
            )
            
            transcription_client = OpenAI(
                api_key=transcription_api_key,
                base_url=transcription_base_url,
                http_client=http_client_with_timeout,
                max_retries=2,  # Limit retries to avoid excessive delays
                timeout=300.0   # 5 minute timeout for API calls
            )
            whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")
            
            # Get user language preference
            user_transcription_language = None
            with app_context:
                recording = db.session.get(Recording, recording_id)
                if recording and recording.owner:
                    user_transcription_language = recording.owner.transcription_language
            
            for i, chunk in enumerate(chunks):
                max_chunk_retries = 3
                chunk_retry_count = 0
                chunk_success = False
                
                while chunk_retry_count < max_chunk_retries and not chunk_success:
                    try:
                        retry_suffix = f" (retry {chunk_retry_count + 1}/{max_chunk_retries})" if chunk_retry_count > 0 else ""
                        app.logger.info(f"Processing chunk {i+1}/{len(chunks)}: {chunk['filename']} ({chunk['size_mb']:.1f}MB){retry_suffix}")
                        
                        # Log detailed timing for each step
                        step_start_time = time.time()
                        
                        # Step 1: File opening
                        file_open_start = time.time()
                        with open(chunk['path'], 'rb') as chunk_file:
                            file_open_time = time.time() - file_open_start
                            app.logger.info(f"Chunk {i+1}: File opened in {file_open_time:.2f}s")
                            
                            # Step 2: Prepare transcription parameters
                            param_start = time.time()
                            transcription_params = {
                                "model": whisper_model,
                                "file": chunk_file
                            }
                            
                            if user_transcription_language:
                                transcription_params["language"] = user_transcription_language
                            
                            param_time = time.time() - param_start
                            app.logger.info(f"Chunk {i+1}: Parameters prepared in {param_time:.2f}s")
                            
                            # Step 3: API call with detailed timing
                            api_start = time.time()
                            app.logger.info(f"Chunk {i+1}: Starting API call to {transcription_base_url}")
                            
                            # Log connection details
                            app.logger.info(f"Chunk {i+1}: Using timeout config - connect: 30s, read: 300s, write: 60s")
                            app.logger.info(f"Chunk {i+1}: Max retries: 2, API timeout: 300s")
                            
                            transcript = transcription_client.audio.transcriptions.create(**transcription_params)
                            
                            api_time = time.time() - api_start
                            app.logger.info(f"Chunk {i+1}: API call completed in {api_time:.2f}s")
                            
                            # Step 4: Process response
                            response_start = time.time()
                            chunk_result = {
                                'index': chunk['index'],
                                'start_time': chunk['start_time'],
                                'end_time': chunk['end_time'],
                                'duration': chunk['duration'],
                                'size_mb': chunk['size_mb'],
                                'transcription': transcript.text,
                                'filename': chunk['filename'],
                                'processing_time': api_time  # Store the actual API processing time
                            }
                            chunk_results.append(chunk_result)
                            response_time = time.time() - response_start
                            
                            total_time = time.time() - step_start_time
                            app.logger.info(f"Chunk {i+1}: Response processed in {response_time:.2f}s")
                            app.logger.info(f"Chunk {i+1}: Total processing time: {total_time:.2f}s")
                            app.logger.info(f"Chunk {i+1} transcribed successfully: {len(transcript.text)} characters")
                            chunk_success = True
                            
                    except Exception as chunk_error:
                        chunk_retry_count += 1
                        error_msg = str(chunk_error)
                        
                        if chunk_retry_count < max_chunk_retries:
                            # Determine wait time based on error type
                            if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                                wait_time = 30  # 30 seconds for timeout errors
                            elif "rate limit" in error_msg.lower():
                                wait_time = 60  # 1 minute for rate limit errors
                            else:
                                wait_time = 15  # 15 seconds for other errors
                            
                            app.logger.warning(f"Chunk {i+1} failed (attempt {chunk_retry_count}/{max_chunk_retries}): {chunk_error}. Retrying in {wait_time} seconds...")
                            time.sleep(wait_time)
                        else:
                            app.logger.error(f"Chunk {i+1} failed after {max_chunk_retries} attempts: {chunk_error}")
                            # Add failed chunk to results
                            chunk_result = {
                                'index': chunk['index'],
                                'start_time': chunk['start_time'],
                                'end_time': chunk['end_time'],
                                'transcription': f"[Chunk {i+1} transcription failed after {max_chunk_retries} attempts: {str(chunk_error)}]",
                                'filename': chunk['filename']
                            }
                            chunk_results.append(chunk_result)
                
                # Add small delay between chunks to avoid overwhelming the API
                if i < len(chunks) - 1:  # Don't delay after the last chunk
                    time.sleep(2)
            
            # Merge transcriptions
            app.logger.info(f"Merging {len(chunk_results)} chunk transcriptions...")
            merged_transcription = chunking_service.merge_transcriptions(chunk_results)
            
            if not merged_transcription.strip():
                raise ChunkProcessingError("Merged transcription is empty")
            
            # Log detailed performance statistics and analysis
            chunking_service.log_processing_statistics(chunk_results)
            
            # Get performance recommendations
            recommendations = chunking_service.get_performance_recommendations(chunk_results)
            if recommendations:
                app.logger.info("=== PERFORMANCE RECOMMENDATIONS ===")
                for i, rec in enumerate(recommendations, 1):
                    app.logger.info(f"{i}. {rec}")
                app.logger.info("=== END RECOMMENDATIONS ===")
            
            app.logger.info(f"Chunked transcription completed. Final length: {len(merged_transcription)} characters")
            return merged_transcription
            
        except Exception as e:
            app.logger.error(f"Chunking transcription failed for {filepath}: {e}")
            # Clean up chunks if they exist
            if 'chunks' in locals():
                chunking_service.cleanup_chunks(chunks)
            raise ChunkProcessingError(f"Chunked transcription failed: {str(e)}")
        finally:
            # Cleanup is handled by tempfile.TemporaryDirectory context manager
            pass

@app.route('/speakers', methods=['GET'])
@login_required
def get_speakers():
    """Get all speakers for the current user, ordered by usage frequency and recency."""
    try:
        speakers = Speaker.query.filter_by(user_id=current_user.id)\
                               .order_by(Speaker.use_count.desc(), Speaker.last_used.desc())\
                               .all()
        return jsonify([speaker.to_dict() for speaker in speakers])
    except Exception as e:
        app.logger.error(f"Error fetching speakers: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/speakers/search', methods=['GET'])
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
        app.logger.error(f"Error searching speakers: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/speakers', methods=['POST'])
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
        app.logger.error(f"Error creating speaker: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/speakers/<int:speaker_id>', methods=['DELETE'])
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
        app.logger.error(f"Error deleting speaker: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/speakers/delete_all', methods=['DELETE'])
@login_required
def delete_all_speakers():
    """Delete all speakers for the current user."""
    try:
        deleted_count = Speaker.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        return jsonify({'success': True, 'deleted_count': deleted_count})
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting all speakers: {e}")
        return jsonify({'error': str(e)}), 500

def update_speaker_usage(speaker_names):
    """Helper function to update speaker usage statistics."""
    if not speaker_names or not current_user.is_authenticated:
        return
    
    try:
        for name in speaker_names:
            name = name.strip()
            if not name:
                continue
                
            speaker = Speaker.query.filter_by(user_id=current_user.id, name=name).first()
            if speaker:
                speaker.use_count += 1
                speaker.last_used = datetime.utcnow()
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
    except Exception as e:
        app.logger.error(f"Error updating speaker usage: {e}")
        db.session.rollback()

@app.route('/recording/<int:recording_id>/update_speakers', methods=['POST'])
@login_required
def update_speakers(recording_id):
    """Updates speaker labels in a transcription with provided names."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to edit this recording'}), 403

        data = request.json
        speaker_map = data.get('speaker_map')
        regenerate_summary = data.get('regenerate_summary', False)

        if not speaker_map:
            return jsonify({'error': 'No speaker map provided'}), 400

        transcription_text = recording.transcription
        is_json = False
        try:
            transcription_data = json.loads(transcription_text)
            # Updated check for our new simplified JSON format (a list of segment objects)
            is_json = isinstance(transcription_data, list)
        except (json.JSONDecodeError, TypeError):
            is_json = False

        speaker_names_used = []

        if is_json:
            # Handle new simplified JSON transcript (list of segments)
            for segment in transcription_data:
                original_speaker_label = segment.get('speaker')
                if original_speaker_label in speaker_map:
                    new_name_info = speaker_map[original_speaker_label]
                    new_name = new_name_info.get('name', '').strip()
                    if new_name_info.get('isMe'):
                        new_name = current_user.name or 'Me'
                    
                    if new_name:
                        segment['speaker'] = new_name
                        if new_name not in speaker_names_used:
                            speaker_names_used.append(new_name)
            
            recording.transcription = json.dumps(transcription_data)
            
            # Update participants only from speakers that were actually given names (not default labels)
            final_speakers = set()
            for seg in transcription_data:
                speaker = seg.get('speaker')
                if speaker and str(speaker).strip():
                    # Only include speakers that have been given actual names (not default labels like "SPEAKER_01", "SPEAKER_09", etc.)
                    # Check if this speaker was updated with a real name (not a default label)
                    if not re.match(r'^SPEAKER_\d+$', str(speaker), re.IGNORECASE):
                        final_speakers.add(speaker)
            recording.participants = ', '.join(sorted(list(final_speakers)))

        else:
            # Handle plain text transcript
            new_participants = []
            for speaker_label, new_name_info in speaker_map.items():
                new_name = new_name_info.get('name', '').strip()
                if new_name_info.get('isMe'):
                    new_name = current_user.name or 'Me'
                
                if new_name:
                    transcription_text = re.sub(r'\[\s*' + re.escape(speaker_label) + r'\s*\]', f'[{new_name}]', transcription_text, flags=re.IGNORECASE)
                    if new_name not in new_participants:
                        new_participants.append(new_name)
            
            recording.transcription = transcription_text
            if new_participants:
                recording.participants = ', '.join(new_participants)
            speaker_names_used = new_participants

        # Update speaker usage statistics
        if speaker_names_used:
            update_speaker_usage(speaker_names_used)
        
        db.session.commit()

        if regenerate_summary:
            app.logger.info(f"Regenerating summary for recording {recording_id} after speaker update.")
            start_time = datetime.utcnow()
            thread = threading.Thread(
                target=generate_summary_task,
                args=(app.app_context(), recording.id, start_time)
            )
            thread.start()
        
        return jsonify({
            'success': True, 
            'message': 'Speakers updated successfully.',
            'recording': recording.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error updating speakers for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

def identify_speakers_from_text(transcription):
    """
    Uses an LLM to identify speakers from a transcription.
    """
    if not TEXT_MODEL_API_KEY:
        raise ValueError("TEXT_MODEL_API_KEY not configured.")

    # The transcription passed here could be JSON, so we format it.
    formatted_transcription = format_transcription_for_llm(transcription)

    # Extract existing speaker labels (e.g., SPEAKER_00, SPEAKER_01) in order of appearance
    all_labels = re.findall(r'\[(SPEAKER_\d+)\]', formatted_transcription)
    seen = set()
    speaker_labels = [x for x in all_labels if not (x in seen or seen.add(x))]
    
    if not speaker_labels:
        return {}

    # Get configurable transcript length limit
    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if transcript_limit == -1:
        # No limit
        transcript_text = formatted_transcription
    else:
        transcript_text = formatted_transcription[:transcript_limit]

    prompt = f"""Analyze the following transcription and identify the names of the speakers. The speakers are labeled as {', '.join(speaker_labels)}. Based on the context of the conversation, determine the most likely name for each speaker label.

Transcription:
---
{transcript_text}
---

Respond with a single JSON object where keys are the speaker labels (e.g., "SPEAKER_00") and values are the identified full names. If a name cannot be determined, use the value "Unknown".

Example:
{{
  "SPEAKER_00": "John Doe",
  "SPEAKER_01": "Jane Smith",
  "SPEAKER_02": "Unknown"
}}

JSON Response:
"""

    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are an expert in analyzing conversation transcripts to identify speakers. Your response must be a single, valid JSON object."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        response_content = completion.choices[0].message.content
        speaker_map = safe_json_loads(response_content, {})

        # Post-process the map to replace "Unknown" with an empty string
        for speaker_label, identified_name in speaker_map.items():
            if identified_name.strip().lower() == "unknown":
                speaker_map[speaker_label] = ""
                
        return speaker_map
    except Exception as e:
        app.logger.error(f"Error calling LLM for speaker identification: {e}")
        raise

def identify_unidentified_speakers_from_text(transcription, unidentified_speakers):
    """
    Uses an LLM to identify only the unidentified speakers from a transcription.
    """
    if not TEXT_MODEL_API_KEY:
        raise ValueError("TEXT_MODEL_API_KEY not configured.")

    # The transcription passed here could be JSON, so we format it.
    formatted_transcription = format_transcription_for_llm(transcription)

    if not unidentified_speakers:
        return {}

    # Get configurable transcript length limit
    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if transcript_limit == -1:
        # No limit
        transcript_text = formatted_transcription
    else:
        transcript_text = formatted_transcription[:transcript_limit]

    prompt = f"""Analyze the following conversation transcript and identify the names of the UNIDENTIFIED speakers based on the context and content of their dialogue. 

The speakers that need to be identified are: {', '.join(unidentified_speakers)}

Look for clues in the conversation such as:
- Names mentioned by other speakers when addressing someone
- Self-introductions or references to their own name
- Context clues about roles, relationships, or positions
- Any direct mentions of names in the dialogue

Here is the complete conversation transcript:

{transcript_text}

Based on the conversation above, identify the most likely real names for the unidentified speakers. Pay close attention to how speakers address each other and any names that are mentioned in the dialogue.

Respond with a single JSON object where keys are the speaker labels (e.g., "SPEAKER_01") and values are the identified full names. If a name cannot be determined from the conversation context, use an empty string "".

Example format:
{{
  "SPEAKER_01": "Jane Smith",
  "SPEAKER_03": "Bob Johnson",
  "SPEAKER_05": ""
}}

JSON Response:
"""

    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are an expert in analyzing conversation transcripts to identify speakers based on contextual clues in the dialogue. Analyze the conversation carefully to find names mentioned when speakers address each other or introduce themselves. Your response must be a single, valid JSON object containing only the requested speaker identifications."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        response_content = completion.choices[0].message.content
        speaker_map = safe_json_loads(response_content, {})

        # Post-process the map to replace "Unknown" with an empty string
        for speaker_label, identified_name in speaker_map.items():
            if identified_name and identified_name.strip().lower() in ["unknown", "n/a", "not available", "unclear"]:
                speaker_map[speaker_label] = ""
                
        return speaker_map
    except Exception as e:
        app.logger.error(f"Error calling LLM for speaker identification: {e}")
        raise

@app.route('/recording/<int:recording_id>/auto_identify_speakers', methods=['POST'])
@login_required
def auto_identify_speakers(recording_id):
    """
    Automatically identifies speakers in a transcription using an LLM.
    Only identifies speakers that haven't been identified yet.
    """
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403

        if not recording.transcription:
            return jsonify({'error': 'No transcription available for speaker identification'}), 400

        # Get the current speaker map from the request
        data = request.json or {}
        current_speaker_map = data.get('current_speaker_map', {})
        
        # Extract all speaker labels from transcription
        formatted_transcription = format_transcription_for_llm(recording.transcription)
        all_labels = re.findall(r'\[(SPEAKER_\d+)\]', formatted_transcription)
        seen = set()
        speaker_labels = [x for x in all_labels if not (x in seen or seen.add(x))]
        
        # Filter out speakers that already have names assigned
        unidentified_speakers = []
        for speaker_label in speaker_labels:
            speaker_info = current_speaker_map.get(speaker_label, {})
            speaker_name = speaker_info.get('name', '').strip() if isinstance(speaker_info, dict) else str(speaker_info).strip()
            
            # Only include speakers that don't have names or have empty names
            if not speaker_name:
                unidentified_speakers.append(speaker_label)
        
        if not unidentified_speakers:
            return jsonify({'success': True, 'speaker_map': {}, 'message': 'All speakers are already identified'})

        # Call the helper function with only unidentified speakers
        speaker_map = identify_unidentified_speakers_from_text(recording.transcription, unidentified_speakers)

        return jsonify({'success': True, 'speaker_map': speaker_map})

    except ValueError as ve:
        # Handle cases where API key is not set
        return jsonify({'error': str(ve)}), 503
    except Exception as e:
        app.logger.error(f"Error during auto speaker identification for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': f'An unexpected error occurred: {e}'}), 500

# --- Chat with Transcription ---
@app.route('/chat', methods=['POST'])
@login_required
def chat_with_transcription():
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        recording_id = data.get('recording_id')
        user_message = data.get('message')
        message_history = data.get('message_history', [])
        
        if not recording_id:
            return jsonify({'error': 'No recording ID provided'}), 400
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
            
        # Get the recording
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404
            
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to chat with this recording'}), 403
            
        # Check if OpenRouter client is available
        if client is None:
            return jsonify({'error': 'Chat service is not available (OpenRouter client not configured)'}), 503
            
        # Prepare the system prompt with the transcription
        user_chat_output_language = current_user.output_language if current_user.is_authenticated else None
        
        language_instruction = ""
        if user_chat_output_language:
            language_instruction = f"Please provide all your responses in {user_chat_output_language}."
        
        user_name = current_user.name if current_user.is_authenticated and current_user.name else "User"
        user_title = current_user.job_title if current_user.is_authenticated and current_user.job_title else "a professional"
        user_company = current_user.company if current_user.is_authenticated and current_user.company else "their organization"

        formatted_transcription = format_transcription_for_llm(recording.transcription)
        
        # Get configurable transcript length limit for chat
        transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
        if transcript_limit == -1:
            # No limit
            chat_transcript = formatted_transcription
        else:
            chat_transcript = formatted_transcription[:transcript_limit]
        
        system_prompt = f"""You are a professional meeting and audio transcription analyst assisting {user_name}, who is a(n) {user_title} at {user_company}. {language_instruction} Analyze the following meeting information and respond to the specific request.

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

        def generate():
            try:
                # Enable streaming
                stream = client.chat.completions.create(
                    model=TEXT_MODEL_NAME,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=int(os.environ.get("CHAT_MAX_TOKENS", "2000")),
                    stream=True
                )
                
                for chunk in stream:
                    content = chunk.choices[0].delta.content
                    if content:
                        # Yield the content in SSE format
                        yield f"data: {json.dumps({'delta': content})}\n\n"
                
                # Signal the end of the stream
                yield f"data: {json.dumps({'end_of_stream': True})}\n\n"

            except Exception as e:
                app.logger.error(f"Error during chat stream generation: {str(e)}")
                # Yield an error message in SSE format
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return Response(generate(), mimetype='text/event-stream')

    except Exception as e:
        app.logger.error(f"Error in chat endpoint: {str(e)}")
        return jsonify({'error': str(e)}), 500


# --- Reprocessing Endpoints ---
@app.route('/recording/<int:recording_id>/reprocess_transcription', methods=['POST'])
@login_required
def reprocess_transcription(recording_id):
    """Reprocess transcription for a given recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to reprocess this recording'}), 403

        if not recording.audio_path or not os.path.exists(recording.audio_path):
            return jsonify({'error': 'Audio file not found for reprocessing'}), 404

        if recording.status in ['PROCESSING', 'SUMMARIZING']:
            return jsonify({'error': 'Recording is already being processed'}), 400

        # --- Convert file if necessary before reprocessing ---
        filepath = recording.audio_path
        filename_for_asr = recording.original_filename or os.path.basename(filepath)
        filename_lower = filename_for_asr.lower()

        supported_formats = ('.wav', '.mp3', '.flac')
        if not filename_lower.endswith(supported_formats):
            app.logger.info(f"Reprocessing: Converting {filename_lower} format to WAV.")
            base_filepath, file_ext = os.path.splitext(filepath)
            temp_wav_filepath = f"{base_filepath}_temp.wav"
            final_wav_filepath = f"{base_filepath}.wav"

            try:
                # Convert to a temporary file first
                subprocess.run(
                    ['ffmpeg', '-i', filepath, '-y', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', temp_wav_filepath],
                    check=True, capture_output=True, text=True
                )
                app.logger.info(f"Successfully converted {filepath} to {temp_wav_filepath}")

                # If the original file is not the same as the final wav file, remove it
                if filepath.lower() != final_wav_filepath.lower():
                    os.remove(filepath)
                
                # Rename the temporary file to the final filename
                os.rename(temp_wav_filepath, final_wav_filepath)
                
                filepath = final_wav_filepath
                filename_for_asr = os.path.basename(filepath)
                
                # Update database with new path and mime type
                recording.audio_path = filepath
                recording.mime_type, _ = mimetypes.guess_type(filepath)
                db.session.commit()

            except FileNotFoundError:
                app.logger.error("ffmpeg command not found. Please ensure ffmpeg is installed and in the system's PATH.")
                return jsonify({'error': 'Audio conversion tool (ffmpeg) not found on server.'}), 500
            except subprocess.CalledProcessError as e:
                app.logger.error(f"ffmpeg conversion failed for {filepath}: {e.stderr}")
                return jsonify({'error': f'Failed to convert audio file: {e.stderr}'}), 500

        # --- Proceed with reprocessing ---
        recording.transcription = None
        recording.summary = None
        recording.status = 'PROCESSING'
        db.session.commit()

        app.logger.info(f"Starting transcription reprocessing for recording {recording_id}")

        # Decide which transcription method to use
        if USE_ASR_ENDPOINT:
            app.logger.info(f"Using ASR endpoint for reprocessing recording {recording_id}")
            
            data = request.json or {}
            language = data.get('language') or (recording.owner.transcription_language if recording.owner else None)
            min_speakers = data.get('min_speakers')
            max_speakers = data.get('max_speakers')
            if 'ASR_DIARIZE' in os.environ:
                diarize_setting = ASR_DIARIZE
            else:
                diarize_setting = recording.owner.diarize if recording.owner else False

            start_time = datetime.utcnow()
            thread = threading.Thread(
                target=transcribe_audio_asr,
                args=(app.app_context(), recording.id, filepath, filename_for_asr, start_time, recording.mime_type, language, diarize_setting, min_speakers, max_speakers)
            )
        else:
            app.logger.info(f"Using standard transcription API for reprocessing recording {recording_id}")
            start_time = datetime.utcnow()
            thread = threading.Thread(
                target=transcribe_audio_task,
                args=(app.app_context(), recording.id, filepath, filename_for_asr, start_time)
            )
        
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Transcription reprocessing started',
            'recording': recording.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error reprocessing transcription for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/recording/<int:recording_id>/reprocess_summary', methods=['POST'])
@login_required
def reprocess_summary(recording_id):
    """Reprocess summary for a given recording (requires existing transcription)."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404
            
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to reprocess this recording'}), 403
            
        # Check if transcription exists
        if not recording.transcription or len(recording.transcription.strip()) < 10:
            return jsonify({'error': 'No valid transcription available for summary generation'}), 400
            
        # Check if already processing
        if recording.status in ['PROCESSING', 'SUMMARIZING']:
            return jsonify({'error': 'Recording is already being processed'}), 400
            
        # Check if OpenRouter client is available
        if client is None:
            return jsonify({'error': 'Summary service is not available (OpenRouter client not configured)'}), 503
            
        # Set status to SUMMARIZING and clear existing summary
        recording.summary = None
        recording.status = 'SUMMARIZING'
        db.session.commit()
        
        app.logger.info(f"Starting summary reprocessing for recording {recording_id}")
        
        # Start summary generation in background thread
        def reprocess_summary_task(app_context, recording_id):
            with app_context:
                recording = db.session.get(Recording, recording_id)
                if not recording:
                    app.logger.error(f"Recording {recording_id} not found during summary reprocessing")
                    return
                    
                try:
                    # Get user preferences
                    user_summary_prompt = None
                    user_output_language = None
                    if recording.owner:
                        user_summary_prompt = recording.owner.summary_prompt
                        user_output_language = recording.owner.output_language
                    
                    # Prepare prompt (same logic as in transcribe_audio_task)
                    default_summary_prompt = """Analyze the following audio transcription and generate a concise title and a brief summary.

Transcription:
\"\"\"
{transcription}
\"\"\"

Respond STRICTLY with a JSON object containing two keys: "title" (a short, descriptive title, max 6 words without using words introductory words and phrases like brief, "discussion on", "Meeting about" etc.) and "minutes" (a paragraph summarizing the key points, max 150 words). The title should get to the point without inroductory phrases as we have very little space to show the title.
{language_directive}
Example Format:
{{
  "title": "Q3 Results for SPERO Program",
  "summary": "### Minutes

**Meeting Participants:**  
- Bob  
- Alice  

---

**1. Introduction and Overview:**
- Alice expressed interest in understanding the responsibilities at the north division and the potential for technological innovations.
....
### Key Issues Discussed
....
//and so on and so forth. Make sure not to miss any nuance or details. 
"
}}

JSON Response:"""
                    
                    language_directive = f"Please provide the title and summary in {user_output_language}." if user_output_language else ""
                    
                    # Get configurable transcript length limit for reprocessing
                    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
                    if transcript_limit == -1:
                        # No limit
                        transcript_text = recording.transcription
                    else:
                        transcript_text = recording.transcription[:transcript_limit]
                    
                    if user_summary_prompt:
                        prompt_text = f"""Analyze the following audio transcription and generate a concise title and a summary according to the following instructions.
                
Transcription:
\"\"\"
{transcript_text} 
\"\"\"

Generate a response STRICTLY as a JSON object with two keys: "title" and "summary". The summary should be markdown, not JSON. 

For the "title": create a short, descriptive title (max 6 words, no introductory phrases like "brief", "discussion on", "Meeting about").
For the "summary": {user_summary_prompt}. 

{language_directive}

JSON Response:"""
                    else:
                        prompt_text = default_summary_prompt.format(transcription=transcript_text, language_directive=language_directive)
                    
                    system_message_content = "You are an AI assistant that generates titles and summaries for meeting transcripts. Respond only with the requested JSON object."
                    if user_output_language:
                        system_message_content += f" Ensure your response (both title and summary) is in {user_output_language}."
                    
                    # Log the request details for debugging
                    app.logger.info(f"Making OpenRouter API request for summary reprocessing {recording_id}")
                    app.logger.debug(f"Model: {TEXT_MODEL_NAME}")
                    app.logger.debug(f"System message length: {len(system_message_content)}")
                    app.logger.debug(f"User prompt length: {len(prompt_text)}")
                    
                    # Call OpenRouter API
                    completion = client.chat.completions.create(
                        model=TEXT_MODEL_NAME,
                        messages=[
                            {"role": "system", "content": system_message_content},
                            {"role": "user", "content": prompt_text}
                        ],
                        temperature=0.5,
                        max_tokens=int(os.environ.get("SUMMARY_MAX_TOKENS", "3000")),
                        response_format={"type": "json_object"}
                    )
                    
                    app.logger.info(f"OpenRouter API request completed for summary reprocessing {recording_id}")
                    
                    response_content = completion.choices[0].message.content
                    app.logger.debug(f"Raw OpenRouter response for summary reprocessing {recording_id}: {response_content}")
                    
                    # Check if response is empty or None
                    if not response_content or response_content.strip() == "":
                        app.logger.error(f"Empty response from OpenRouter for summary reprocessing {recording_id}")
                        recording.summary = "[AI summary generation failed: Empty response from API]"
                        recording.status = 'COMPLETED'
                        db.session.commit()
                        return
                    
                    # Parse response (same logic as in transcribe_audio_task)
                    json_match = re.search(r'```(?:json)?(.*?)```|(.+)', response_content, re.DOTALL)
                    
                    if json_match:
                        sanitized_response = json_match.group(1) if json_match.group(1) else json_match.group(2)
                        sanitized_response = sanitized_response.strip()
                    else:
                        sanitized_response = response_content.strip()
                    
                    # Additional check for empty sanitized response
                    if not sanitized_response or sanitized_response.strip() in ["{}", ""]:
                        app.logger.error(f"Empty or invalid JSON response from OpenRouter for summary reprocessing {recording_id}: '{sanitized_response}'")
                        recording.summary = "[AI summary generation failed: Invalid JSON response]"
                        recording.status = 'COMPLETED'
                        db.session.commit()
                        return
                        
                    summary_data = safe_json_loads(sanitized_response, {})
                    
                    # Check if the parsed JSON is empty or doesn't contain expected keys
                    if not summary_data or (not summary_data.get("title") and not summary_data.get("summary")):
                        app.logger.error(f"OpenRouter returned empty or invalid JSON structure for summary reprocessing {recording_id}: {summary_data}")
                        recording.summary = "[AI summary generation failed: Empty response structure]"
                        recording.status = 'COMPLETED'
                        db.session.commit()
                        return
                    
                    # Debug log the parsed response
                    app.logger.debug(f"Parsed summary data for reprocessing {recording_id}: {summary_data}")
                    
                    raw_title = summary_data.get("title")
                    raw_summary = summary_data.get("summary")
                    
                    # Log what we got from the response
                    app.logger.debug(f"Raw title for reprocessing {recording_id}: {raw_title}")
                    app.logger.debug(f"Raw summary for reprocessing {recording_id}: {raw_summary}")
                    
                    # Process title
                    if isinstance(raw_title, str):
                        processed_title = raw_title.strip()
                    elif raw_title is None:
                        processed_title = "[Title not generated]"
                        app.logger.warning(f"Title was missing in OpenRouter response for summary reprocessing {recording_id}.")
                    else:
                        processed_title = "[Title generation error: Unexpected format]"
                        app.logger.warning(f"Title had unexpected format for summary reprocessing {recording_id}: {type(raw_title)}. Content: {raw_title}")
                    
                    # Process summary
                    if isinstance(raw_summary, str):
                        processed_summary = raw_summary.strip()
                    elif isinstance(raw_summary, dict):
                        processed_summary = json.dumps(raw_summary, indent=2)
                        app.logger.info(f"Generated summary for reprocessing {recording_id} was a dictionary, converted to JSON string.")
                    elif raw_summary is None:
                        processed_summary = "[Summary not generated]"
                        app.logger.warning(f"Summary was missing in OpenRouter response for summary reprocessing {recording_id}.")
                    else:
                        processed_summary = "[Summary generation error: Unexpected format]"
                        app.logger.warning(f"Summary had unexpected format for summary reprocessing {recording_id}: {type(raw_summary)}. Content: {raw_summary}")
                    
                    # Check if both title and summary are valid before updating
                    if raw_title is not None and raw_summary is not None:
                        recording.title = processed_title
                        recording.summary = processed_summary
                        recording.status = 'COMPLETED'
                        app.logger.info(f"Summary reprocessing completed successfully for recording {recording_id}")
                    else:
                        # Handle cases where one or both keys might be missing
                        if raw_title is None:
                            recording.title = recording.title or "[Title not generated]"  # Keep existing if any
                        else:
                            recording.title = processed_title
                        
                        if raw_summary is None:
                            recording.summary = "[AI summary generation failed: Missing summary key]"
                        else:
                            recording.summary = processed_summary
                        
                        app.logger.warning(f"OpenRouter response for summary reprocessing {recording_id} might have lacked 'title' or 'summary'. Title: {raw_title is not None}, Summary: {raw_summary is not None}. Response: {sanitized_response}")
                        recording.status = 'COMPLETED'  # Still completed, but summary might be partial/failed
                    
                    db.session.commit()
                    
                except json.JSONDecodeError as json_e:
                    app.logger.error(f"Failed to parse JSON response from OpenRouter for summary reprocessing {recording_id}: {json_e}. Response: {sanitized_response}")
                    recording.summary = f"[AI summary generation failed: Invalid JSON response ({json_e})]"
                    recording.status = 'COMPLETED'
                    db.session.commit()
                    
                except Exception as summary_e:
                    app.logger.error(f"Error during summary reprocessing for recording {recording_id}: {str(summary_e)}")
                    recording.summary = format_api_error_message(str(summary_e))
                    recording.status = 'COMPLETED'
                    db.session.commit()
        
        thread = threading.Thread(
            target=reprocess_summary_task,
            args=(app.app_context(), recording.id)
        )
        thread.start()
        
        return jsonify({
            'success': True, 
            'message': 'Summary reprocessing started',
            'recording': recording.to_dict()
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error reprocessing summary for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/recording/<int:recording_id>/reset_status', methods=['POST'])
@login_required
def reset_status(recording_id):
    """Resets the status of a stuck or failed recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403

        # Allow resetting if it's stuck or failed
        if recording.status in ['PROCESSING', 'SUMMARIZING', 'FAILED']:
            recording.status = 'FAILED'
            recording.error_message = "Manually reset from stuck or failed state."
            db.session.commit()
            app.logger.info(f"Manually reset status for recording {recording_id} to FAILED.")
            return jsonify({'success': True, 'message': 'Recording status has been reset.', 'recording': recording.to_dict()})
        else:
            return jsonify({'error': f'Recording is not in a state that can be reset. Current status: {recording.status}'}), 400

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error resetting status for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

# --- Authentication Routes ---
@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def register():
    # Check if registration is allowed
    allow_registration = os.environ.get('ALLOW_REGISTRATION', 'true').lower() == 'true'
    
    if not allow_registration:
        flash('Registration is currently disabled. Please contact the administrator.', 'danger')
        return redirect(url_for('login'))
        
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        user = User(username=form.username.data, email=form.email.data, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html', title='Register', form=form)

def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user, remember=form.remember.data)
            next_page = request.args.get('next')
            if not is_safe_url(next_page):
                return redirect(url_for('index'))
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('Login unsuccessful. Please check email and password.', 'danger')
    
    return render_template('login.html', title='Login', form=form)

@app.route('/logout')
@csrf.exempt
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        # Handle language preference updates
        transcription_lang = request.form.get('transcription_language')
        output_lang = request.form.get('output_language')
        summary_prompt_text = request.form.get('summary_prompt')
        user_name = request.form.get('user_name')
        user_job_title = request.form.get('user_job_title')
        user_company = request.form.get('user_company')

        current_user.transcription_language = transcription_lang if transcription_lang else None
        current_user.output_language = output_lang if output_lang else None
        current_user.summary_prompt = summary_prompt_text if summary_prompt_text else None
        current_user.name = user_name if user_name else None
        current_user.job_title = user_job_title if user_job_title else None
        current_user.company = user_company if user_company else None
        
        # Only update diarize if it's not locked by env var
        if 'ASR_DIARIZE' not in os.environ:
            current_user.diarize = 'diarize' in request.form
        
        db.session.commit()
        flash('Account details updated successfully!', 'success')
        return redirect(url_for('account'))
        
    default_summary_prompt_text = """Identify the key issues discussed. First, give me minutes. Then, give me the key issues discussed. Then, any key takeaways. Then, key next steps. Then, all important things that I didn't ask for but that need to be recorded. Make sure every important nuance is covered."""
    
    asr_diarize_locked = 'ASR_DIARIZE' in os.environ
    
    return render_template('account.html', 
                           title='Account', 
                           default_summary_prompt_text=default_summary_prompt_text,
                           use_asr_endpoint=USE_ASR_ENDPOINT,
                           asr_diarize_locked=asr_diarize_locked,
                           asr_diarize_env_value=ASR_DIARIZE)

@app.route('/change_password', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def change_password():
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    # Validate form data
    if not current_password or not new_password or not confirm_password:
        flash('All fields are required.', 'danger')
        return redirect(url_for('account'))
    
    if new_password != confirm_password:
        flash('New password and confirmation do not match.', 'danger')
        return redirect(url_for('account'))
        
    # Custom validation for new password
    try:
        password_check(None, type('obj', (object,), {'data': new_password}))
    except ValidationError as e:
        flash(str(e), 'danger')
        return redirect(url_for('account'))
    
    # Check if current password is correct
    if not bcrypt.check_password_hash(current_user.password, current_password):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('account'))
    
    # Update password
    current_user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    db.session.commit()
    
    flash('Your password has been updated successfully.', 'success')
    return redirect(url_for('account'))

# --- Admin Routes ---
@app.route('/admin', methods=['GET'])
@login_required
def admin():
    # Check if user is admin
    if not current_user.is_admin:
        flash('You do not have permission to access the admin page.', 'danger')
        return redirect(url_for('index'))
    return render_template('admin.html', title='Admin Dashboard')

@app.route('/admin/users', methods=['GET'])
@login_required
def admin_get_users():
    # Check if user is admin
    if not current_user.is_admin:
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
            'recordings_count': recordings_count,
            'storage_used': storage_used
        })
    
    return jsonify(user_data)

@app.route('/admin/users', methods=['POST'])
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

@app.route('/admin/users/<int:user_id>', methods=['PUT'])
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
    
    db.session.commit()
    
    # Get recordings count and storage used
    recordings_count = len(user.recordings)
    storage_used = sum(r.file_size for r in user.recordings if r.file_size) or 0
    
    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_admin': user.is_admin,
        'recordings_count': recordings_count,
        'storage_used': storage_used
    })

@app.route('/admin/users/<int:user_id>', methods=['DELETE'])
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
    for recording in user.recordings:
        try:
            if recording.audio_path and os.path.exists(recording.audio_path):
                os.remove(recording.audio_path)
        except Exception as e:
            app.logger.error(f"Error deleting audio file {recording.audio_path}: {e}")
    
    # Delete user
    db.session.delete(user)
    db.session.commit()
    
    return jsonify({'success': True})

@app.route('/admin/users/<int:user_id>/toggle-admin', methods=['POST'])
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

@app.route('/admin/stats', methods=['GET'])
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

@app.route('/admin/settings', methods=['GET'])
@login_required
def admin_get_settings():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    settings = SystemSetting.query.all()
    return jsonify([setting.to_dict() for setting in settings])

@app.route('/admin/settings', methods=['POST'])
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
        app.logger.error(f"Error updating setting {key}: {e}")
        return jsonify({'error': str(e)}), 500

# --- Configuration API ---
@app.route('/api/config', methods=['GET'])
def get_config():
    """Get application configuration settings for the frontend."""
    try:
        # Get configurable file size limit
        max_file_size_mb = SystemSetting.get_setting('max_file_size_mb', 250)
        
        return jsonify({
            'max_file_size_mb': max_file_size_mb,
            'use_asr_endpoint': USE_ASR_ENDPOINT
        })
    except Exception as e:
        app.logger.error(f"Error fetching configuration: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/csrf-token', methods=['GET'])
@csrf.exempt  # Exempt this endpoint from CSRF protection since it's providing tokens
def get_csrf_token():
    """Get a fresh CSRF token for the frontend."""
    try:
        from flask_wtf.csrf import generate_csrf
        token = generate_csrf()
        app.logger.info("Fresh CSRF token generated successfully")
        return jsonify({'csrf_token': token})
    except Exception as e:
        app.logger.error(f"Error generating CSRF token: {e}")
        return jsonify({'error': str(e)}), 500

# --- Flask Routes ---
@app.route('/')
@login_required
def index():
    # Pass the ASR config to the template
    return render_template('index.html', use_asr_endpoint=USE_ASR_ENDPOINT)

@app.route('/recordings', methods=['GET'])
def get_recordings():
    try:
        # Check if user is logged in
        if not current_user.is_authenticated:
            return jsonify([])  # Return empty array if not logged in
            
        # Filter recordings by the current user
        stmt = select(Recording).where(Recording.user_id == current_user.id).order_by(Recording.created_at.desc())
        recordings = db.session.execute(stmt).scalars().all()
        return jsonify([recording.to_dict() for recording in recordings])
    except Exception as e:
        app.logger.error(f"Error fetching recordings: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/inbox_recordings', methods=['GET'])
@login_required
@limiter.limit("1250 per hour")  # Allow frequent polling for inbox recordings
def get_inbox_recordings():
    """Get recordings that are in the inbox and currently processing."""
    try:
        stmt = select(Recording).where(
            Recording.user_id == current_user.id,
            Recording.is_inbox == True,
            Recording.status.in_(['PENDING', 'PROCESSING', 'SUMMARIZING'])
        ).order_by(Recording.created_at.desc())
        
        recordings = db.session.execute(stmt).scalars().all()
        return jsonify([recording.to_dict() for recording in recordings])
    except Exception as e:
        app.logger.error(f"Error fetching inbox recordings: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/save', methods=['POST'])
@login_required
def save_metadata():
    try:
        data = request.json
        if not data: return jsonify({'error': 'No data provided'}), 400
        recording_id = data.get('id')
        if not recording_id: return jsonify({'error': 'No recording ID provided'}), 400

        recording = db.session.get(Recording, recording_id)
        if not recording: return jsonify({'error': 'Recording not found'}), 404
        
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to edit this recording'}), 403

        # Update fields if provided (with sanitization for notes and summary)
        if 'title' in data: recording.title = data['title']
        if 'participants' in data: recording.participants = data['participants']
        if 'notes' in data: recording.notes = sanitize_html(data['notes']) if data['notes'] else data['notes']
        if 'summary' in data: recording.summary = sanitize_html(data['summary']) if data['summary'] else data['summary']
        if 'is_inbox' in data: recording.is_inbox = data['is_inbox']
        if 'is_highlighted' in data: recording.is_highlighted = data['is_highlighted']
        if 'meeting_date' in data:
            try:
                # Attempt to parse date string (e.g., "YYYY-MM-DD")
                date_str = data['meeting_date']
                if date_str:
                    recording.meeting_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                else:
                    recording.meeting_date = None # Allow clearing the date
            except (ValueError, TypeError) as e:
                app.logger.warning(f"Could not parse meeting_date '{data.get('meeting_date')}': {e}")
                # Optionally return an error or just ignore the invalid date
                # return jsonify({'error': f"Invalid date format for meeting_date. Use YYYY-MM-DD."}), 400

        # Do not update transcription or status here
        db.session.commit()
        return jsonify({'success': True, 'recording': recording.to_dict()})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error saving metadata for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred while saving.'}), 500

@app.route('/recording/<int:recording_id>/update_transcription', methods=['POST'])
@login_required
def update_transcription(recording_id):
    """Updates the transcription content for a recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to edit this recording'}), 403

        data = request.json
        new_transcription = data.get('transcription')

        if new_transcription is None:
            return jsonify({'error': 'No transcription data provided'}), 400

        # The incoming data could be a JSON string (from ASR edit) or plain text
        recording.transcription = new_transcription
        
        # Optional: If the transcription changes, we might want to indicate that the summary is outdated.
        # For now, we'll just save the transcript. A "regenerate summary" button could be a good follow-up.

        db.session.commit()
        app.logger.info(f"Transcription for recording {recording_id} was updated.")
        
        return jsonify({'success': True, 'message': 'Transcription updated successfully.', 'recording': recording.to_dict()})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error updating transcription for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred while updating the transcription.'}), 500

# Toggle inbox status endpoint
@app.route('/recording/<int:recording_id>/toggle_inbox', methods=['POST'])
@login_required
def toggle_inbox(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404
            
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403

        # Toggle the inbox status
        recording.is_inbox = not recording.is_inbox
        db.session.commit()
        
        return jsonify({'success': True, 'is_inbox': recording.is_inbox})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error toggling inbox status for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred.'}), 500

# Toggle highlighted status endpoint
@app.route('/recording/<int:recording_id>/toggle_highlight', methods=['POST'])
@login_required
def toggle_highlight(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404
            
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403

        # Toggle the highlighted status
        recording.is_highlighted = not recording.is_highlighted
        db.session.commit()
        
        return jsonify({'success': True, 'is_highlighted': recording.is_highlighted})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error toggling highlighted status for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred.'}), 500


@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        original_filename = file.filename
        safe_filename = secure_filename(original_filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_filename}")

        # Get original file size
        file.seek(0, os.SEEK_END)
        original_file_size = file.tell()
        file.seek(0)

        # Check size limit before saving - only enforce if chunking is disabled or using ASR endpoint
        max_content_length = app.config.get('MAX_CONTENT_LENGTH')
        
        # Skip size check if chunking is enabled and using OpenAI Whisper API
        should_enforce_size_limit = True
        if ENABLE_CHUNKING and chunking_service and not USE_ASR_ENDPOINT:
            should_enforce_size_limit = False
            app.logger.info(f"Chunking enabled for OpenAI Whisper API - skipping {original_file_size/1024/1024:.1f}MB size limit check")
        
        if should_enforce_size_limit and max_content_length and original_file_size > max_content_length:
            raise RequestEntityTooLarge()

        file.save(filepath)
        app.logger.info(f"File saved to {filepath}")

        # --- Convert non-wav/mp3/flac files to WAV ---
        filename_lower = original_filename.lower()
        supported_formats = ('.wav', '.mp3', '.flac')
        convertible_formats = ('.amr', '.3gp', '.3gpp', '.m4a', '.aac', '.ogg', '.wma', '.webm')
        
        if not filename_lower.endswith(supported_formats):
            if filename_lower.endswith(convertible_formats):
                app.logger.info(f"Converting {filename_lower} format to WAV for processing.")
            else:
                app.logger.info(f"Attempting to convert unknown format ({filename_lower}) to WAV.")
            
            base_filepath, _ = os.path.splitext(filepath)
            temp_wav_filepath = f"{base_filepath}_temp.wav"
            wav_filepath = f"{base_filepath}.wav"

            try:
                # Using -acodec pcm_s16le for standard WAV format, 16kHz sample rate, mono
                subprocess.run(
                    ['ffmpeg', '-i', filepath, '-y', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', temp_wav_filepath],
                    check=True, capture_output=True, text=True
                )
                app.logger.info(f"Successfully converted {filepath} to {temp_wav_filepath}")
                
                # If the original file is not the same as the final wav file, remove it
                if filepath.lower() != wav_filepath.lower():
                    os.remove(filepath)
                
                # Rename the temporary file to the final filename
                os.rename(temp_wav_filepath, wav_filepath)
                
                filepath = wav_filepath
            except FileNotFoundError:
                app.logger.error("ffmpeg command not found. Please ensure ffmpeg is installed and in the system's PATH.")
                return jsonify({'error': 'Audio conversion tool (ffmpeg) not found on server.'}), 500
            except subprocess.CalledProcessError as e:
                app.logger.error(f"ffmpeg conversion failed for {filepath}: {e.stderr}")
                return jsonify({'error': f'Failed to convert audio file: {e.stderr}'}), 500

        # Get final file size (of original or converted file)
        final_file_size = os.path.getsize(filepath)

        # Determine MIME type of the final file
        mime_type, _ = mimetypes.guess_type(filepath)
        app.logger.info(f"Final MIME type: {mime_type} for file {filepath}")

        # Get notes from the form
        notes = request.form.get('notes')

        # Create initial database entry
        recording = Recording(
            audio_path=filepath,
            original_filename=original_filename,
            title=f"Recording - {original_filename}",
            file_size=final_file_size,
            status='PENDING',
            meeting_date=datetime.utcnow().date(),
            user_id=current_user.id,
            mime_type=mime_type,
            notes=notes,
            processing_source='upload'  # Track that this was manually uploaded
        )
        db.session.add(recording)
        db.session.commit()
        app.logger.info(f"Initial recording record created with ID: {recording.id}")

        # --- Start transcription & summarization in background thread ---
        start_time = datetime.utcnow()
        thread = threading.Thread(
            target=transcribe_audio_task,
            args=(app.app_context(), recording.id, filepath, os.path.basename(filepath), start_time)
        )
        thread.start()
        app.logger.info(f"Background processing thread started for recording ID: {recording.id}")

        return jsonify(recording.to_dict()), 202

    except RequestEntityTooLarge:
        max_size_mb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024)
        app.logger.warning(f"Upload failed: File too large (>{max_size_mb}MB)")
        return jsonify({
            'error': f'File too large. Maximum size is {max_size_mb:.0f} MB.',
            'max_size_mb': max_size_mb
        }), 413
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error during file upload: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred during upload.'}), 500


# Status Endpoint
@app.route('/status/<int:recording_id>', methods=['GET'])
@login_required
@limiter.limit("1250 per hour")  # Allow frequent polling for status checks
def get_status(recording_id):
    """Endpoint to check the transcription/summarization status."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404
        
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to view this recording'}), 403
            
        return jsonify(recording.to_dict())
    except Exception as e:
        app.logger.error(f"Error fetching status for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred.'}), 500

# Get Audio Endpoint
@app.route('/audio/<int:recording_id>')
@login_required
def get_audio(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording or not recording.audio_path:
            return jsonify({'error': 'Recording or audio file not found'}), 404
            
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to access this audio file'}), 403
        if not os.path.exists(recording.audio_path):
            app.logger.error(f"Audio file missing from server: {recording.audio_path}")
            return jsonify({'error': 'Audio file missing from server'}), 404
        return send_file(recording.audio_path)
    except Exception as e:
        app.logger.error(f"Error serving audio for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred.'}), 500

@app.route('/share/audio/<string:public_id>')
def get_shared_audio(public_id):
    try:
        share = Share.query.filter_by(public_id=public_id).first_or_404()
        recording = share.recording
        if not recording or not recording.audio_path:
            return jsonify({'error': 'Recording or audio file not found'}), 404
        if not os.path.exists(recording.audio_path):
            app.logger.error(f"Audio file missing from server: {recording.audio_path}")
            return jsonify({'error': 'Audio file missing from server'}), 404
        return send_file(recording.audio_path)
    except Exception as e:
        app.logger.error(f"Error serving shared audio for public_id {public_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred.'}), 500

# Delete Recording Endpoint
@app.route('/recording/<int:recording_id>', methods=['DELETE'])
@login_required
def delete_recording(recording_id):
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404
            
        # Check if the recording belongs to the current user
        if recording.user_id and recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to delete this recording'}), 403

        # Delete the audio file first
        try:
            if recording.audio_path and os.path.exists(recording.audio_path):
                os.remove(recording.audio_path)
                app.logger.info(f"Deleted audio file: {recording.audio_path}")
        except Exception as e:
            app.logger.error(f"Error deleting audio file {recording.audio_path}: {e}")

        # Delete the database record
        db.session.delete(recording)
        db.session.commit()
        app.logger.info(f"Deleted recording record ID: {recording_id}")

        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred while deleting.'}), 500


# --- Auto-Processing File Monitor Integration ---
def initialize_file_monitor():
    """Initialize file monitor after app is fully loaded to avoid circular imports."""
    try:
        # Import here to avoid circular imports
        import file_monitor
        file_monitor.start_file_monitor()
        app.logger.info("File monitor initialization completed")
    except Exception as e:
        app.logger.warning(f"File monitor initialization failed: {e}")

def get_file_monitor_functions():
    """Get file monitor functions, handling import errors gracefully."""
    try:
        import file_monitor
        return file_monitor.start_file_monitor, file_monitor.stop_file_monitor, file_monitor.get_file_monitor_status
    except ImportError as e:
        app.logger.warning(f"File monitor not available: {e}")
        
        # Create stub functions if file_monitor is not available
        def start_file_monitor():
            pass
        def stop_file_monitor():
            pass
        def get_file_monitor_status():
            return {'running': False, 'error': 'File monitor module not available'}
        
        return start_file_monitor, stop_file_monitor, get_file_monitor_status

# --- Auto-Processing API Endpoints ---
@app.route('/admin/auto-process/status', methods=['GET'])
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
        app.logger.error(f"Error getting auto-process status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/auto-process/start', methods=['POST'])
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
        app.logger.error(f"Error starting auto-process: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/auto-process/stop', methods=['POST'])
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
        app.logger.error(f"Error stopping auto-process: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/admin/auto-process/config', methods=['POST'])
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
        app.logger.error(f"Error updating auto-process config: {e}")
        return jsonify({'error': str(e)}), 500

with app.app_context():
    # Set dynamic MAX_CONTENT_LENGTH based on database setting
    max_file_size_mb = SystemSetting.get_setting('max_file_size_mb', 250)
    app.config['MAX_CONTENT_LENGTH'] = max_file_size_mb * 1024 * 1024
    app.logger.info(f"Set MAX_CONTENT_LENGTH to {max_file_size_mb}MB from database setting")

    # Initialize file monitor after app setup
    initialize_file_monitor()

if __name__ == '__main__':
    # Consider using waitress or gunicorn for production
    # waitress-serve --host 0.0.0.0 --port 8899 app:app
    # For development:
    app.run(host='0.0.0.0', port=8899, debug=True) # Set debug=False if thread issues arise
