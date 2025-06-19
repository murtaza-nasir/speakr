# Speakr - Audio Transcription and Summarization App
import os
import sys
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash
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
from sqlalchemy import select
import threading
from dotenv import load_dotenv # Import load_dotenv
import httpx 
import re
import subprocess
import mimetypes
import markdown
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError

# Load environment variables from .env file
load_dotenv()

# Initialize Flask-Bcrypt
bcrypt = Bcrypt()

# Helper function to convert markdown to HTML
def md_to_html(text):
    if not text:
        return ""
    # Convert markdown to HTML with extensions for tables, code highlighting, etc.
    html = markdown.markdown(text, extensions=[
        'extra',            # Includes many useful extensions like sane_lists, fenced_code, tables, etc.
        'codehilite',       # Syntax highlighting for code blocks (ensure Pygments is installed)
        'smarty'            # Smart quotes, dashes, etc.
    ])
    return html

app = Flask(__name__)
# Use environment variables or default paths for Docker compatibility
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:////data/instance/transcriptions.db')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/data/uploads')
app.config['MAX_CONTENT_LENGTH'] = 250 * 1024 * 1024  # 250MB max file size
# Set a secret key for session management and CSRF protection
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-dev-key-change-in-production')
db = SQLAlchemy()
db.init_app(app)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'
bcrypt.init_app(app)

# Add context processor to make 'now' available to all templates
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

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
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'use_count': self.use_count
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
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'meeting_date': self.meeting_date.isoformat() if self.meeting_date else None, # <-- ADDED: Include meeting_date
            'file_size': self.file_size,
            'original_filename': self.original_filename, # <-- ADDED: Include original filename
            'user_id': self.user_id,
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'mime_type': self.mime_type
        }

# --- Forms for Authentication ---
class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
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
ASR_ENCODE = os.environ.get('ASR_ENCODE', 'true').lower() == 'true'
ASR_TASK = os.environ.get('ASR_TASK', 'transcribe')
ASR_DIARIZE = os.environ.get('ASR_DIARIZE', 'true').lower() == 'true'
ASR_MIN_SPEAKERS = os.environ.get('ASR_MIN_SPEAKERS')
ASR_MAX_SPEAKERS = os.environ.get('ASR_MAX_SPEAKERS')

app.logger.info(f"Using OpenRouter model for summaries: {TEXT_MODEL_NAME}")
app.logger.info(f"Using Whisper API at: {transcription_base_url}")
if USE_ASR_ENDPOINT:
    app.logger.info(f"ASR endpoint is enabled at: {ASR_BASE_URL}")

# --- Background Transcription & Summarization Task ---
def generate_summary_task(app_context, recording_id):
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
        
        if user_summary_prompt:
            prompt_text = f"""Analyze the following audio transcription and generate a concise title and a summary according to the following instructions.
            
Transcription:
\"\"\"
{recording.transcription[:50000]} 
\"\"\"

Generate a response STRICTLY as a JSON object with two keys: "title" and "summary". The summary should be markdown, not JSON. 

For the "title": create a short, descriptive title (max 6 words, no introductory phrases like "brief", "discussion on", "Meeting about").
For the "summary": {user_summary_prompt}. 

{language_directive}

JSON Response:"""
        else:
            prompt_text = default_summary_prompt.format(transcription=recording.transcription[:30000], language_directive=language_directive)
        
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
            summary_data = json.loads(sanitized_response)
            
            raw_title = summary_data.get("title")
            raw_summary = summary_data.get("summary")

            recording.title = raw_title.strip() if isinstance(raw_title, str) else "[Title not generated]"
            recording.summary = raw_summary.strip() if isinstance(raw_summary, str) else "[Summary not generated]"
            recording.status = 'COMPLETED'
            app.logger.info(f"Title and summary processed successfully for recording {recording_id}.")

        except Exception as summary_e:
            app.logger.error(f"Error calling OpenRouter API for summary ({recording_id}): {str(summary_e)}")
            recording.summary = f"[AI summary generation failed: API Error ({str(summary_e)})]"
            recording.status = 'COMPLETED'

        db.session.commit()

