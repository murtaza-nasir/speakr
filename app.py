# Speakr - Audio Transcription and Summarization App
import os
import sys
from flask import Flask, render_template, request, jsonify, send_file, Markup, redirect, url_for, flash
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
        'tables',           # Support for tables
        'fenced_code',      # Support for ```code blocks```
        'codehilite',       # Syntax highlighting for code blocks
        'nl2br',            # Convert newlines to <br> tags
        'sane_lists',       # Better list handling
        'smarty'            # Smart quotes, dashes, etc.
    ])
    return html

app = Flask(__name__)
# Ensure the path uses the directory structure from your setup script
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////opt/transcription-app/instance/transcriptions.db'
app.config['UPLOAD_FOLDER'] = '/opt/transcription-app/uploads' # Use absolute path based on setup
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
    
    def __repr__(self):
        return f"User('{self.username}', '{self.email}')"
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
            'is_highlighted': self.is_highlighted
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

app.logger.info(f"Using OpenRouter model for summaries: {TEXT_MODEL_NAME}")

# --- Background Transcription & Summarization Task ---
def transcribe_audio_task(app_context, recording_id, filepath, original_filename):
    """Runs the transcription and summarization in a background thread."""
    with app_context: # Need app context for db operations in thread
        recording = db.session.get(Recording, recording_id)
        if not recording:
            app.logger.error(f"Error: Recording {recording_id} not found for transcription.")
            return

        try:
            app.logger.info(f"Starting transcription for recording {recording_id} ({original_filename})...")
            recording.status = 'PROCESSING'
            db.session.commit()

            # --- Step 1: Transcription ---
            with open(filepath, 'rb') as audio_file:
                # NOTE: This still uses the hardcoded Whisper model.
                # If you want OpenRouter for transcription too, change the model here
                # and potentially adjust the API call if needed.
                # For now, assuming Whisper via a local compatible endpoint.
                # You might need a *separate* client for Whisper if it's at a different URL.
                # Example using the configured client (assuming it points to Whisper or OpenRouter handles it):
                transcription_client = OpenAI(
                    api_key=transcription_api_key,
                    base_url=transcription_base_url,
                    http_client=http_client_no_proxy # Reuse the same client configuration
                )
                # Get the Whisper model name from environment variables
                whisper_model = os.environ.get("WHISPER_MODEL", "Systran/faster-distil-whisper-large-v3")
                transcript = transcription_client.audio.transcriptions.create(
                    model=whisper_model, # Use model from environment variables
                    file=audio_file,
                    language="en" # Specify language if known
                )
            recording.transcription = transcript.text
            app.logger.info(f"Transcription completed for recording {recording_id}. Text length: {len(recording.transcription)}")
            # Don't commit yet, proceed to summarization

            # --- Step 2: Title & Summary Generation using OpenRouter ---
            if client is None: # Check if OpenRouter client initialized successfully earlier
                app.logger.warning(f"Skipping summary for {recording_id}: OpenRouter client not configured.")
                recording.summary = "[Summary skipped: OpenRouter client not configured]"
                recording.status = 'COMPLETED'
                db.session.commit()
                return # Exit cleanly
            
            recording.status = 'SUMMARIZING' # Update status
            db.session.commit()
            app.logger.info(f"Requesting title and summary from OpenRouter for recording {recording_id} using model {TEXT_MODEL_NAME}...")

            if not recording.transcription or len(recording.transcription.strip()) < 10: # Basic check for valid transcript
                 app.logger.warning(f"Transcription for recording {recording_id} is too short or empty. Skipping summarization.")
                 recording.status = 'COMPLETED' # Mark as completed even without summary
                 recording.summary = "[Summary skipped due to short transcription]"
                 db.session.commit()
                 return # Exit the task cleanly

            # Prepare the prompt for OpenRouter
            prompt_text = f"""Analyze the following audio transcription and generate a concise title and a brief summary.

Transcription:
\"\"\"
{recording.transcription[:30000]}
\"\"\"

Respond STRICTLY with a JSON object containing two keys: "title" (a short, descriptive title, max 15 words) and "summary" (a paragraph summarizing the key points, max 150 words).
Example Format:
{{
  "title": "Example Meeting Discussion on Q3 Results",
  "summary": "The meeting covered the financial results for Q3, highlighting key achievements and areas for improvement. Action items were assigned for follow-up."
}}

JSON Response:""" # The prompt guides the model towards the desired output

            try:
                # Use the OpenRouter client configured earlier
                completion = client.chat.completions.create(
                    model=TEXT_MODEL_NAME,
                    messages=[
                        {"role": "system", "content": "You are an AI assistant that generates titles and summaries for meeting transcripts. Respond only with the requested JSON object."},
                        {"role": "user", "content": prompt_text}
                    ],
                    temperature=0.5, # Adjust temperature as needed
                    max_tokens=300, # Adjust based on expected title+summary length
                    response_format={"type": "json_object"} # Request JSON output
                )

                response_content = completion.choices[0].message.content
                app.logger.debug(f"Raw OpenRouter response for {recording_id}: {response_content}")
                
                try:
                    response_content = completion.choices[0].message.content
                    app.logger.debug(f"Raw OpenRouter response for {recording_id}: {response_content}")

                    # Use regex to extract JSON content from potential markdown code blocks
                    # This looks for content between markdown code blocks or just takes the whole content
                    json_match = re.search(r'```(?:json)?(.*?)```|(.+)', response_content, re.DOTALL)
                    
                    if json_match:
                        # Use the first group that matched (either between ``` or the whole content)
                        sanitized_response = json_match.group(1) if json_match.group(1) else json_match.group(2)
                        sanitized_response = sanitized_response.strip()
                    else:
                        sanitized_response = response_content.strip()
                        
                    summary_data = json.loads(response_content)
                    generated_title = summary_data.get("title")
                    generated_summary = summary_data.get("summary")

                    if generated_title and generated_summary:
                        # Update recording with AI generated content
                        recording.title = generated_title.strip()
                        recording.summary = generated_summary.strip()
                        recording.status = 'COMPLETED'
                        app.logger.info(f"Title and summary generated successfully for recording {recording_id}.")
                    else:
                        app.logger.warning(f"OpenRouter response for {recording_id} lacked 'title' or 'summary' key. Response: {response_content}")
                        recording.summary = "[AI summary generation failed: Invalid JSON structure]"
                        recording.status = 'COMPLETED' # Still completed, but summary failed

                except json.JSONDecodeError as json_e:
                    app.logger.error(f"Failed to parse JSON response from OpenRouter for {recording_id}: {json_e}. Response: {response_content}")
                    recording.summary = f"[AI summary generation failed: Invalid JSON response ({json_e})]"
                    recording.status = 'COMPLETED' # Mark as completed, summary failed

            except Exception as summary_e:
                app.logger.error(f"Error calling OpenRouter API for summary ({recording_id}): {str(summary_e)}")
                # Keep transcription, but mark summary failed. Don't change status from SUMMARIZING yet.
                recording.summary = f"[AI summary generation failed: API Error ({str(summary_e)})]"
                recording.status = 'COMPLETED' # Even if summary fails, transcription worked.


            db.session.commit() # Final commit for this step

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
        system_prompt = f"""You are a professional meeting analyst working with Murtaza Nasir, Assistant Professor at Wichita State University. Analyze the following meeting information and respond to the specific request.

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
                max_tokens=1000
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

@app.route('/account', methods=['GET'])
@login_required
def account():
    return render_template('account.html', title='Account')

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
    return render_template('index.html')

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

        original_filename = file.filename # <-- ADDED: Capture original filename
        safe_filename = secure_filename(original_filename)
        # Ensure filepath uses the configured UPLOAD_FOLDER
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_filename}")

        # Get file size before saving
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        # Check size limit again
        if file_size > app.config['MAX_CONTENT_LENGTH']:
            raise RequestEntityTooLarge()

        file.save(filepath)
        app.logger.info(f"File saved to {filepath}")

        # Create initial database entry with PENDING status and filename as placeholder title
        recording = Recording(
            audio_path=filepath,
            original_filename=original_filename, # <-- ADDED: Save original filename
            # Use original filename (without path part) as initial title
            title=f"Recording - {original_filename}",
            file_size=file_size,
            status='PENDING', # Explicitly set status
            meeting_date=datetime.utcnow().date(), # <-- ADDED: Default meeting_date to today
            user_id=current_user.id # Associate with the current user
        )
        db.session.add(recording)
        db.session.commit()
        app.logger.info(f"Initial recording record created with ID: {recording.id}")

        # --- Start transcription & summarization in background thread ---
        thread = threading.Thread(
            target=transcribe_audio_task,
            # Pass original filename for logging clarity
            args=(app.app_context(), recording.id, filepath, original_filename) # Pass original_filename here too
        )
        thread.start()
        app.logger.info(f"Background processing thread started for recording ID: {recording.id}")

        # Return the initial recording data and ID immediately
        return jsonify(recording.to_dict()), 202 # 202 Accepted

    except RequestEntityTooLarge:
        max_size_mb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024)
        app.logger.warning(f"Upload failed: File too large (>{max_size_mb}MB)")
        return jsonify({
            'error': f'File too large. Maximum size is {max_size_mb:.0f} MB.',
            'max_size_mb': max_size_mb
        }), 413
    except Exception as e:
        db.session.rollback() # Rollback if initial save failed
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