def transcribe_audio_asr(app_context, recording_id, filepath, original_filename, mime_type=None, language=None, diarize=False, min_speakers=None, max_speakers=None):
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
                    'encode': ASR_ENCODE,
                    'task': ASR_TASK,
                    'output': 'txt'
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
                    recording.transcription = response.text
            
            app.logger.info(f"ASR transcription completed for recording {recording_id}.")
            generate_summary_task(app_context, recording_id)

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"ASR processing FAILED for recording {recording_id}: {str(e)}", exc_info=True)
            recording = db.session.get(Recording, recording_id)
            if recording:
                recording.status = 'FAILED'
                recording.transcription = f"ASR processing failed: {str(e)}"
                db.session.commit()

def transcribe_audio_task(app_context, recording_id, filepath, filename_for_asr):
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
        transcribe_audio_asr(app_context, recording_id, filepath, filename_for_asr, mime_type=recording.mime_type, language=user_transcription_language, diarize=diarize_setting)
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

            # --- Step 1: Transcription ---
            with open(filepath, 'rb') as audio_file:
                transcription_client = OpenAI(
                    api_key=transcription_api_key,
                    base_url=transcription_base_url,
                    http_client=http_client_no_proxy
                )
                whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")
                
                user_transcription_language = None
                user_output_language = None
                if recording and recording.owner:
                    user_transcription_language = recording.owner.transcription_language
                    user_output_language = recording.owner.output_language
                
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
            recording.transcription = transcript.text
            app.logger.info(f"Transcription completed for recording {recording_id}. Text length: {len(recording.transcription)}")
            generate_summary_task(app_context, recording_id)

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

                db.session.commit()

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

        transcription = recording.transcription
        new_participants = []
        speaker_names_used = []

        for speaker_label, new_name_info in speaker_map.items():
            new_name = new_name_info['name']
            if new_name_info.get('isMe'):
                new_name = current_user.name or 'Me'
            
            if new_name:
                # Regex to handle variations like [SPEAKER_00], [ SPEAKER_00 ], etc.
                transcription = re.sub(r'\[\s*' + re.escape(speaker_label) + r'\s*\]', f'[{new_name}]', transcription)
                if new_name not in new_participants:
                    new_participants.append(new_name)
                    speaker_names_used.append(new_name)

        recording.transcription = transcription
        if new_participants:
            recording.participants = ', '.join(new_participants)
        
        # Update speaker usage statistics
        if speaker_names_used:
            update_speaker_usage(speaker_names_used)
        
        db.session.commit()

        if regenerate_summary:
            app.logger.info(f"Regenerating summary for recording {recording_id} after speaker update.")
            thread = threading.Thread(
                target=generate_summary_task,
                args=(app.app_context(), recording.id)
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

    # Extract existing speaker labels (e.g., SPEAKER_00, SPEAKER_01) in order of appearance
    all_labels = re.findall(r'\[(SPEAKER_\d+)\]', transcription)
    seen = set()
    speaker_labels = [x for x in all_labels if not (x in seen or seen.add(x))]
    
    if not speaker_labels:
        return {}

    prompt = f"""Analyze the following transcription and identify the names of the speakers. The speakers are labeled as {', '.join(speaker_labels)}. Based on the context of the conversation, determine the most likely name for each speaker label.

Transcription:
---
{transcription[:28000]}
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
        speaker_map = json.loads(response_content)

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

    if not unidentified_speakers:
        return {}

    prompt = f"""Analyze the following transcription and identify the names of the UNIDENTIFIED speakers only. The unidentified speakers are labeled as {', '.join(unidentified_speakers)}. Based on the context of the conversation, determine the most likely name for each of these speaker labels.

Note: Only identify the speakers listed above. Other speakers in the transcription may already be identified and should be ignored.

Transcription:
---
{transcription[:28000]}
---

Respond with a single JSON object where keys are the unidentified speaker labels (e.g., "SPEAKER_01") and values are the identified full names. If a name cannot be determined, use the value "Unknown".

Example:
{{
  "SPEAKER_01": "Jane Smith",
  "SPEAKER_03": "Unknown"
}}

JSON Response:
"""

    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are an expert in analyzing conversation transcripts to identify speakers. Your response must be a single, valid JSON object containing only the requested unidentified speakers."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        response_content = completion.choices[0].message.content
        speaker_map = json.loads(response_content)

        # Post-process the map to replace "Unknown" with an empty string
        for speaker_label, identified_name in speaker_map.items():
            if identified_name.strip().lower() == "unknown":
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
        all_labels = re.findall(r'\[(SPEAKER_\d+)\]', recording.transcription)
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
        # output_language_chat = os.environ.get("OUTPUT_LANGUAGE", None) # Get preferred output language for chat - Now from user
        user_chat_output_language = current_user.output_language if current_user.is_authenticated else None
        
        language_instruction = ""
        if user_chat_output_language:
            language_instruction = f"Please provide all your responses in {user_chat_output_language}."
        
        user_name = current_user.name if current_user.is_authenticated and current_user.name else "User"
        user_title = current_user.job_title if current_user.is_authenticated and current_user.job_title else "a professional"
        user_company = current_user.company if current_user.is_authenticated and current_user.company else "their organization"

        system_prompt = f"""You are a professional meeting and audio transcription analyst assisting {user_name}, who is a(n) {user_title} at {user_company}. {language_instruction} Analyze the following meeting information and respond to the specific request.

Following are the meeting participants and their roles:
{recording.participants or "No specific participants information provided."}

Following is the meeting transcript:
<<start transcript>>
{recording.transcription or "No transcript available."}
<<end transcript>>

Additional context and notes about the meeting:
{recording.notes or "none"}
"""
        
        # Call the LLM
        try:
            # Prepare messages array with system prompt and conversation history
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add message history if provided
            if message_history:
                messages.extend(message_history)
            
            # Add the current user message
            messages.append({"role": "user", "content": user_message})
            
            completion = client.chat.completions.create(
                model=TEXT_MODEL_NAME,
                messages=messages,
                temperature=0.7,
                max_tokens=int(os.environ.get("CHAT_MAX_TOKENS", "2000"))
            )
            
            response_content = completion.choices[0].message.content
            
            # Convert markdown in the response to HTML
            response_html = md_to_html(response_content)
            
            return jsonify({
                'response': response_content,
                'response_html': response_html,
                'success': True
            })
            
        except Exception as chat_error:
            app.logger.error(f"Error calling OpenRouter API for chat: {str(chat_error)}")
            return jsonify({'error': f'Chat service error: {str(chat_error)}'}), 500
            
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

        if not (filename_lower.endswith('.wav') or filename_lower.endswith('.mp3') or filename_lower.endswith('.flac')):
            app.logger.info(f"Reprocessing: Unsupported format detected ({filename_lower}). Converting to WAV.")
            base_filepath, _ = os.path.splitext(filepath)
            wav_filepath = f"{base_filepath}.wav"

            try:
                subprocess.run(
                    ['ffmpeg', '-i', filepath, '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', wav_filepath],
                    check=True, capture_output=True, text=True
                )
                app.logger.info(f"Successfully converted {filepath} to {wav_filepath}")
                os.remove(filepath)
                filepath = wav_filepath
                filename_for_asr = os.path.basename(wav_filepath)
                
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

            thread = threading.Thread(
                target=transcribe_audio_asr,
                args=(app.app_context(), recording.id, filepath, filename_for_asr, recording.mime_type, language, diarize_setting, min_speakers, max_speakers)
            )
        else:
            app.logger.info(f"Using standard transcription API for reprocessing recording {recording_id}")
            thread = threading.Thread(
                target=transcribe_audio_task,
                args=(app.app_context(), recording.id, filepath, filename_for_asr)
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
                    
                    if user_summary_prompt:
                        prompt_text = f"""Analyze the following audio transcription and generate a concise title and a summary according to the following instructions.
                
Transcription:
\"\"\"
{recording.transcription[:50000]} 
\"\"\"

Generate a response STRICTLY as a JSON object with two keys: "title" and "summary". The summary should be markdown, not JSON. 

For the "title": create a short, descriptive title (max 6 words, no introductory phrases like "brief", "discussion on", "Meeting about").
For the "summary": {user_summary_prompt}. 

{language_directive}

JSON Response:"""
                    else:
                        prompt_text = default_summary_prompt.format(transcription=recording.transcription[:30000], language_directive=language_directive)
                    
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
                        
                    summary_data = json.loads(sanitized_response)
                    
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
                    recording.summary = f"[AI summary generation failed: API Error ({str(summary_e)})]"
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


# --- Authentication Routes ---
@app.route('/register', methods=['GET', 'POST'])
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user, remember=form.remember.data)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('Login unsuccessful. Please check email and password.', 'danger')
    
    return render_template('login.html', title='Login', form=form)

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

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

# --- Flask Routes ---
@app.route('/')
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

        # Update fields if provided
        if 'title' in data: recording.title = data['title']
        if 'participants' in data: recording.participants = data['participants']
        if 'notes' in data: recording.notes = data['notes']
        if 'summary' in data: recording.summary = data['summary'] # <-- ADDED: Allow saving edited summary
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
        app.logger.error(f"Error saving metadata for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

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
        app.logger.error(f"Error toggling inbox status for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

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
        app.logger.error(f"Error toggling highlighted status for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


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

        # Check size limit before saving
        if original_file_size > app.config['MAX_CONTENT_LENGTH']:
            raise RequestEntityTooLarge()

        file.save(filepath)
        app.logger.info(f"File saved to {filepath}")

        # --- Convert non-wav/mp3 files to WAV ---
        filename_lower = original_filename.lower()
        if not (filename_lower.endswith('.wav') or filename_lower.endswith('.mp3') or filename_lower.endswith('.flac')):
            app.logger.info(f"Unsupported format detected ({filename_lower}). Converting to WAV.")
            
            base_filepath, _ = os.path.splitext(filepath)
            wav_filepath = f"{base_filepath}.wav"

            try:
                # Using -acodec pcm_s16le for standard WAV format, 16kHz sample rate, mono
                subprocess.run(
                    ['ffmpeg', '-i', filepath, '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', wav_filepath],
                    check=True, capture_output=True, text=True
                )
                app.logger.info(f"Successfully converted {filepath} to {wav_filepath}")
                os.remove(filepath)
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

        # Create initial database entry
        recording = Recording(
            audio_path=filepath,
            original_filename=original_filename,
            title=f"Recording - {original_filename}",
            file_size=final_file_size,
            status='PENDING',
            meeting_date=datetime.utcnow().date(),
            user_id=current_user.id,
            mime_type=mime_type
        )
        db.session.add(recording)
        db.session.commit()
        app.logger.info(f"Initial recording record created with ID: {recording.id}")

        # --- Start transcription & summarization in background thread ---
        thread = threading.Thread(
            target=transcribe_audio_task,
            args=(app.app_context(), recording.id, filepath, os.path.basename(filepath))
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
        return jsonify({'error': str(e)}), 500


# Status Endpoint
@app.route('/status/<int:recording_id>', methods=['GET'])
@login_required
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
        app.logger.error(f"Error fetching status for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

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
        app.logger.error(f"Error serving audio for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500

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
        app.logger.error(f"Error deleting recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # Consider using waitress or gunicorn for production
    # waitress-serve --host 0.0.0.0 --port 8899 app:app
    # For development:
    app.run(host='0.0.0.0', port=8899, debug=True) # Set debug=False if thread issues arise
