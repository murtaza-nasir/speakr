# Project Files

Generated documentation of all project files.


## reset_db.py

```python

#!/usr/bin/env python3

# Add this near the top if you run this standalone often outside app context
import os
import sys
import shutil
# Add project root to path if necessary for 'app' import
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load environment variables in case DB path relies on them (optional here)
# from dotenv import load_dotenv
# load_dotenv()

# Check if running within app context already (e.g., via Flask command)
try:
    from flask import current_app
    # Ensure app context is pushed if needed for config access
    app = current_app._get_current_object()
    # Make sure db is initialized within the app context if needed
    # (SQLAlchemy initialization in app.py handles this mostly)
    with app.app_context():
      db = app.extensions['sqlalchemy'].db # Access db via extensions
except (RuntimeError, AttributeError, KeyError):
     # If not in app context, import directly
    try:
        # Ensure this import reflects the updated app.py with the new model
        from app import app, db
    except ImportError as e:
        print(f"Error: Could not import 'app' and 'db': {e}")
        print("Make sure reset_db.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def reset_database(delete_uploads=True):
    # Determine the database path relative to the instance folder
    # Use app config if available
    instance_path = app.instance_path if hasattr(app, 'instance_path') else os.path.join(os.getcwd(), 'instance')
    try:
        # Ensure app context for config access if not already present
        with app.app_context():
             # Use absolute path from config
             db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///instance/transcriptions.db')
             # Handle relative vs absolute paths specified in URI
             if db_uri.startswith('sqlite:///'):
                 # Assume absolute path from URI root if starts with '///'
                 db_path = db_uri.replace('sqlite:///', '/', 1) # Replace only first
                 # Ensure instance path reflects the directory containing the DB
                 instance_path = os.path.dirname(db_path)
             elif db_uri.startswith('sqlite://'):
                 # Assume relative path from instance folder
                 db_filename = db_uri.split('/')[-1]
                 db_path = os.path.join(instance_path, db_filename)
             else: # Handle other DB types or formats if needed
                 print(f"Warning: Non-SQLite URI detected: {db_uri}. Deletion logic might need adjustment.")
                 # Attempt to parse or fallback
                 db_filename = db_uri.split('/')[-1] # Best guess
                 db_path = os.path.join(instance_path, db_filename)

    except Exception as config_e:
        print(f"Error accessing app config for DB path: {config_e}. Using default.")
        # Fallback if config access fails
        instance_path = os.path.join(os.getcwd(), 'instance')
        db_filename = 'transcriptions.db'
        db_path = os.path.join(instance_path, db_filename)

    # Ensure instance directory exists
    print(f"Ensuring instance directory exists: {instance_path}")
    os.makedirs(instance_path, exist_ok=True)
    print(f"Database path identified as: {db_path}")

    # Remove existing database if it exists
    if os.path.exists(db_path):
        print(f"Removing existing database at {db_path}")
        try:
            os.remove(db_path)
            # Also remove journal file if it exists
            journal_path = db_path + "-journal"
            if os.path.exists(journal_path):
                os.remove(journal_path)
                print(f"Removing existing journal file at {journal_path}")
        except OSError as e:
            print(f"Error removing database file: {e}. Check permissions or if it's in use.")
            # Decide whether to exit or continue
            # sys.exit(1)

    # Create application context to work with the database
    try:
        with app.app_context():
            print("Creating new database schema (including 'summary' column)...")
            # Create all tables defined in models (app.py)
            db.create_all()
            print("Database schema created successfully!")
    except Exception as e:
        print(f"Error creating database schema: {e}")
        # Attempt rollback if possible (though less relevant for create_all)
        try:
             db.session.rollback()
        except Exception as rb_e:
             print(f"Rollback attempt failed: {rb_e}")
        sys.exit(1)
        
    # Delete all files in the uploads directory if requested
    if delete_uploads:
        try:
            uploads_dir = os.path.join(os.getcwd(), 'uploads')
            if os.path.exists(uploads_dir):
                print(f"Deleting all files in uploads directory: {uploads_dir}")
                for filename in os.listdir(uploads_dir):
                    file_path = os.path.join(uploads_dir, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            print(f"Deleted file: {file_path}")
                        elif os.path.isdir(file_path):
                            shutil.rmtree(file_path)
                            print(f"Deleted directory: {file_path}")
                    except Exception as e:
                        print(f"Error deleting {file_path}: {e}")
                print("All files in uploads directory have been deleted.")
            else:
                print(f"Uploads directory not found: {uploads_dir}")
                # Create the directory if it doesn't exist
                os.makedirs(uploads_dir, exist_ok=True)
                print(f"Created uploads directory: {uploads_dir}")
        except Exception as e:
            print(f"Error cleaning uploads directory: {e}")

if __name__ == "__main__":
    print("Attempting to reset the database and clean up all data...")
    reset_database(delete_uploads=True)
    print("Database reset process finished.")

```


## create_admin.py

```python

#!/usr/bin/env python3

import os
import sys
import getpass
from email_validator import validate_email, EmailNotValidError

# Try to import from app context
try:
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        db = app.extensions['sqlalchemy'].db
        User = app.extensions['sqlalchemy'].db.metadata.tables['user']
        bcrypt = app.extensions.get('bcrypt')
except (RuntimeError, AttributeError, KeyError):
    # If not in app context, import directly
    try:
        from app import app, db, User, bcrypt
    except ImportError as e:
        print(f"Error: Could not import required modules: {e}")
        print("Make sure create_admin.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def create_admin_user():
    """
    Create an admin user interactively.
    """
    print("Creating admin user for Speakr application")
    print("=========================================")
    
    # Get username
    while True:
        username = input("Enter username (min 3 characters): ").strip()
        if len(username) < 3:
            print("Username must be at least 3 characters long.")
            continue
        
        # Check if username already exists
        with app.app_context():
            existing_user = db.session.query(User).filter_by(username=username).first()
            if existing_user:
                print(f"Username '{username}' already exists. Please choose another.")
                continue
            break
    
    # Get email
    while True:
        email = input("Enter email address: ").strip()
        try:
            # Validate email
            validate_email(email)
            
            # Check if email already exists
            with app.app_context():
                existing_email = db.session.query(User).filter_by(email=email).first()
                if existing_email:
                    print(f"Email '{email}' already exists. Please use another.")
                    continue
                break
        except EmailNotValidError as e:
            print(f"Invalid email: {str(e)}")
    
    # Get password
    while True:
        password = getpass.getpass("Enter password (min 8 characters): ")
        if len(password) < 8:
            print("Password must be at least 8 characters long.")
            continue
        
        confirm_password = getpass.getpass("Confirm password: ")
        if password != confirm_password:
            print("Passwords do not match. Please try again.")
            continue
        break
    
    # Create user
    with app.app_context():
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        new_user = User(username=username, email=email, password=hashed_password, is_admin=True)
        db.session.add(new_user)
        db.session.commit()
        
        print("\nAdmin user created successfully!")
        print(f"Username: {username}")
        print(f"Email: {email}")
        print("You can now log in to the application with these credentials.")

if __name__ == "__main__":
    create_admin_user()

```


## app.py

```python

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
            'user_id': self.user_id
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

with app.app_context():
    db.create_all()

# --- API client setup for OpenRouter ---
# Use environment variables from .env
openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")
openrouter_base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
openrouter_model_name = os.environ.get("OPENROUTER_MODEL_NAME", "openai/gpt-3.5-turbo") # Default if not set

http_client_no_proxy = httpx.Client(verify=True) # verify=True is default, but good to be explicit

if not openrouter_api_key:
    app.logger.warning("OPENROUTER_API_KEY not found. Title/Summary generation DISABLED.")
else:
    try:
        # ---> Pass the custom httpx_client <---
        client = OpenAI(
            api_key=openrouter_api_key,
            base_url=openrouter_base_url,
            http_client=http_client_no_proxy # Pass the proxy-disabled client
        )
        app.logger.info(f"OpenRouter client initialized. Using model: {openrouter_model_name}")
    except Exception as client_init_e:
         app.logger.error(f"Failed to initialize OpenRouter client: {client_init_e}", exc_info=True)

# Store details for the transcription client (potentially different)
transcription_api_key = os.environ.get("TRANSCRIPTION_API_KEY", "cant-be-empty")
transcription_base_url = os.environ.get("TRANSCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")

app.logger.info(f"Using OpenRouter model for summaries: {openrouter_model_name}")

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
            app.logger.info(f"Requesting title and summary from OpenRouter for recording {recording_id} using model {openrouter_model_name}...")

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
                    model=openrouter_model_name,
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
                model=openrouter_model_name,
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

```


## create_docs.py

```python

import os
from pathlib import Path

def create_markdown_doc(base_dir):
    output = []
    
    # Add header
    output.append("# Project Files\n")
    output.append("Generated documentation of all project files.\n")

    # Function to read and format file content
    def add_file_content(filepath, relative_path):
        output.append(f"\n## {relative_path}\n")
        output.append("```" + get_file_extension(filepath) + "\n")
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                output.append(f.read())
        except Exception as e:
            output.append(f"Error reading file: {e}")
        output.append("```\n")

    def get_file_extension(filepath):
        ext = os.path.splitext(filepath)[1][1:].lower()
        # Map file extensions to markdown code block languages
        extension_map = {
            'py': 'python',
            'html': 'html',
            'js': 'javascript',
            'css': 'css',
            'sh': 'bash',
            'md': 'markdown',
            'txt': 'text'
        }
        return extension_map.get(ext, '')

    # List of important file patterns to include
    patterns = [
        '*.py',
        '*.html',
        '*.js',
        '*.css',
        '*.sh',
        'requirements.txt'
    ]

    # Walk through directory and add files
    for root, _, _ in os.walk(base_dir):
        for pattern in patterns:
            for filepath in Path(root).glob(pattern):
                if 'venv' not in str(filepath) and '__pycache__' not in str(filepath):
                    relative_path = os.path.relpath(filepath, base_dir)
                    add_file_content(filepath, relative_path)

    # Write to output file
    output_path = os.path.join(base_dir, 'project_files.md')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))
    
    return output_path

if __name__ == "__main__":
    # Get the current directory
    current_dir = os.getcwd()
    
    # Create the markdown file
    output_file = create_markdown_doc(current_dir)
    print(f"Created markdown documentation at: {output_file}")
```


## requirements.txt

```text

flask==2.3.3
flask-sqlalchemy==3.1.1
flask-login==0.6.3
flask-wtf==1.2.2
flask-bcrypt==1.0.1
email-validator==2.2.0
openai==1.3.0
werkzeug==2.3.7
gunicorn==21.2.0
python-dotenv==1.0.0
markdown==3.5.1

```


## .migrate/migrate_is_admin.py

```python

#!/usr/bin/env python3

import os
import sys
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Boolean

# Try to import from app context
try:
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        db = app.extensions['sqlalchemy'].db
        User = app.extensions['sqlalchemy'].db.metadata.tables['user']
except (RuntimeError, AttributeError, KeyError):
    # If not in app context, import directly
    try:
        from app import app, db, User
    except ImportError as e:
        print(f"Error: Could not import required modules: {e}")
        print("Make sure migrate_is_admin.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def migrate_is_admin_field():
    """
    Add is_admin field to User model if it doesn't exist
    """
    print("Migrating database to add is_admin field to User model")
    print("=====================================================")
    
    with app.app_context():
        # Check if is_admin column exists
        inspector = db.inspect(db.engine)
        columns = [column['name'] for column in inspector.get_columns('user')]
        
        if 'is_admin' in columns:
            print("is_admin column already exists in User table.")
            return
        
        # Add is_admin column
        print("Adding is_admin column to User table...")
        try:
            db.engine.execute('ALTER TABLE user ADD COLUMN is_admin BOOLEAN DEFAULT 0')
            print("Column added successfully.")
        except Exception as e:
            print(f"Error adding column: {e}")
            sys.exit(1)
        
        print("\nMigration completed successfully!")
        print("You can now use the create_admin.py script to create admin users.")

if __name__ == "__main__":
    migrate_is_admin_field()

```


## .migrate/migrate_original_filename.py

```python

#!/usr/bin/env python3

import os
import sys
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

# Try to import from app context
try:
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        db = app.extensions['sqlalchemy'].db
except (RuntimeError, AttributeError, KeyError):
    # If not in app context, import directly
    try:
        from app import app, db, Recording
    except ImportError as e:
        print(f"Error: Could not import 'app' and 'db': {e}")
        print("Make sure migrate_original_filename.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def migrate_database():
    """
    Migrate the database schema to add original_filename column without losing data
    """
    print("Starting database migration for original_filename column...")
    
    with app.app_context():
        inspector = inspect(db.engine)
        
        # Check if Recording.original_filename column exists
        recording_columns = [column['name'] for column in inspector.get_columns('recording')]
        if 'original_filename' not in recording_columns:
            print("Adding original_filename column to Recording table...")
            try:
                # Add original_filename column to Recording table
                with db.engine.begin() as conn:
                    conn.execute(db.text("ALTER TABLE recording ADD COLUMN original_filename VARCHAR(500)"))
                print("original_filename column added successfully!")
            except OperationalError as e:
                print(f"Error adding original_filename column: {e}")
                print("Attempting alternative approach...")
                try:
                    # SQLite doesn't support ALTER TABLE ADD COLUMN with foreign keys
                    # So we need to create a new table, copy data, drop old table, rename new table
                    
                    # 1. Create temporary table with new schema
                    with db.engine.begin() as conn:
                        conn.execute(db.text("""
                            CREATE TABLE recording_new (
                                id INTEGER PRIMARY KEY,
                                title VARCHAR(200),
                                participants VARCHAR(500),
                                notes TEXT,
                                transcription TEXT,
                                summary TEXT,
                                status VARCHAR(50),
                                audio_path VARCHAR(500),
                                created_at DATETIME,
                                meeting_date DATE,
                                file_size INTEGER,
                                user_id INTEGER REFERENCES user(id),
                                original_filename VARCHAR(500)
                            )
                        """))
                    
                    # 2. Copy data from old table to new table
                    with db.engine.begin() as conn:
                        conn.execute(db.text("""
                            INSERT INTO recording_new (
                                id, title, participants, notes, transcription, summary, 
                                status, audio_path, created_at, meeting_date, file_size, user_id
                            )
                            SELECT 
                                id, title, participants, notes, transcription, summary, 
                                status, audio_path, created_at, meeting_date, file_size, user_id
                            FROM recording
                        """))
                    
                    # 3. Drop old table
                    with db.engine.begin() as conn:
                        conn.execute(db.text("DROP TABLE recording"))
                    
                    # 4. Rename new table
                    with db.engine.begin() as conn:
                        conn.execute(db.text("ALTER TABLE recording_new RENAME TO recording"))
                    
                    print("original_filename column added successfully using table recreation!")
                except Exception as e2:
                    print(f"Error during table recreation: {e2}")
                    sys.exit(1)
        else:
            print("original_filename column already exists in Recording table.")
        
        print("Database migration completed successfully!")

if __name__ == "__main__":
    print("Attempting to migrate the database to add original_filename column...")
    migrate_database()
    print("Database migration process finished.")

```


## .migrate/migrate_db.py

```python

#!/usr/bin/env python3

import os
import sys
from sqlalchemy import inspect, Column, Integer, ForeignKey
from sqlalchemy.exc import OperationalError

# Try to import from app context
try:
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        db = app.extensions['sqlalchemy'].db
except (RuntimeError, AttributeError, KeyError):
    # If not in app context, import directly
    try:
        from app import app, db, User, Recording
    except ImportError as e:
        print(f"Error: Could not import 'app' and 'db': {e}")
        print("Make sure migrate_db.py is runnable and PYTHONPATH is set.")
        sys.exit(1)

def migrate_database():
    """
    Migrate the database schema without losing data:
    1. Check if User table exists, create it if not
    2. Check if Recording.user_id column exists, add it if not
    """
    print("Starting database migration...")
    
    with app.app_context():
        inspector = inspect(db.engine)
        existing_tables = inspector.get_table_names()
        
        # Check if User table exists
        if 'user' not in existing_tables:
            print("Creating User table...")
            # Create User table
            class TempUser(db.Model):
                __tablename__ = 'user'
                id = db.Column(db.Integer, primary_key=True)
                username = db.Column(db.String(20), unique=True, nullable=False)
                email = db.Column(db.String(120), unique=True, nullable=False)
                password = db.Column(db.String(60), nullable=False)
            
            # Create only the User table
            db.create_all(tables=[TempUser.__table__])
            print("User table created successfully!")
        else:
            print("User table already exists.")
        
        # Check if Recording.user_id column exists
        recording_columns = [column['name'] for column in inspector.get_columns('recording')]
        if 'user_id' not in recording_columns:
            print("Adding user_id column to Recording table...")
            try:
                # Add user_id column to Recording table
                with db.engine.begin() as conn:
                    conn.execute(db.text("ALTER TABLE recording ADD COLUMN user_id INTEGER REFERENCES user(id)"))
                print("user_id column added successfully!")
            except OperationalError as e:
                print(f"Error adding user_id column: {e}")
                print("Attempting alternative approach...")
                try:
                    # SQLite doesn't support ALTER TABLE ADD COLUMN with foreign keys
                    # So we need to create a new table, copy data, drop old table, rename new table
                    
                    # 1. Create temporary table with new schema
                    with db.engine.begin() as conn:
                        conn.execute(db.text("""
                            CREATE TABLE recording_new (
                                id INTEGER PRIMARY KEY,
                                title VARCHAR(200),
                                participants VARCHAR(500),
                                notes TEXT,
                                transcription TEXT,
                                summary TEXT,
                                status VARCHAR(50),
                                audio_path VARCHAR(500),
                                created_at DATETIME,
                                meeting_date DATE,
                                file_size INTEGER,
                                user_id INTEGER REFERENCES user(id)
                            )
                        """))
                    
                    # 2. Copy data from old table to new table
                    with db.engine.begin() as conn:
                        conn.execute(db.text("""
                            INSERT INTO recording_new (
                                id, title, participants, notes, transcription, summary, 
                                status, audio_path, created_at, meeting_date, file_size
                            )
                            SELECT 
                                id, title, participants, notes, transcription, summary, 
                                status, audio_path, created_at, meeting_date, file_size
                            FROM recording
                        """))
                    
                    # 3. Drop old table
                    with db.engine.begin() as conn:
                        conn.execute(db.text("DROP TABLE recording"))
                    
                    # 4. Rename new table
                    with db.engine.begin() as conn:
                        conn.execute(db.text("ALTER TABLE recording_new RENAME TO recording"))
                    
                    print("user_id column added successfully using table recreation!")
                except Exception as e2:
                    print(f"Error during table recreation: {e2}")
                    sys.exit(1)
        else:
            print("user_id column already exists in Recording table.")
        
        print("Database migration completed successfully!")

if __name__ == "__main__":
    print("Attempting to migrate the database...")
    migrate_database()
    print("Database migration process finished.")

```


## templates/account.html

```html

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Speakr</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        /* Dark Mode CSS Variables */
        :root {
            /* Light mode variables */
            --bg-primary: #f3f4f6; /* gray-100 */
            --bg-secondary: #ffffff; /* white */
            --bg-tertiary: #f9fafb; /* gray-50 */
            --bg-accent: #dbeafe; /* blue-100 */
            --bg-accent-hover: #bfdbfe; /* blue-200 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #1d4ed8; /* blue-700 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #b91c1c; /* red-700 */
            --bg-danger-light: #fee2e2; /* red-100 */
            --bg-info-light: #dbeafe; /* blue-100 */
            --bg-warn-light: #fef3c7; /* amber-100 */
            --bg-success-light: #d1fae5; /* green-100 */
            --bg-pending-light: #f5f5f4; /* stone-100 */
            --bg-input: #ffffff; /* white */

            --text-primary: #1f2937; /* gray-800 */
            --text-secondary: #374151; /* gray-700 */
            --text-muted: #6b7280; /* gray-500 */
            --text-light: #9ca3af; /* gray-400 */
            --text-accent: #1d4ed8; /* blue-700 */
            --text-button: #ffffff; /* white */
            --text-danger: #b91c1c; /* red-700 */
            --text-danger-strong: #991b1b; /* red-800 */
            --text-info-strong: #1e40af; /* blue-800 */
            --text-warn-strong: #92400e; /* amber-800 */
            --text-success-strong: #065f46; /* green-800 */
            --text-pending-strong: #44403c; /* stone-700 */

            --border-primary: #e5e7eb; /* gray-200 */
            --border-secondary: #d1d5db; /* gray-300 */
            --border-accent: #93c5fd; /* blue-300 */
            --border-danger: #f87171; /* red-400 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #bfdbfe; /* blue-200 */
        }

        .dark {
            /* Dark mode variables */
            --bg-primary: #111827; /* gray-900 */
            --bg-secondary: #1f2937; /* gray-800 */
            --bg-tertiary: #374151; /* gray-700 */
            --bg-accent: #1e3a8a; /* blue-900 */
            --bg-accent-hover: #1e40af; /* blue-800 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #3b82f6; /* blue-500 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #ef4444; /* red-500 */
            --bg-danger-light: #7f1d1d; /* red-900 */
            --bg-info-light: #1e3a8a; /* blue-900 */
            --bg-warn-light: #78350f; /* amber-900 */
            --bg-success-light: #064e3b; /* green-900 */
            --bg-pending-light: #292524; /* stone-800 */
            --bg-input: #374151; /* gray-700 */

            --text-primary: #f3f4f6; /* gray-100 */
            --text-secondary: #d1d5db; /* gray-300 */
            --text-muted: #9ca3af; /* gray-400 */
            --text-light: #6b7280; /* gray-500 */
            --text-accent: #60a5fa; /* blue-400 */
            --text-button: #ffffff; /* white */
            --text-danger: #f87171; /* red-400 */
            --text-danger-strong: #fca5a5; /* red-300 */
            --text-info-strong: #93c5fd; /* blue-300 */
            --text-warn-strong: #fcd34d; /* amber-300 */
            --text-success-strong: #6ee7b7; /* green-300 */
            --text-pending-strong: #d6d3d1; /* stone-300 */

            --border-primary: #374151; /* gray-700 */
            --border-secondary: #4b5563; /* gray-600 */
            --border-accent: #1d4ed8; /* blue-700 */
            --border-danger: #ef4444; /* red-500 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #1e40af; /* blue-800 */
        }

        html {
            background-color: var(--bg-primary); 
            color: var(--text-primary); 
            transition: background-color 0.3s, color 0.3s; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        html, body { height: 100%; margin: 0; }
    </style>
</head>
<body class="bg-[var(--bg-primary)] text-[var(--text-primary)]">
    <div class="container mx-auto px-4 sm:px-6 lg:px-8 py-6 flex flex-col min-h-screen">
        <header class="flex justify-between items-center mb-6 pb-4 border-b border-[var(--border-primary)]">
            <h1 class="text-3xl font-bold text-[var(--text-primary)]">
                <a href="{{ url_for('index') }}">Speakr</a>
            </h1>
            <div class="flex items-center space-x-2">
                <button id="darkModeToggle" class="p-2 rounded-full text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] dark:text-gray-400 dark:hover:bg-gray-700 transition-colors duration-200">
                    <i id="darkModeIcon" class="fas fa-moon"></i>
                </button>
                <div class="relative" id="userDropdown">
                    <button id="userDropdownButton" class="flex items-center px-3 py-2 border border-[var(--border-secondary)] rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-accent)] focus:outline-none">
                        <i class="fas fa-user mr-2"></i>
                        <span>{{ current_user.username }}</span>
                        <i class="fas fa-chevron-down ml-2"></i>
                    </button>
                    <div id="userDropdownMenu" class="hidden absolute right-0 mt-2 w-48 bg-[var(--bg-secondary)] border border-[var(--border-primary)] rounded-lg shadow-lg z-10">
                        <a href="{{ url_for('index') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)]">
                            <i class="fas fa-home mr-2"></i> Home
                        </a>
                        <a href="{{ url_for('account') }}" class="block px-4 py-2 text-[var(--text-accent)] bg-[var(--bg-accent)]">
                            <i class="fas fa-user-circle mr-2"></i> Account
                        </a>
                        {% if current_user.is_admin %}
                        <a href="{{ url_for('admin') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)]">
                            <i class="fas fa-user-shield mr-2"></i> Admin
                        </a>
                        {% endif %}
                        <a href="{{ url_for('logout') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-danger)]">
                            <i class="fas fa-sign-out-alt mr-2"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </header>

        <main class="flex-grow">
            <div class="bg-[var(--bg-secondary)] p-8 rounded-xl shadow-lg border border-[var(--border-primary)]">
                <h2 class="text-2xl font-semibold text-[var(--text-primary)] mb-6">Account Information</h2>
                
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="mb-4 p-3 rounded-lg {% if category == 'success' %}bg-[var(--bg-success-light)] text-[var(--text-success-strong)]{% elif category == 'danger' %}bg-[var(--bg-danger-light)] text-[var(--text-danger-strong)]{% else %}bg-[var(--bg-info-light)] text-[var(--text-info-strong)]{% endif %}">
                                {{ message }}
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
                    <div>
                        <div class="mb-6">
                            <h3 class="text-lg font-medium text-[var(--text-secondary)] mb-2">User Details</h3>
                            <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)]">
                                <div class="mb-3">
                                    <span class="block text-sm font-medium text-[var(--text-muted)]">Username</span>
                                    <span class="block text-[var(--text-primary)]">{{ current_user.username }}</span>
                                </div>
                                <div>
                                    <span class="block text-sm font-medium text-[var(--text-muted)]">Email</span>
                                    <span class="block text-[var(--text-primary)]">{{ current_user.email }}</span>
                                </div>
                            </div>
                        </div>
                        
                        <div>
                            <h3 class="text-lg font-medium text-[var(--text-secondary)] mb-2">Account Actions</h3>
                            <div class="flex flex-col space-y-3">
                                <a href="{{ url_for('index') }}" class="inline-flex items-center px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[var(--border-focus)] transition-colors duration-200">
                                    <i class="fas fa-microphone mr-2"></i> Go to Recordings
                                </a>
                                <button id="changePasswordBtn" class="inline-flex items-center px-4 py-2 border border-[var(--border-secondary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)] focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[var(--border-focus)] transition-colors duration-200">
                                    <i class="fas fa-key mr-2"></i> Change Password
                                </button>
                            </div>
                        </div>
                    </div>
                    
                    <div>
                        <h3 class="text-lg font-medium text-[var(--text-secondary)] mb-2">Account Statistics</h3>
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)]">
                            <div class="grid grid-cols-2 gap-4">
                                <div class="text-center p-4 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-3xl font-bold text-[var(--text-accent)]">{{ current_user.recordings|length }}</span>
                                    <span class="block text-sm text-[var(--text-muted)]">Total Recordings</span>
                                </div>
                                
                                <div class="text-center p-4 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-3xl font-bold text-[var(--text-accent)]">
                                        {% set completed_count = current_user.recordings|selectattr('status', 'equalto', 'COMPLETED')|list|length %}
                                        {{ completed_count }}
                                    </span>
                                    <span class="block text-sm text-[var(--text-muted)]">Completed</span>
                                </div>
                                
                                <div class="text-center p-4 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-3xl font-bold text-[var(--text-warn-strong)]">
                                        {% set processing_count = current_user.recordings|selectattr('status', 'in', ['PENDING', 'PROCESSING', 'SUMMARIZING'])|list|length %}
                                        {{ processing_count }}
                                    </span>
                                    <span class="block text-sm text-[var(--text-muted)]">Processing</span>
                                </div>
                                
                                <div class="text-center p-4 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-3xl font-bold text-[var(--text-danger)]">
                                        {% set failed_count = current_user.recordings|selectattr('status', 'equalto', 'FAILED')|list|length %}
                                        {{ failed_count }}
                                    </span>
                                    <span class="block text-sm text-[var(--text-muted)]">Failed</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </main>

        <footer class="text-center py-4 mt-8 text-xs text-[var(--text-light)] border-t border-[var(--border-primary)]">
            Speakr &copy; {{ now.year }}
        </footer>
    </div>

    <!-- Change Password Modal -->
    <div id="changePasswordModal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 hidden">
        <div class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-xl w-full max-w-md">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xl font-semibold text-[var(--text-primary)]">Change Password</h3>
                <button id="closeModalBtn" class="text-[var(--text-light)] hover:text-[var(--text-muted)] text-2xl leading-none">&times;</button>
            </div>
            <form id="changePasswordForm" method="POST" action="{{ url_for('change_password') }}">
                <div class="mb-4">
                    <label for="current_password" class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Current Password</label>
                    <input type="password" id="current_password" name="current_password" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                </div>
                <div class="mb-4">
                    <label for="new_password" class="block text-sm font-medium text-[var(--text-secondary)] mb-1">New Password</label>
                    <input type="password" id="new_password" name="new_password" required minlength="8" class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    <p class="text-xs text-[var(--text-muted)] mt-1">Password must be at least 8 characters long</p>
                </div>
                <div class="mb-6">
                    <label for="confirm_password" class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Confirm New Password</label>
                    <input type="password" id="confirm_password" name="confirm_password" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                </div>
                <div class="flex justify-end space-x-3">
                    <button type="button" id="cancelBtn" class="px-4 py-2 bg-[var(--bg-tertiary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--border-secondary)]">Cancel</button>
                    <button type="submit" class="px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)]">Change Password</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        // Dark mode toggle functionality
        const darkModeToggle = document.getElementById('darkModeToggle');
        const darkModeIcon = document.getElementById('darkModeIcon');
        
        // Initialize dark mode based on localStorage or system preference
        function initializeDarkMode() {
            const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
            const savedMode = localStorage.getItem('darkMode');
            
            if (savedMode === 'true' || (savedMode === null && prefersDark)) {
                document.documentElement.classList.add('dark');
                darkModeIcon.classList.remove('fa-moon');
                darkModeIcon.classList.add('fa-sun');
            } else {
                document.documentElement.classList.remove('dark');
                darkModeIcon.classList.remove('fa-sun');
                darkModeIcon.classList.add('fa-moon');
            }
        }
        
        // Toggle dark mode
        darkModeToggle.addEventListener('click', () => {
            const isDarkMode = document.documentElement.classList.toggle('dark');
            localStorage.setItem('darkMode', isDarkMode);
            
            if (isDarkMode) {
                darkModeIcon.classList.remove('fa-moon');
                darkModeIcon.classList.add('fa-sun');
            } else {
                darkModeIcon.classList.remove('fa-sun');
                darkModeIcon.classList.add('fa-moon');
            }
        });
        
        // User dropdown functionality
        const userDropdownButton = document.getElementById('userDropdownButton');
        const userDropdownMenu = document.getElementById('userDropdownMenu');
        
        userDropdownButton.addEventListener('click', () => {
            userDropdownMenu.classList.toggle('hidden');
        });
        
        // Close dropdown when clicking outside
        document.addEventListener('click', (event) => {
            if (!userDropdownButton.contains(event.target) && !userDropdownMenu.contains(event.target)) {
                userDropdownMenu.classList.add('hidden');
            }
        });
        
        // Initialize dark mode on page load
        initializeDarkMode();
        
        // Change Password Modal functionality
        const changePasswordBtn = document.getElementById('changePasswordBtn');
        const changePasswordModal = document.getElementById('changePasswordModal');
        const closeModalBtn = document.getElementById('closeModalBtn');
        const cancelBtn = document.getElementById('cancelBtn');
        const changePasswordForm = document.getElementById('changePasswordForm');
        const newPasswordInput = document.getElementById('new_password');
        const confirmPasswordInput = document.getElementById('confirm_password');
        
        // Open modal
        changePasswordBtn.addEventListener('click', () => {
            changePasswordModal.classList.remove('hidden');
            // Reset form
            changePasswordForm.reset();
        });
        
        // Close modal functions
        function closeModal() {
            changePasswordModal.classList.add('hidden');
        }
        
        closeModalBtn.addEventListener('click', closeModal);
        cancelBtn.addEventListener('click', closeModal);
        
        // Close modal when clicking outside
        changePasswordModal.addEventListener('click', (event) => {
            if (event.target === changePasswordModal) {
                closeModal();
            }
        });
        
        // Form validation
        changePasswordForm.addEventListener('submit', (event) => {
            if (newPasswordInput.value !== confirmPasswordInput.value) {
                event.preventDefault();
                alert('New password and confirmation do not match.');
            }
        });
    </script>
</body>
</html>

```


## templates/admin.html

```html

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Dashboard - Speakr</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        /* Dark Mode CSS Variables */
        :root {
            /* Light mode variables */
            --bg-primary: #f3f4f6; /* gray-100 */
            --bg-secondary: #ffffff; /* white */
            --bg-tertiary: #f9fafb; /* gray-50 */
            --bg-accent: #dbeafe; /* blue-100 */
            --bg-accent-hover: #bfdbfe; /* blue-200 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #1d4ed8; /* blue-700 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #b91c1c; /* red-700 */
            --bg-danger-light: #fee2e2; /* red-100 */
            --bg-info-light: #dbeafe; /* blue-100 */
            --bg-warn-light: #fef3c7; /* amber-100 */
            --bg-success-light: #d1fae5; /* green-100 */
            --bg-pending-light: #f5f5f4; /* stone-100 */
            --bg-input: #ffffff; /* white */

            --text-primary: #1f2937; /* gray-800 */
            --text-secondary: #374151; /* gray-700 */
            --text-muted: #6b7280; /* gray-500 */
            --text-light: #9ca3af; /* gray-400 */
            --text-accent: #1d4ed8; /* blue-700 */
            --text-button: #ffffff; /* white */
            --text-danger: #b91c1c; /* red-700 */
            --text-danger-strong: #991b1b; /* red-800 */
            --text-info-strong: #1e40af; /* blue-800 */
            --text-warn-strong: #92400e; /* amber-800 */
            --text-success-strong: #065f46; /* green-800 */
            --text-pending-strong: #44403c; /* stone-700 */

            --border-primary: #e5e7eb; /* gray-200 */
            --border-secondary: #d1d5db; /* gray-300 */
            --border-accent: #93c5fd; /* blue-300 */
            --border-danger: #f87171; /* red-400 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #bfdbfe; /* blue-200 */
        }

        .dark {
            /* Dark mode variables */
            --bg-primary: #111827; /* gray-900 */
            --bg-secondary: #1f2937; /* gray-800 */
            --bg-tertiary: #374151; /* gray-700 */
            --bg-accent: #1e3a8a; /* blue-900 */
            --bg-accent-hover: #1e40af; /* blue-800 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #3b82f6; /* blue-500 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #ef4444; /* red-500 */
            --bg-danger-light: #7f1d1d; /* red-900 */
            --bg-info-light: #1e3a8a; /* blue-900 */
            --bg-warn-light: #78350f; /* amber-900 */
            --bg-success-light: #064e3b; /* green-900 */
            --bg-pending-light: #292524; /* stone-800 */
            --bg-input: #374151; /* gray-700 */

            --text-primary: #f3f4f6; /* gray-100 */
            --text-secondary: #d1d5db; /* gray-300 */
            --text-muted: #9ca3af; /* gray-400 */
            --text-light: #6b7280; /* gray-500 */
            --text-accent: #60a5fa; /* blue-400 */
            --text-button: #ffffff; /* white */
            --text-danger: #f87171; /* red-400 */
            --text-danger-strong: #fca5a5; /* red-300 */
            --text-info-strong: #93c5fd; /* blue-300 */
            --text-warn-strong: #fcd34d; /* amber-300 */
            --text-success-strong: #6ee7b7; /* green-300 */
            --text-pending-strong: #d6d3d1; /* stone-300 */

            --border-primary: #374151; /* gray-700 */
            --border-secondary: #4b5563; /* gray-600 */
            --border-accent: #1d4ed8; /* blue-700 */
            --border-danger: #ef4444; /* red-500 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #1e40af; /* blue-800 */
        }

        html {
            background-color: var(--bg-primary); 
            color: var(--text-primary); 
            transition: background-color 0.3s, color 0.3s; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        html, body { height: 100%; margin: 0; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: var(--scrollbar-track); border-radius: 12px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 12px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }
    </style>
</head>
<body class="bg-[var(--bg-primary)] text-[var(--text-primary)]">
    <div id="app" class="container mx-auto px-4 sm:px-6 lg:px-8 py-6 flex flex-col min-h-screen">
        <header class="flex justify-between items-center mb-6 pb-4 border-b border-[var(--border-primary)]">
            <h1 class="text-3xl font-bold text-[var(--text-primary)]">
                <a href="{{ url_for('index') }}">Speakr</a>
            </h1>
            <div class="flex items-center space-x-2">
                <button @click="toggleDarkMode" class="p-2 rounded-full text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] dark:text-gray-400 dark:hover:bg-gray-700 transition-colors duration-200" :title="isDarkMode ? 'Switch to Light Mode' : 'Switch to Dark Mode'">
                    <i :class="isDarkMode ? 'fas fa-sun' : 'fas fa-moon'"></i>
                </button>
                <div class="relative" @click.away="isDropdownOpen = false">
                    <button @click="isDropdownOpen = !isDropdownOpen" class="flex items-center px-3 py-2 border border-[var(--border-secondary)] rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-accent)] focus:outline-none">
                        <i class="fas fa-user-shield mr-2"></i>
                        <span>{{ current_user.username }}</span>
                        <i class="fas fa-chevron-down ml-2"></i>
                    </button>
                    <div v-show="isDropdownOpen" class="absolute right-0 mt-2 w-48 bg-[var(--bg-secondary)] border border-[var(--border-primary)] rounded-lg shadow-lg z-10">
                        <a href="{{ url_for('index') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)]">
                            <i class="fas fa-home mr-2"></i> Home
                        </a>
                        <a href="{{ url_for('account') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)]">
                            <i class="fas fa-user mr-2"></i> Account
                        </a>
                        <a href="{{ url_for('admin') }}" class="block px-4 py-2 text-[var(--text-accent)] bg-[var(--bg-accent)]">
                            <i class="fas fa-user-shield mr-2"></i> Admin
                        </a>
                        <a href="{{ url_for('logout') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-danger)]">
                            <i class="fas fa-sign-out-alt mr-2"></i> Logout
                        </a>
                    </div>
                </div>
            </div>
        </header>

        <main class="flex-grow">
            <div class="bg-[var(--bg-secondary)] p-6 rounded-xl shadow-lg border border-[var(--border-primary)]">
                <h2 class="text-2xl font-semibold text-[var(--text-primary)] mb-6">Admin Dashboard</h2>
                
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="mb-4 p-3 rounded-lg {% if category == 'success' %}bg-[var(--bg-success-light)] text-[var(--text-success-strong)]{% elif category == 'danger' %}bg-[var(--bg-danger-light)] text-[var(--text-danger-strong)]{% else %}bg-[var(--bg-info-light)] text-[var(--text-info-strong)]{% endif %}">
                                {{ message }}
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                <!-- Tabs -->
                <div class="border-b border-[var(--border-primary)] mb-6">
                    <nav class="-mb-px flex space-x-8" aria-label="Tabs">
                        <button @click="activeTab = 'users'" 
                                :class="activeTab === 'users' ? 'border-[var(--border-focus)] text-[var(--text-accent)]' : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:border-[var(--border-secondary)]'"
                                class="whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm">
                            <i class="fas fa-users mr-2"></i> User Management
                        </button>
                        <button @click="activeTab = 'stats'" 
                                :class="activeTab === 'stats' ? 'border-[var(--border-focus)] text-[var(--text-accent)]' : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:border-[var(--border-secondary)]'"
                                class="whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm">
                            <i class="fas fa-chart-bar mr-2"></i> System Statistics
                        </button>
                    </nav>
                </div>
                
                <!-- User Management Tab -->
                <div v-show="activeTab === 'users'">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-lg font-medium text-[var(--text-secondary)]">User Management</h3>
                        <button @click="showAddUserModal = true" class="px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-lg shadow hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] focus:ring-offset-2 transition duration-150 ease-in-out">
                            <i class="fas fa-user-plus mr-2"></i> Add User
                        </button>
                    </div>
                    
                    <!-- Search and Filter -->
                    <div class="mb-4 flex">
                        <div class="relative flex-grow">
                            <input type="text" v-model="userSearchQuery" placeholder="Search users..." class="w-full px-4 py-2 border border-[var(--border-secondary)] rounded-lg focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                            <button v-if="userSearchQuery" @click="userSearchQuery = ''" class="absolute right-3 top-2.5 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
                                <i class="fas fa-times"></i>
                            </button>
                        </div>
                    </div>
                    
                    <!-- Users Table -->
                    <div class="overflow-x-auto">
                        <table class="min-w-full divide-y divide-[var(--border-primary)]">
                            <thead class="bg-[var(--bg-tertiary)]">
                                <tr>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">ID</th>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Username</th>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Email</th>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Admin</th>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Recordings</th>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Storage Used</th>
                                    <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Actions</th>
                                </tr>
                            </thead>
                            <tbody class="bg-[var(--bg-secondary)] divide-y divide-[var(--border-primary)]">
                                <tr v-for="user in filteredUsers" :key="user.id" class="hover:bg-[var(--bg-tertiary)]">
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-[var(--text-secondary)]">${ user.id }</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-[var(--text-primary)]">${ user.username }</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-[var(--text-secondary)]">${ user.email }</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-[var(--text-secondary)]">
                                        <span v-if="user.is_admin" class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-[var(--bg-success-light)] text-[var(--text-success-strong)]">Yes</span>
                                        <span v-else class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-[var(--bg-pending-light)] text-[var(--text-pending-strong)]">No</span>
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-[var(--text-secondary)]">${ user.recordings_count }</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-[var(--text-secondary)]">${ formatFileSize(user.storage_used) }</td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-[var(--text-secondary)]">
                                        <div class="flex space-x-2">
                                            <button @click="editUser(user)" class="text-[var(--text-accent)] hover:text-[var(--text-accent-hover)]" title="Edit User">
                                                <i class="fas fa-edit"></i>
                                            </button>
                                            <button @click="toggleAdminStatus(user)" :class="user.is_admin ? 'text-[var(--text-warn-strong)]' : 'text-[var(--text-info-strong)]'" :title="user.is_admin ? 'Remove Admin' : 'Make Admin'">
                                                <i class="fas" :class="user.is_admin ? 'fa-user-minus' : 'fa-user-shield'"></i>
                                            </button>
                                            <button @click="confirmDeleteUser(user)" class="text-[var(--text-danger)] hover:text-[var(--text-danger-hover)]" title="Delete User">
                                                <i class="fas fa-trash"></i>
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                                <tr v-if="filteredUsers.length === 0">
                                    <td colspan="7" class="px-6 py-4 text-center text-sm text-[var(--text-muted)]">
                                        <div v-if="isLoadingUsers">
                                            <i class="fas fa-spinner fa-spin mr-2"></i> Loading users...
                                        </div>
                                        <div v-else>
                                            No users found.
                                        </div>
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <!-- System Statistics Tab -->
                <div v-show="activeTab === 'stats'">
                    <h3 class="text-lg font-medium text-[var(--text-secondary)] mb-4">System Statistics</h3>
                    
                    <!-- Summary Cards -->
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)] shadow-sm">
                            <div class="flex items-center">
                                <div class="p-3 rounded-full bg-[var(--bg-info-light)] text-[var(--text-info-strong)]">
                                    <i class="fas fa-users text-xl"></i>
                                </div>
                                <div class="ml-4">
                                    <p class="text-sm font-medium text-[var(--text-muted)]">Total Users</p>
                                    <p class="text-2xl font-semibold text-[var(--text-primary)]">${ stats.total_users }</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)] shadow-sm">
                            <div class="flex items-center">
                                <div class="p-3 rounded-full bg-[var(--bg-success-light)] text-[var(--text-success-strong)]">
                                    <i class="fas fa-file-audio text-xl"></i>
                                </div>
                                <div class="ml-4">
                                    <p class="text-sm font-medium text-[var(--text-muted)]">Total Recordings</p>
                                    <p class="text-2xl font-semibold text-[var(--text-primary)]">${ stats.total_recordings }</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)] shadow-sm">
                            <div class="flex items-center">
                                <div class="p-3 rounded-full bg-[var(--bg-warn-light)] text-[var(--text-warn-strong)]">
                                    <i class="fas fa-database text-xl"></i>
                                </div>
                                <div class="ml-4">
                                    <p class="text-sm font-medium text-[var(--text-muted)]">Total Storage Used</p>
                                    <p class="text-2xl font-semibold text-[var(--text-primary)]">${ formatFileSize(stats.total_storage) }</p>
                                </div>
                            </div>
                        </div>
                        
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)] shadow-sm">
                            <div class="flex items-center">
                                <div class="p-3 rounded-full bg-[var(--bg-accent)] text-[var(--text-accent)]">
                                    <i class="fas fa-comments text-xl"></i>
                                </div>
                                <div class="ml-4">
                                    <p class="text-sm font-medium text-[var(--text-muted)]">Total Queries</p>
                                    <p class="text-2xl font-semibold text-[var(--text-primary)]">${ stats.total_queries }</p>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Status Distribution -->
                    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)] shadow-sm">
                            <h4 class="text-md font-medium text-[var(--text-secondary)] mb-3">Recording Status Distribution</h4>
                            <div class="grid grid-cols-2 gap-4">
                                <div class="text-center p-3 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-2xl font-bold text-[var(--text-success-strong)]">${ stats.completed_recordings }</span>
                                    <span class="block text-sm text-[var(--text-muted)]">Completed</span>
                                </div>
                                <div class="text-center p-3 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-2xl font-bold text-[var(--text-warn-strong)]">${ stats.processing_recordings }</span>
                                    <span class="block text-sm text-[var(--text-muted)]">Processing</span>
                                </div>
                                <div class="text-center p-3 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-2xl font-bold text-[var(--text-pending-strong)]">${ stats.pending_recordings }</span>
                                    <span class="block text-sm text-[var(--text-muted)]">Pending</span>
                                </div>
                                <div class="text-center p-3 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)] shadow-sm">
                                    <span class="block text-2xl font-bold text-[var(--text-danger-strong)]">${ stats.failed_recordings }</span>
                                    <span class="block text-sm text-[var(--text-muted)]">Failed</span>
                                </div>
                            </div>
                        </div>
                        
                        <div class="bg-[var(--bg-tertiary)] p-4 rounded-lg border border-[var(--border-primary)] shadow-sm">
                            <h4 class="text-md font-medium text-[var(--text-secondary)] mb-3">Top Users by Storage</h4>
                            <div class="space-y-3">
                                <div v-for="user in stats.top_users" :key="user.id" class="flex justify-between items-center p-2 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-primary)]">
                                    <div class="flex items-center">
                                        <i class="fas fa-user text-[var(--text-accent)] mr-2"></i>
                                        <span class="text-sm font-medium text-[var(--text-primary)]">${ user.username }</span>
                                    </div>
                                    <div class="text-sm text-[var(--text-secondary)]">
                                        <span class="font-medium">${ formatFileSize(user.storage_used) }</span>
                                        <span class="text-[var(--text-muted)] ml-2">(${ user.recordings_count} recordings)</span>
                                    </div>
                                </div>
                                <div v-if="stats.top_users.length === 0" class="text-center text-sm text-[var(--text-muted)] p-2">
                                    No data available
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </main>

        <footer class="text-center py-4 mt-8 text-xs text-[var(--text-light)] border-t border-[var(--border-primary)]">
            Speakr &copy; {{ now.year }}
        </footer>
        
        <!-- Add User Modal -->
        <div v-if="showAddUserModal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-xl w-full max-w-md">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-semibold text-[var(--text-primary)]">Add New User</h3>
                    <button @click="showAddUserModal = false" class="text-[var(--text-muted)] hover:text-[var(--text-secondary)]">&times;</button>
                </div>
                <form @submit.prevent="addUser">
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Username</label>
                        <input v-model="newUser.username" type="text" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Email</label>
                        <input v-model="newUser.email" type="email" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Password</label>
                        <input v-model="newUser.password" type="password" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Confirm Password</label>
                        <input v-model="newUser.confirmPassword" type="password" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4 flex items-center">
                        <input v-model="newUser.isAdmin" type="checkbox" id="isAdmin" class="rounded border-[var(--border-secondary)] text-[var(--text-accent)] focus:ring-[var(--border-focus)]">
                        <label for="isAdmin" class="ml-2 text-sm text-[var(--text-secondary)]">Admin User</label>
                    </div>
                    <div class="flex justify-end space-x-3">
                        <button type="button" @click="showAddUserModal = false" class="px-4 py-2 border border-[var(--border-secondary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--bg-tertiary)]">Cancel</button>
                        <button type="submit" class="px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)]">Add User</button>
                    </div>
                </form>
            </div>
        </div>
        
        <!-- Edit User Modal -->
        <div v-if="showEditUserModal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-xl w-full max-w-md">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-semibold text-[var(--text-primary)]">Edit User</h3>
                    <button @click="showEditUserModal = false" class="text-[var(--text-muted)] hover:text-[var(--text-secondary)]">&times;</button>
                </div>
                <form @submit.prevent="updateUser">
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Username</label>
                        <input v-model="editingUser.username" type="text" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Email</label>
                        <input v-model="editingUser.email" type="email" required class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">New Password (leave blank to keep current)</label>
                        <input v-model="editingUser.password" type="password" class="w-full px-3 py-2 border border-[var(--border-secondary)] rounded-md focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div class="mb-4 flex items-center">
                        <input v-model="editingUser.is_admin" type="checkbox" id="editIsAdmin" class="rounded border-[var(--border-secondary)] text-[var(--text-accent)] focus:ring-[var(--border-focus)]">
                        <label for="editIsAdmin" class="ml-2 text-sm text-[var(--text-secondary)]">Admin User</label>
                    </div>
                    <div class="flex justify-end space-x-3">
                        <button type="button" @click="showEditUserModal = false" class="px-4 py-2 border border-[var(--border-secondary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--bg-tertiary)]">Cancel</button>
                        <button type="submit" class="px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)]">Update User</button>
                    </div>
                </form>
            </div>
        </div>
        
        <!-- Delete User Confirmation Modal -->
        <div v-if="showDeleteUserModal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
            <div class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-xl w-full max-w-md">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-semibold text-[var(--text-primary)]">Confirm Delete</h3>
                    <button @click="showDeleteUserModal = false" class="text-[var(--text-muted)] hover:text-[var(--text-secondary)]">&times;</button>
                </div>
                <p class="mb-4 text-[var(--text-secondary)]">Are you sure you want to delete the user <span class="font-semibold">${ userToDelete?.username }</span>? This action cannot be undone.</p>
                <div class="flex justify-end space-x-3">
                    <button @click="showDeleteUserModal = false" class="px-4 py-2 border border-[var(--border-secondary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--bg-tertiary)]">Cancel</button>
                    <button @click="deleteUser" class="px-4 py-2 bg-[var(--bg-danger)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-danger-hover)]">Delete User</button>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        const { createApp, ref, computed, onMounted, watch } = Vue
        
        createApp({
            setup() {
                // State
                const activeTab = ref('users');
                const users = ref([]);
                const stats = ref({
                    total_users: 0,
                    total_recordings: 0,
                    total_storage: 0,
                    total_queries: 0,
                    completed_recordings: 0,
                    processing_recordings: 0,
                    pending_recordings: 0,
                    failed_recordings: 0,
                    top_users: []
                });
                const isLoadingUsers = ref(true);
                const userSearchQuery = ref('');
                const isDarkMode = ref(false);
                const isDropdownOpen = ref(false);
                
                // Modal state
                const showAddUserModal = ref(false);
                const showEditUserModal = ref(false);
                const showDeleteUserModal = ref(false);
                const newUser = ref({
                    username: '',
                    email: '',
                    password: '',
                    confirmPassword: '',
                    isAdmin: false
                });
                const editingUser = ref(null);
                const userToDelete = ref(null);
                
                // Computed properties
                const filteredUsers = computed(() => {
                    if (!userSearchQuery.value) return users.value;
                    
                    const query = userSearchQuery.value.toLowerCase();
                    return users.value.filter(user => 
                        user.username.toLowerCase().includes(query) || 
                        user.email.toLowerCase().includes(query)
                    );
                });
                
                // Methods
                const loadUsers = async () => {
                    isLoadingUsers.value = true;
                    try {
                        const response = await fetch('/admin/users');
                        if (!response.ok) throw new Error('Failed to load users');
                        
                        const data = await response.json();
                        users.value = data;
                    } catch (error) {
                        console.error('Error loading users:', error);
                        // Show error notification
                    } finally {
                        isLoadingUsers.value = false;
                    }
                };
                
                const loadStats = async () => {
                    try {
                        const response = await fetch('/admin/stats');
                        if (!response.ok) throw new Error('Failed to load statistics');
                        
                        const data = await response.json();
                        stats.value = data;
                    } catch (error) {
                        console.error('Error loading statistics:', error);
                        // Show error notification
                    }
                };
                
                const addUser = async () => {
                    if (newUser.value.password !== newUser.value.confirmPassword) {
                        alert('Passwords do not match');
                        return;
                    }
                    
                    try {
                        const response = await fetch('/admin/users', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({
                                username: newUser.value.username,
                                email: newUser.value.email,
                                password: newUser.value.password,
                                is_admin: newUser.value.isAdmin
                            })
                        });
                        
                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.error || 'Failed to add user');
                        }
                        
                        // Reset form and close modal
                        newUser.value = {
                            username: '',
                            email: '',
                            password: '',
                            confirmPassword: '',
                            isAdmin: false
                        };
                        showAddUserModal.value = false;
                        
                        // Reload users
                        await loadUsers();
                        await loadStats();
                        
                    } catch (error) {
                        console.error('Error adding user:', error);
                        alert(error.message);
                    }
                };
                
                const editUser = (user) => {
                    editingUser.value = { ...user, password: '' };
                    showEditUserModal.value = true;
                };
                
                const updateUser = async () => {
                    try {
                        const payload = {
                            username: editingUser.value.username,
                            email: editingUser.value.email,
                            is_admin: editingUser.value.is_admin
                        };
                        
                        // Only include password if it was changed
                        if (editingUser.value.password) {
                            payload.password = editingUser.value.password;
                        }
                        
                        const response = await fetch(`/admin/users/${editingUser.value.id}`, {
                            method: 'PUT',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify(payload)
                        });
                        
                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.error || 'Failed to update user');
                        }
                        
                        // Close modal and reload users
                        showEditUserModal.value = false;
                        await loadUsers();
                        
                    } catch (error) {
                        console.error('Error updating user:', error);
                        alert(error.message);
                    }
                };
                
                const confirmDeleteUser = (user) => {
                    userToDelete.value = user;
                    showDeleteUserModal.value = true;
                };
                
                const deleteUser = async () => {
                    try {
                        const response = await fetch(`/admin/users/${userToDelete.value.id}`, {
                            method: 'DELETE'
                        });
                        
                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.error || 'Failed to delete user');
                        }
                        
                        // Close modal and reload users
                        showDeleteUserModal.value = false;
                        await loadUsers();
                        await loadStats();
                        
                    } catch (error) {
                        console.error('Error deleting user:', error);
                        alert(error.message);
                    }
                };
                
                const toggleAdminStatus = async (user) => {
                    try {
                        const response = await fetch(`/admin/users/${user.id}/toggle-admin`, {
                            method: 'POST'
                        });
                        
                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.error || 'Failed to toggle admin status');
                        }
                        
                        // Reload users
                        await loadUsers();
                        
                    } catch (error) {
                        console.error('Error toggling admin status:', error);
                        alert(error.message);
                    }
                };
                
                const formatFileSize = (bytes) => {
                    if (!bytes || bytes === 0) return '0 Bytes';
                    
                    const k = 1024;
                    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
                    const i = Math.floor(Math.log(bytes) / Math.log(k));
                    
                    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
                };
                
                const toggleDarkMode = () => {
                    isDarkMode.value = !isDarkMode.value;
                    if (isDarkMode.value) {
                        document.documentElement.classList.add('dark');
                        localStorage.setItem('darkMode', 'true');
                    } else {
                        document.documentElement.classList.remove('dark');
                        localStorage.setItem('darkMode', 'false');
                    }
                };
                
                // Initialize dark mode
                const initializeDarkMode = () => {
                    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                    const savedMode = localStorage.getItem('darkMode');
                    
                    if (savedMode === 'true' || (savedMode === null && prefersDark)) {
                        isDarkMode.value = true;
                        document.documentElement.classList.add('dark');
                    } else {
                        isDarkMode.value = false;
                        document.documentElement.classList.remove('dark');
                    }
                };
                
                // Lifecycle hooks
                onMounted(() => {
                    loadUsers();
                    loadStats();
                    initializeDarkMode();
                });
                
                // Watch for tab changes to reload data
                watch(activeTab, (newTab) => {
                    if (newTab === 'users') {
                        loadUsers();
                    } else if (newTab === 'stats') {
                        loadStats();
                    }
                });
                
                return {
                    // State
                    activeTab,
                    users,
                    stats,
                    isLoadingUsers,
                    userSearchQuery,
                    isDarkMode,
                    isDropdownOpen,
                    
                    // Modal state
                    showAddUserModal,
                    showEditUserModal,
                    showDeleteUserModal,
                    newUser,
                    editingUser,
                    userToDelete,
                    
                    // Computed
                    filteredUsers,
                    
                    // Methods
                    loadUsers,
                    loadStats,
                    addUser,
                    editUser,
                    updateUser,
                    confirmDeleteUser,
                    deleteUser,
                    toggleAdminStatus,
                    formatFileSize,
                    toggleDarkMode
                };
            },
            delimiters: ['${', '}'] // Use different delimiters to avoid conflict with Flask's Jinja2
        }).mount('#app');
    </script>
</body>
</html>

```


## templates/index.html

```html

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Speakr</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        /* Dark Mode CSS Variables */
        :root {
            /* Light mode variables */
            --bg-primary: #f3f4f6; /* gray-100 */
            --bg-secondary: #ffffff; /* white */
            --bg-tertiary: #f9fafb; /* gray-50 */
            --bg-accent: #dbeafe; /* blue-100 */
            --bg-accent-hover: #bfdbfe; /* blue-200 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #1d4ed8; /* blue-700 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #b91c1c; /* red-700 */
            --bg-danger-light: #fee2e2; /* red-100 */
            --bg-info-light: #dbeafe; /* blue-100 */
            --bg-warn-light: #fef3c7; /* amber-100 */
            --bg-success-light: #d1fae5; /* green-100 */
            --bg-pending-light: #f5f5f4; /* stone-100 */
            --bg-input: #ffffff; /* white */
            --bg-audio-player: linear-gradient(to right, #eff6ff, #eef2ff); /* blue-50, indigo-50 */

            --text-primary: #1f2937; /* gray-800 */
            --text-secondary: #374151; /* gray-700 */
            --text-muted: #6b7280; /* gray-500 */
            --text-light: #9ca3af; /* gray-400 */
            --text-accent: #1d4ed8; /* blue-700 */
            --text-button: #ffffff; /* white */
            --text-danger: #b91c1c; /* red-700 */
            --text-danger-strong: #991b1b; /* red-800 */
            --text-info-strong: #1e40af; /* blue-800 */
            --text-warn-strong: #92400e; /* amber-800 */
            --text-success-strong: #065f46; /* green-800 */
            --text-pending-strong: #44403c; /* stone-700 */

            --border-primary: #e5e7eb; /* gray-200 */
            --border-secondary: #d1d5db; /* gray-300 */
            --border-accent: #93c5fd; /* blue-300 */
            --border-danger: #f87171; /* red-400 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #bfdbfe; /* blue-200 */
            
            --scrollbar-track: #f1f1f1;
            --scrollbar-thumb: #c5c5c5;
            --scrollbar-thumb-hover: #a8a8a8;
        }

        .dark {
            /* Dark mode variables */
            --bg-primary: #111827; /* gray-900 */
            --bg-secondary: #1f2937; /* gray-800 */
            --bg-tertiary: #374151; /* gray-700 */
            --bg-accent: #1e3a8a; /* blue-900 */
            --bg-accent-hover: #1e40af; /* blue-800 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #3b82f6; /* blue-500 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #ef4444; /* red-500 */
            --bg-danger-light: #7f1d1d; /* red-900 */
            --bg-info-light: #1e3a8a; /* blue-900 */
            --bg-warn-light: #78350f; /* amber-900 */
            --bg-success-light: #064e3b; /* green-900 */
            --bg-pending-light: #292524; /* stone-800 */
            --bg-input: #374151; /* gray-700 */
            --bg-audio-player: linear-gradient(to right, #374151, #4b5563); /* gray-700, gray-600 */

            --text-primary: #f3f4f6; /* gray-100 */
            --text-secondary: #d1d5db; /* gray-300 */
            --text-muted: #9ca3af; /* gray-400 */
            --text-light: #6b7280; /* gray-500 */
            --text-accent: #60a5fa; /* blue-400 */
            --text-button: #ffffff; /* white */
            --text-danger: #f87171; /* red-400 */
            --text-danger-strong: #fca5a5; /* red-300 */
            --text-info-strong: #93c5fd; /* blue-300 */
            --text-warn-strong: #fcd34d; /* amber-300 */
            --text-success-strong: #6ee7b7; /* green-300 */
            --text-pending-strong: #d6d3d1; /* stone-300 */

            --border-primary: #374151; /* gray-700 */
            --border-secondary: #4b5563; /* gray-600 */
            --border-accent: #1d4ed8; /* blue-700 */
            --border-danger: #ef4444; /* red-500 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #1e40af; /* blue-800 */
            
            --scrollbar-track: #2d3748; /* gray-800 */
            --scrollbar-thumb: #4a5568; /* gray-600 */
            --scrollbar-thumb-hover: #718096; /* gray-500 */
        }

        /* Modern UI styles */
        .drag-area { transition: background-color 0.3s ease, border-color 0.3s ease; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: var(--scrollbar-track); border-radius: 12px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 12px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }
        html { /* Apply base colors to html for smoother transitions */
            background-color: var(--bg-primary); 
            color: var(--text-primary); 
            transition: background-color 0.3s, color 0.3s; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        html, body { height: 100%; margin: 0; }
        #app { min-height: 100%; display: flex; flex-direction: column; }
        main { flex-grow: 1; position: relative; }
        .progress-popup { position: fixed; bottom: 1rem; left: 1rem; z-index: 100; transition: all 0.3s ease-in-out; min-width: 300px; border-radius: 12px; overflow: hidden; }
        .progress-popup.minimized { transform: translateY(calc(100% - 45px)); }
        .progress-list-item { display: grid; grid-template-columns: auto 1fr auto; gap: 0.5rem; align-items: center; }
         /* Modern style for summary box */
        .summary-box {
            background-color: #f9fafb; /* bg-gray-50 */
            padding: 1rem; /* p-4 */
            border-radius: 0.75rem; /* rounded-xl */
            border: 1px solid #e5e7eb; /* border-gray-200 */
            min-height: 60px; /* min-h-[60px] or adjust as needed */
            /* max-height removed to allow full height */
            overflow-y: auto;
            white-space: pre-wrap;
            font-family: inherit; /* Use body font */
            font-size: 0.875rem; /* text-sm */
            line-height: 1.5; /* Improved line height */
            box-shadow: 0 1px 2px rgba(0,0,0,0.03); /* Subtle shadow */
        }
        
        /* Standardize border radius for all content boxes */
        .transcription-box, 
        .summary-box, 
        .chat-container,
        textarea,
        div[v-if="!editingParticipants"],
        div[v-if="!editingNotes"] {
            border-radius: 0.75rem !important; /* rounded-xl */
        }
        
        /* Ensure consistent height for tab content boxes */
        .tab-content-box {
            height: 200px !important;
            overflow-y: auto;
        }
        .chat-content-box {
            height: 650px !important;
            overflow-y: auto;
        }
        .metadata-panel {
            background-color: var(--bg-tertiary);
            border: 1px solid var(--border-primary);
            border-radius: 0.75rem; /* rounded-xl to match others */
            padding: 1rem; /* p-4 to be consistent */
            /* margin-top removed to align with other boxes */
            font-size: 0.875rem; /* text-sm */
            color: var(--text-secondary);
        }
        .metadata-panel dt {
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 0.1rem;
        }
        .metadata-panel dd {
            margin-left: 0;
            margin-bottom: 0.5rem;
            word-break: break-all; /* Wrap long filenames */
        }
         .status-badge {
             display: inline-block;
             padding: 0.15rem 0.6rem; /* Smaller padding */
             font-size: 0.65rem; /* Smaller text */
             font-weight: 500; /* font-medium */
             border-radius: 9999px; /* rounded-full */
             /* margin-top: 0.5rem; /* mt-2 */ /* Removed margin-top */
             box-shadow: 0 1px 2px rgba(0,0,0,0.05);
             letter-spacing: 0.025em;
             vertical-align: middle; /* Align with text */
             margin-left: 0.75rem; /* Add some space */
         }
         .status-processing { color: #1d4ed8; background-color: #dbeafe; } /* text-blue-800 bg-blue-100 */
         .status-summarizing { color: #92400e; background-color: #fef3c7; } /* text-amber-800 bg-amber-100 */
         .status-completed { color: #065f46; background-color: #d1fae5; } /* text-green-800 bg-green-100 */
         .status-failed { color: #991b1b; background-color: #fee2e2; } /* text-red-800 bg-red-100 */
         .status-pending { color: #57534e; background-color: #f5f5f4; } /* text-stone-700 bg-stone-100 */
         
         /* Fixed height transcription box */
         .transcription-box {
             height: 400px;
             overflow-y: auto;
             position: relative;
         }
         
         /* Modern copy button styles */
         .copy-btn {
             position: sticky;
             top: 10px;
             right: 10px;
             float: right;
             background-color: rgba(255, 255, 255, 0.9);
             border: 1px solid #e5e7eb;
             border-radius: 0.5rem;
             padding: 0.35rem 0.75rem;
             font-size: 0.75rem;
             cursor: pointer;
             z-index: 10;
             transition: all 0.2s cubic-bezier(0.25, 0.1, 0.25, 1);
             margin-bottom: 10px;
             box-shadow: 0 1px 2px rgba(0,0,0,0.05);
         }
         
         .copy-btn:hover {
             background-color: #f3f4f6;
             transform: translateY(-1px);
             box-shadow: 0 2px 4px rgba(0,0,0,0.1);
         }
         
         /* Hover edit button styles */
         .content-box {
             position: relative;
         }
         
         .hover-edit-btn {
             position: absolute;
             top: 10px;
             right: 10px;
             background-color: rgba(255, 255, 255, 0.9);
             border: 1px solid #e5e7eb;
             border-radius: 0.5rem;
             padding: 0.35rem 0.75rem;
             font-size: 0.75rem;
             cursor: pointer;
             z-index: 10;
             transition: all 0.2s ease;
             box-shadow: 0 1px 2px rgba(0,0,0,0.05);
             opacity: 0;
         }
         
         .content-box:hover .hover-edit-btn {
             opacity: 1;
         }
         
         .hover-edit-btn:hover {
             background-color: #f3f4f6;
             transform: translateY(-1px);
             box-shadow: 0 2px 4px rgba(0,0,0,0.1);
         }
         
         .dark .hover-edit-btn {
             background-color: rgba(55, 65, 81, 0.9);
             border-color: #4b5563;
         }
         
         .dark .hover-edit-btn:hover {
             background-color: #4b5563;
         }
         
         /* Modern chat section styles */
         .chat-container {
             border: 1px solid #e5e7eb;
             border-radius: 0.75rem;
             display: flex;
             flex-direction: column;
             height: 100%;
             box-shadow: 0 1px 3px rgba(0,0,0,0.05);
             overflow: hidden;
         }
         
         .chat-messages {
             flex-grow: 1;
             overflow-y: auto;
             padding: 1.25rem;
         }
         
         .chat-input-container {
             border-top: 1px solid #e5e7eb;
             padding: 0.75rem;
             display: flex;
             background-color: var(--bg-tertiary);
         }
         
         .message {
             margin-bottom: 1.25rem;
             max-width: 80%;
             box-shadow: 0 1px 2px rgba(0,0,0,0.05);
             line-height: 1.5;
         }
         
         .user-message {
             background-color: #dbeafe;
             border-radius: 1.25rem 1.25rem 0.25rem 1.25rem;
             padding: 0.875rem 1rem;
             margin-left: auto;
         }
         
         .ai-message {
             background-color: #f3f4f6;
             border-radius: 1.25rem 1.25rem 1.25rem 0.25rem;
             padding: 0.875rem 1rem;
         }
         
         .copyable {
             position: relative;
         }
         
         /* Markdown styling */
         .ai-message h1, .ai-message h2, .ai-message h3, 
         .summary-box h1, .summary-box h2, .summary-box h3 {
             font-weight: 600;
             margin-top: 1rem;
             margin-bottom: 0.5rem;
         }
         
         .ai-message h1, .summary-box h1 { font-size: 1.25rem; }
         .ai-message h2, .summary-box h2 { font-size: 1.15rem; }
         .ai-message h3, .summary-box h3 { font-size: 1.05rem; }
         
         .ai-message p, .summary-box p {
             margin-bottom: 0.75rem;
         }
         
         .ai-message ul, .ai-message ol,
         .summary-box ul, .summary-box ol {
             margin-left: 1.5rem;
             margin-bottom: 0.75rem;
         }
         
         .ai-message ul, .summary-box ul { list-style-type: disc; }
         .ai-message ol, .summary-box ol { list-style-type: decimal; }
         
         .ai-message code, .summary-box code {
             background-color: #f1f1f1;
             padding: 0.1rem 0.3rem;
             border-radius: 0.25rem;
             font-family: monospace;
             font-size: 0.9em;
         }
         
         .ai-message pre, .summary-box pre {
             background-color: #f1f1f1;
             padding: 0.75rem;
             border-radius: 0.25rem;
             overflow-x: auto;
             margin-bottom: 0.75rem;
         }
         
         .ai-message pre code, .summary-box pre code {
             background-color: transparent;
             padding: 0;
             border-radius: 0;
         }
         
         .ai-message table, .summary-box table {
             border-collapse: collapse;
             width: 100%;
             margin-bottom: 0.75rem;
         }
         
         .ai-message th, .ai-message td,
         .summary-box th, .summary-box td {
             border: 1px solid #ddd;
             padding: 0.5rem;
             text-align: left;
         }
         
         .ai-message th, .summary-box th {
             background-color: #f1f1f1;
             font-weight: 600;
         }
         
         .ai-message blockquote, .summary-box blockquote {
             border-left: 4px solid #ddd;
             padding-left: 1rem;
             margin-left: 0;
             margin-bottom: 0.75rem;
             color: #666;
         }
         
         /* Modern toast notification styles */
         .toast-container {
             position: fixed;
             bottom: 20px;
             right: 20px;
             z-index: 1000;
             display: flex;
             flex-direction: column;
             align-items: flex-end;
             gap: 12px;
         }
         
         .toast {
             padding: 12px 18px;
             border-radius: 8px;
             background-color: #4CAF50;
             color: white;
             font-size: 14px;
             box-shadow: 0 4px 12px rgba(0,0,0,0.1);
             opacity: 0;
             transform: translateY(20px);
             transition: all 0.3s cubic-bezier(0.25, 0.1, 0.25, 1);
             display: flex;
             align-items: center;
             min-width: 200px;
         }
         
         .toast.show {
             opacity: 1;
             transform: translateY(0);
         }
         
         .toast i {
             margin-right: 8px;
         }
         
         /* Copy button animation */
         @keyframes copy-success {
             0% { transform: scale(1); }
             50% { transform: scale(1.2); }
             100% { transform: scale(1); }
         }
         
         .copy-success {
             animation: copy-success 0.3s ease;
             color: #4CAF50 !important;
         }

    </style>
</head>
<body class="bg-[var(--bg-primary)] text-[var(--text-primary)]">
    <div id="app" class="container mx-auto px-4 sm:px-6 lg:px-8 py-6 flex flex-col">
        <header class="flex justify-between items-center mb-6 pb-4 border-b border-[var(--border-primary)]">
             <h1 class="text-3xl font-bold text-[var(--text-primary)] cursor-pointer" @click="switchToGalleryView" title="Go to Gallery">
                Speakr
            </h1>
            <div class="flex items-center space-x-2">
                 <button @click="toggleDarkMode" class="p-2 rounded-full text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] dark:text-gray-400 dark:hover:bg-gray-700 transition-colors duration-200" :title="isDarkMode ? 'Switch to Light Mode' : 'Switch to Dark Mode'">
                    <i :class="isDarkMode ? 'fas fa-sun' : 'fas fa-moon'"></i>
                </button>
                {% if current_user.is_authenticated %}
                <button @click="switchToUploadView" class="px-4 py-2 mr-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-lg shadow hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] focus:ring-offset-2 transition duration-150 ease-in-out">
                    <i class="fas fa-plus mr-1"></i> New Recording
                </button>
                <button
                    @click="switchToGalleryView"
                    :class="{
                        'bg-[var(--bg-secondary)] text-[var(--text-secondary)] border border-[var(--border-secondary)]': currentView !== 'gallery',
                        'bg-[var(--bg-accent)] text-[var(--text-accent)] border border-[var(--border-accent)]': currentView === 'gallery'
                    }"
                    class="px-4 py-2 rounded-lg shadow-sm hover:bg-[var(--bg-tertiary)] focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] focus:ring-offset-1 transition duration-150 ease-in-out">
                    <i class="fas fa-images mr-1"></i> Gallery
                </button>
                <!-- User dropdown menu -->
                <div class="relative ml-2">
                    <button @click="isUserMenuOpen = !isUserMenuOpen" class="flex items-center px-3 py-2 border border-[var(--border-secondary)] rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-accent)] focus:outline-none">
                        <i class="fas fa-user mr-2"></i>
                        <span>{{ current_user.username }}</span>
                        <i class="fas fa-chevron-down ml-2"></i>
                    </button>
                    <div v-if="isUserMenuOpen" class="absolute right-0 mt-2 w-48 bg-[var(--bg-secondary)] border border-[var(--border-primary)] rounded-lg shadow-lg z-10">
                        <a href="{{ url_for('account') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)]">
                            <i class="fas fa-user-circle mr-2"></i> Account
                        </a>
                        {% if current_user.is_admin %}
                        <a href="{{ url_for('admin') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-accent)]">
                            <i class="fas fa-user-shield mr-2"></i> Admin
                        </a>
                        {% endif %}
                        <a href="{{ url_for('logout') }}" class="block px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-danger)]">
                            <i class="fas fa-sign-out-alt mr-2"></i> Logout
                        </a>
                    </div>
                </div>
                {% else %}
                <a href="{{ url_for('login') }}" class="px-4 py-2 ml-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-lg shadow hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] focus:ring-offset-2 transition duration-150 ease-in-out">
                    <i class="fas fa-sign-in-alt mr-1"></i> Login
                </a>
                <a href="{{ url_for('register') }}" class="px-4 py-2 ml-2 text-[var(--text-secondary)] hover:text-[var(--text-accent)] border border-[var(--border-secondary)] rounded-lg">
                    <i class="fas fa-user-plus mr-1"></i> Register
                </a>
                {% endif %}
            </div>
        </header>

        <div v-if="globalError" class="mb-4 p-4 bg-[var(--bg-danger-light)] border border-[var(--border-danger)] text-[var(--text-danger-strong)] rounded-lg" role="alert">
             <div class="flex justify-between items-center">
                <div>
                    <strong class="font-bold">Error:</strong>
                    <span class="block sm:inline ml-2">${ globalError }</span>
                </div>
                <button @click="globalError = null" class="text-[var(--text-danger-strong)] hover:text-[var(--text-danger)] font-bold">&times;</button>
            </div>
        </div>

        <!-- Sidebar toggle button will be positioned relative to the main container -->

        <main class="flex-grow"
             @dragover.prevent="dragover = true"
             @dragleave.prevent="handleDragLeave"
             @drop.prevent="handleDrop">

            <div v-if="dragover" class="absolute inset-0 flex items-center justify-center bg-[var(--border-focus)]/20 z-10 rounded-lg pointer-events-none border-4 border-dashed border-[var(--border-focus)]">
                <div class="text-center p-6 bg-[var(--bg-secondary)] rounded-lg shadow-xl">
                    <i class="fas fa-upload text-4xl text-[var(--text-accent)] mb-3"></i>
                    <p class="text-xl font-semibold text-[var(--text-secondary)]">Drop audio file(s) here to upload</p>
                </div>
            </div>

            {% if current_user.is_authenticated %}
            <div v-if="currentView === 'gallery'" class="flex-grow flex flex-col rounded-lg relative">
                <!-- Sidebar toggle button that appears when sidebar is collapsed - positioned at the left edge of main content, aligned with title -->
                <button v-if="isSidebarCollapsed" 
                        @click="toggleSidebar" 
                        class="absolute -left-3 top-6 z-20 bg-gradient-to-r from-[var(--bg-accent)] to-[var(--bg-secondary)] p-2 pl-1 pr-3 rounded-r-lg shadow-lg text-[var(--text-accent)] hover:text-[var(--text-primary)] hover:shadow-xl transition-all duration-300 transform hover:translate-x-1 border-r border-t border-b border-[var(--border-accent)]"
                        title="Show Sidebar">
                    <i class="fas fa-chevron-right"></i>
                </button>
                
                <div class="grid grid-cols-1 lg:grid-cols-4 gap-6 flex-grow">
                    <div v-show="!isSidebarCollapsed" class="lg:col-span-1 bg-[var(--bg-secondary)] p-4 rounded-lg shadow-md flex flex-col relative">
                         <div class="flex justify-between items-center mb-4 sticky top-0 bg-[var(--bg-secondary)] pb-3 border-b border-[var(--border-primary)]">
                            <h3 class="text-lg font-semibold text-[var(--text-primary)]">Recordings</h3>
                            <button @click="toggleSidebar" class="p-1 rounded-full text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] dark:text-gray-400 dark:hover:bg-gray-700 transition-colors duration-200" :title="isSidebarCollapsed ? 'Show Sidebar' : 'Hide Sidebar'">
                                <i class="fas fa-chevron-left"></i>
                             </button>
                        </div>
                         <div v-if="isLoadingRecordings" class="text-center text-[var(--text-muted)] py-4">
                            <i class="fas fa-spinner fa-spin mr-2"></i> Loading recordings...
                        </div>
                        <div v-else-if="recordings.length === 0 && uploadQueue.length === 0" class="text-center text-[var(--text-muted)] py-4 flex-grow flex flex-col items-center justify-center">
                             <i class="fas fa-folder-open text-4xl text-[var(--text-light)] mb-3"></i>
                            <p>No recordings yet.</p>
                            <p>Upload one or drag & drop anywhere!</p>
                        </div>
                        <div v-else class="space-y-4 overflow-y-auto custom-scrollbar flex-grow pr-1">
                            <div v-for="group in groupedRecordings" :key="group.title" class="mb-3">
                                <h4 class="font-medium text-[var(--text-muted)] text-xs uppercase tracking-wider mb-2 sticky top-0 bg-[var(--bg-secondary)] py-1">${ group.title }</h4>
                                <ul class="space-y-1">
                                    <li v-for="recording in group.items"
                                        :key="recording.id"
                                        @click="selectRecording(recording)"
                                        class="cursor-pointer p-3 rounded-md flex justify-between items-center transition duration-150 ease-in-out"
                                        :class="{
                                            'bg-[var(--bg-accent)] hover:bg-[var(--bg-accent-hover)] ring-1 ring-[var(--border-accent)] text-[var(--text-accent)]': selectedRecording?.id === recording.id,
                                            'hover:bg-[var(--bg-tertiary)] text-[var(--text-primary)]': selectedRecording?.id !== recording.id
                                        }">
                                         <div class="flex items-center overflow-hidden mr-2">
                                            <i class="fas fa-file-audio text-[var(--text-accent)] mr-2 flex-shrink-0"></i>
                                            <span class="text-sm font-medium truncate" :title="recording.title || 'Loading title...'">${ recording.title || '(Processing...)' }</span>
                                        </div>
                                        <div class="flex space-x-2 flex-shrink-0 items-center">
                                            <span v-if="recording.status === 'PROCESSING'" class="text-xs text-[var(--text-info-strong)] italic flex items-center" title="Transcribing...">
                                                 <i class="fas fa-spinner fa-spin mr-1"></i> Transcribing
                                            </span>
                                             <span v-else-if="recording.status === 'SUMMARIZING'" class="text-xs text-[var(--text-warn-strong)] italic flex items-center" title="Generating Summary...">
                                                <i class="fas fa-spinner fa-spin mr-1"></i> Summarizing
                                            </span>
                                            <span v-else-if="recording.status === 'PENDING'" class="text-xs text-[var(--text-pending-strong)] italic flex items-center" title="Waiting...">
                                                <i class="fas fa-clock mr-1"></i> Pending
                                            </span>
                                            <span v-else-if="recording.status === 'FAILED'" class="text-xs text-[var(--text-danger-strong)] italic flex items-center" title="Processing Failed">
                                                <i class="fas fa-exclamation-triangle mr-1"></i> Failed
                                            </span>
                                            <span v-else-if="recording.status === 'COMPLETED'" class="text-xs text-[var(--text-success-strong)]" title="Completed">
                                                <i class="fas fa-check-circle"></i>
                                            </span>
                                             <button @click.stop="editRecording(recording)" class="text-[var(--text-muted)] hover:text-[var(--text-accent)] text-xs p-1 rounded hover:bg-[var(--bg-tertiary)]" title="Edit Details">
                                                <i class="fas fa-edit"></i>
                                            </button>
                                            <button @click.stop="confirmDelete(recording)" class="text-[var(--text-muted)] hover:text-[var(--text-danger)] text-xs p-1 rounded hover:bg-[var(--bg-tertiary)]" title="Delete Recording">
                                                <i class="fas fa-trash"></i>
                                            </button>
                                        </div>
                                    </li>
                                </ul>
                            </div>
                        </div>
                    </div>

                     <div :class="isSidebarCollapsed ? 'lg:col-span-4' : 'lg:col-span-3'" class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-md flex flex-col">
                        <div v-if="selectedRecording" class="flex-grow flex flex-col">
                            <div class="flex flex-col sm:flex-row justify-between items-start mb-4 border-b border-[var(--border-primary)] pb-4">
                                <div class="mb-3 sm:mb-0 max-w-lg">
                                     <h3 class="text-2xl font-semibold text-[var(--text-primary)]">${ selectedRecording.title || 'Loading...' }</h3>
                                    <p class="text-sm text-[var(--text-muted)] mt-1">
                                        <!-- Display Meeting Date with edit functionality -->
                                        <span>
                                            <i class="fas fa-calendar-alt mr-1"></i> 
                                            <span v-if="!editingMeetingDate">
                                                Meeting Date: 
                                                <span @click="toggleEditMeetingDate" class="cursor-pointer hover:text-[var(--text-accent)]">
                                                    ${ selectedRecording.meeting_date ? formatDisplayDate(selectedRecording.meeting_date) : 'Click to add' }
                                                </span> | 
                                            </span>
                                            <span v-else class="inline-flex items-center">
                                                <input type="date" v-model="selectedRecording.meeting_date" 
                                                    @blur="saveInlineEdit('meeting_date')" 
                                                    class="p-0 w-40 bg-transparent border-b border-[var(--border-secondary)] focus:outline-none focus:border-[var(--border-focus)]" />
                                                <button @click="saveInlineEdit('meeting_date')" class="ml-1 text-xs text-[var(--text-accent)]">
                                                    <i class="fas fa-check"></i>
                                                </button>
                                            </span>
                                        </span>
                                        Created: ${ new Date(selectedRecording.created_at).toLocaleString() }
                                        <!-- Status Badge Moved Here -->
                                        <span :class="getStatusClass(selectedRecording.status)" class="status-badge">
                                            ${ formatStatus(selectedRecording.status) }
                                        </span>
                                    </p>
                                    <!-- Removed Size from here -->
                                    <!-- Removed separate Status Badge span -->
                                </div>
                                <div class="flex space-x-2 flex-shrink-0">
                                    <button @click="editRecording(selectedRecording)" class="px-3 py-1.5 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)] text-sm shadow-sm">
                                        <i class="fas fa-edit mr-1"></i> Edit Details
                                    </button>
                                    <button @click="confirmDelete(selectedRecording)" class="px-3 py-1.5 bg-[var(--bg-danger)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-danger-hover)] text-sm shadow-sm">
                                        <i class="fas fa-trash mr-1"></i> Delete
                                    </button>
                                </div>
                            </div>

                            <!-- Adjusted Grid: md:grid-cols-5 -> Transcription (2), Right Panel (3) -->
                            <div class="grid md:grid-cols-5 gap-6 flex-grow overflow-hidden">
                                <!-- Left Column: Participants, Transcription, Summary & Notes -->
                                <div class="md:col-span-2 flex flex-col overflow-hidden space-y-4">
                                    <div>
                                        <div class="flex justify-between items-center mb-1">
                                            <h4 class="font-semibold text-[var(--text-secondary)]">Participants</h4>
                                            <button @click="toggleEditParticipants" class="text-xs text-[var(--text-muted)] hover:text-[var(--text-accent)] p-1 rounded">
                                                <i class="fas" :class="editingParticipants ? 'fa-check' : 'fa-edit'"></i>
                                            </button>
                                        </div>
                                        <div v-if="!editingParticipants" class="text-sm bg-[var(--bg-tertiary)] p-3 rounded-xl border border-[var(--border-primary)] min-h-[40px] text-[var(--text-secondary)]">${ selectedRecording.participants || 'None specified' }</div>
                                        <textarea v-else v-model="selectedRecording.participants" @blur="saveInlineEdit('participants')" class="text-sm bg-[var(--bg-tertiary)] p-3 rounded border border-[var(--border-primary)] min-h-[40px] w-full focus:ring-[var(--ring-focus)] focus:border-[var(--border-focus)] text-[var(--text-secondary)]" placeholder="Enter participants"></textarea>
                                    </div>
                                    <div>
                                        <div class="flex justify-between items-center mb-1">
                                            <h4 class="font-semibold text-[var(--text-secondary)]">Transcription</h4>
                                            <button @click="copyTranscription($event)" class="text-xs text-[var(--text-muted)] hover:text-[var(--text-accent)] p-1 rounded" title="Copy to clipboard">
                                                <i class="fas fa-copy"></i>
                                            </button>
                                        </div>
                                        <div class="transcription-box p-4 bg-[var(--bg-tertiary)] rounded border border-[var(--border-primary)] text-sm custom-scrollbar relative text-[var(--text-secondary)]">
                                            <div v-if="selectedRecording.status === 'COMPLETED' || selectedRecording.status === 'SUMMARIZING'" class="copyable">
                                                <pre class="whitespace-pre-wrap font-sans">${ selectedRecording.transcription || 'No transcription available.' }</pre>
                                            </div>
                                            <div v-else-if="selectedRecording.status === 'FAILED'" class="text-[var(--text-danger)]">
                                                <p class="font-medium mb-2">Processing Failed:</p>
                                                <pre class="whitespace-pre-wrap font-sans">${ selectedRecording.transcription || 'An unknown error occurred.' }</pre>
                                            </div>
                                            <div v-else class="flex items-center justify-center text-[var(--text-muted)] h-full">
                                                <i class="fas fa-spinner fa-spin mr-2"></i> Transcription in progress...
                                            </div>
                                        </div>
                                    </div>
                                    
                                    <!-- Tabs for Summary and Notes (Moved from right column) -->
                                    <div class="flex flex-col overflow-hidden">
                                        <div class="border-b border-[var(--border-primary)]">
                                            <nav class="-mb-px flex space-x-4" aria-label="Tabs">
                                                <button @click="selectedTab = 'summary'" 
                                                        :class="selectedTab === 'summary' ? 'border-[var(--border-focus)] text-[var(--text-accent)]' : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:border-[var(--border-secondary)]'"
                                                        class="whitespace-nowrap py-3 px-1 border-b-2 font-medium text-sm">
                                                    Summary
                                                </button>
                                                <button @click="selectedTab = 'notes'" 
                                                        :class="selectedTab === 'notes' ? 'border-[var(--border-focus)] text-[var(--text-accent)]' : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:border-[var(--border-secondary)]'"
                                                        class="whitespace-nowrap py-3 px-1 border-b-2 font-medium text-sm">
                                                    Notes
                                                </button>
                                                <button @click="selectedTab = 'metadata'" 
                                                        :class="selectedTab === 'metadata' ? 'border-[var(--border-focus)] text-[var(--text-accent)]' : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:border-[var(--border-secondary)]'"
                                                        class="whitespace-nowrap py-3 px-1 border-b-2 font-medium text-sm">
                                                    Metadata
                                                </button>
                                            </nav>
                                        </div>
                                        <div class="pt-4">
                                            <!-- Summary Panel -->
                                            <div v-show="selectedTab === 'summary'" class="overflow-hidden">
                                                <div v-if="selectedRecording.status === 'COMPLETED'">
                                                    <div v-if="!editingSummary" class="content-box">
                                                        <div class="summary-box custom-scrollbar bg-[var(--bg-tertiary)] border-[var(--border-primary)] text-[var(--text-secondary)] tab-content-box" v-html="selectedRecording.summary_html || selectedRecording.summary || 'No summary generated.'">
                                                        </div>
                                                        <button @click="toggleEditSummary" class="hover-edit-btn text-[var(--text-muted)] hover:text-[var(--text-accent)]" title="Edit Summary">
                                                            <i class="fas fa-edit"></i>
                                                        </button>
                                                    </div>
                                                    <textarea v-else v-model="selectedRecording.summary" @blur="saveInlineEdit('summary')" class="summary-box custom-scrollbar bg-[var(--bg-tertiary)] border-[var(--border-primary)] text-[var(--text-secondary)] w-full focus:ring-[var(--ring-focus)] focus:border-[var(--border-focus)] tab-content-box" placeholder="Enter summary"></textarea>
                                                </div>
                                                <div v-else-if="selectedRecording.status === 'FAILED'" class="summary-box text-[var(--text-danger)] custom-scrollbar bg-[var(--bg-tertiary)] border-[var(--border-primary)] tab-content-box" v-html="selectedRecording.summary_html || selectedRecording.summary || 'Summary generation failed or was skipped.'">
                                                </div>
                                                <div v-else class="h-24 flex items-center justify-center p-4 bg-[var(--bg-tertiary)] rounded border border-[var(--border-primary)] text-[var(--text-muted)]">
                                                    <i class="fas fa-spinner fa-spin mr-2"></i> Summary pending...
                                                </div>
                                                
                                            </div>
                                            
                                            <!-- Notes Panel -->
                                            <div v-show="selectedTab === 'notes'" class="overflow-hidden">
                                                <div v-if="!editingNotes" class="content-box">
                                                    <div class="text-sm bg-[var(--bg-tertiary)] p-4 rounded-xl border border-[var(--border-primary)] custom-scrollbar text-[var(--text-secondary)] tab-content-box" v-html="selectedRecording.notes_html || selectedRecording.notes || 'No notes'"></div>
                                                    <button @click="toggleEditNotes" class="hover-edit-btn text-[var(--text-muted)] hover:text-[var(--text-accent)]" title="Edit Notes">
                                                        <i class="fas fa-edit"></i>
                                                    </button>
                                                </div>
                                                <textarea v-else v-model="selectedRecording.notes" @blur="saveInlineEdit('notes')" class="text-sm bg-[var(--bg-tertiary)] p-4 rounded border border-[var(--border-primary)] custom-scrollbar text-[var(--text-secondary)] w-full focus:ring-[var(--ring-focus)] focus:border-[var(--border-focus)] tab-content-box" placeholder="Enter notes"></textarea>
                                            </div>
                                            
                                            <!-- Metadata Panel (in separate tab) -->
                                            <div v-show="selectedTab === 'metadata'" class="overflow-hidden">
                                                <div class="metadata-panel custom-scrollbar tab-content-box">
                                                    <dl>
                                                        <div v-if="selectedRecording.original_filename">
                                                            <dt>Original Filename</dt>
                                                            <dd>${ selectedRecording.original_filename }</dd>
                                                        </div>
                                                        <div>
                                                            <dt>File Size</dt>
                                                            <dd>${ formatFileSize(selectedRecording.file_size) }</dd>
                                                        </div>
                                                    </dl>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <!-- Right Column: Audio & Chat -->
                                <div class="md:col-span-3 flex flex-col space-y-4 overflow-hidden">
                                    <div>
                                        <h4 class="font-semibold text-[var(--text-secondary)] mb-2">Audio Player</h4>
                                        <!-- Beautified audio player container -->
                                        <div class="bg-[var(--bg-audio-player)] p-3 rounded-lg shadow border border-[var(--border-primary)]">
                                            <audio controls class="w-full h-10" :key="selectedRecording.id" :src="'/audio/' + selectedRecording.id">
                                                Your browser does not support the audio element.
                                            </audio>
                                        </div>
                                    </div>

                                    <!-- Chat Section (Fixed height to match left column) -->
                                    <div class="flex flex-col overflow-hidden">
                                        <h4 class="font-semibold text-[var(--text-secondary)] mb-2 flex-shrink-0">Chat with Transcript</h4>
                                        <!-- Chat container with fixed height matching left column -->
                                        <div class="chat-container border-[var(--border-primary)] bg-[var(--bg-secondary)] chat-content-box"> 
                                            <div class="chat-messages custom-scrollbar" ref="chatMessagesRef">
                                                <div v-if="chatMessages.length === 0" class="flex items-center justify-center h-full text-[var(--text-muted)]">
                                                    <div class="text-center">
                                                        <i class="fas fa-comments text-2xl mb-2"></i>
                                                        <p>Ask questions about the transcript</p>
                                                    </div>
                                                </div>
                                                <div v-for="(message, index) in chatMessages" :key="index" class="message" :class="message.role === 'user' ? 'user-message bg-[var(--bg-accent)] text-[var(--text-accent)]' : 'ai-message bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'">
                                                    <div class="flex justify-between items-start">
                                                        <div class="flex-grow" v-if="message.role === 'user'">${ message.content }</div>
                                                        <div class="flex-grow" v-else v-html="message.html || message.content"></div>
                                                        <button v-if="message.role === 'assistant'" @click="copyMessage(message.content, $event)" class="ml-2 text-[var(--text-light)] hover:text-[var(--text-muted)]" title="Copy response">
                                                            <i class="fas fa-copy text-xs"></i>
                                                        </button>
                                                    </div>
                                                </div>
                                                <div v-if="isChatLoading" class="message ai-message bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
                                                    <div class="flex items-center">
                                                        <i class="fas fa-spinner fa-spin mr-2"></i> Thinking...
                                                    </div>
                                                </div>
                                            </div>
                                            <div class="chat-input-container border-t-[var(--border-primary)]">
                                                <input 
                                                    v-model="chatInput" 
                                                    @keyup.enter="sendChatMessage"
                                                    type="text" 
                                                    placeholder="Ask about the transcript..." 
                                                    class="flex-grow px-3 py-2 border-0 focus:ring-0 focus:outline-none bg-[var(--bg-input)] text-[var(--text-primary)] placeholder-[var(--text-muted)]"
                                                    :disabled="isChatLoading || !selectedRecording || selectedRecording.status !== 'COMPLETED'"
                                                >
                                                <button 
                                                    @click="sendChatMessage" 
                                                    class="px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)] disabled:bg-[var(--bg-tertiary)] disabled:text-[var(--text-muted)] disabled:cursor-not-allowed"
                                                    :disabled="!chatInput.trim() || isChatLoading || !selectedRecording || selectedRecording.status !== 'COMPLETED'"
                                                >
                                                    <i class="fas fa-paper-plane"></i>
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div v-else class="flex flex-col items-center justify-center text-center text-[var(--text-muted)] flex-grow">
                             <i class="fas fa-hand-pointer text-4xl text-[var(--text-light)] mb-4"></i>
                            <p class="text-lg font-medium mb-2">Select a recording from the list to view details.</p>
                            <p>Or, drag and drop audio file(s) anywhere on this page to upload.</p>
                        </div>
                    </div>
                </div>
            </div>

             <div v-if="currentView === 'upload'"
                 class="flex-grow flex items-center justify-center p-4">
                 <div class="w-full max-w-lg bg-[var(--bg-secondary)] p-8 rounded-xl shadow-lg border border-[var(--border-primary)] text-center drag-area"
                     :class="{'border-[var(--border-focus)] bg-[var(--bg-accent)]': dragover}">
                    <div class="py-8">
                        <i class="fas fa-cloud-upload-alt text-5xl mb-5 text-[var(--text-accent)]"></i>
                         <h2 class="text-xl font-semibold text-[var(--text-secondary)] mb-2">Upload New Recordings</h2>
                        <p class="text-[var(--text-muted)] mb-4">Drag & drop your audio files here or click below.</p>
                         <input type="file" @change="handleFileSelect" accept="audio/*" class="hidden" ref="fileInput" multiple>
                        <button @click="$refs.fileInput.click()" class="mt-4 px-6 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-lg hover:bg-[var(--bg-button-hover)] shadow-sm transition duration-150 ease-in-out">
                            <i class="fas fa-file-import mr-2"></i> Select Files
                        </button>
                         <p class="text-xs text-[var(--text-light)] mt-4">Max file size per file: ${ maxFileSizeMB } MB</p>
                    </div>
                </div>
            </div>

            {% else %}
            <div class="flex-grow flex items-center justify-center">
                <div class="text-center max-w-lg p-8 bg-[var(--bg-secondary)] rounded-xl shadow-lg border border-[var(--border-primary)]">
                    <i class="fas fa-lock text-5xl mb-4 text-[var(--text-accent)]"></i>
                    <h2 class="text-2xl font-semibold text-[var(--text-primary)] mb-4">Welcome to Speakr</h2>
                    <p class="text-[var(--text-secondary)] mb-6">
                        Speakr is an audio transcription and summarization app that helps you convert your audio recordings into text and generate summaries.
                    </p>
                    <div class="flex flex-col sm:flex-row justify-center space-y-3 sm:space-y-0 sm:space-x-4">
                        <a href="{{ url_for('login') }}" class="px-6 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-lg shadow hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--border-focus)] focus:ring-offset-2 transition duration-150 ease-in-out">
                            <i class="fas fa-sign-in-alt mr-2"></i> Login
                        </a>
                        <a href="{{ url_for('register') }}" class="px-6 py-2 text-[var(--text-secondary)] hover:text-[var(--text-accent)] border border-[var(--border-secondary)] rounded-lg">
                            <i class="fas fa-user-plus mr-2"></i> Register
                        </a>
                    </div>
                </div>
            </div>
            {% endif %}
            </main>

        <footer class="text-center py-4 mt-8 text-xs text-[var(--text-light)] border-t border-[var(--border-primary)]">
            Speakr &copy; ${ new Date().getFullYear() }
        </footer>

         <div v-if="showEditModal" class="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-50 p-4">
             <div class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto custom-scrollbar">
                 <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-semibold text-[var(--text-primary)]">Edit Recording Details</h3>
                    <button @click="cancelEdit" class="text-[var(--text-light)] hover:text-[var(--text-muted)] text-2xl leading-none">&times;</button>
                </div>
                <div v-if="editingRecording" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Title</label>
                        <input v-model="editingRecording.title" class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Participants</label>
                        <input v-model="editingRecording.participants" class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                    <!-- Add Meeting Date Input -->
                    <div>
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Meeting Date</label>
                        <input type="date" v-model="editingRecording.meeting_date" class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]">
                    </div>
                     <div>
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Summary</label>
                        <textarea v-model="editingRecording.summary" class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]" rows="5"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-[var(--text-secondary)] mb-1">Notes</label>
                        <textarea v-model="editingRecording.notes" class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]" rows="4"></textarea>
                    </div>
                    <div class="flex justify-end space-x-3 pt-4">
                        <button @click="cancelEdit" class="px-4 py-2 bg-[var(--bg-tertiary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--border-secondary)]">Cancel</button>
                        <button @click="saveEdit" class="px-4 py-2 bg-[var(--bg-button)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-button-hover)]">Save Changes</button>
                    </div>
                </div>
            </div>
        </div>

        <div v-if="showDeleteModal" class="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-50 p-4">
             <div class="bg-[var(--bg-secondary)] p-6 rounded-lg shadow-xl max-w-md w-full">
                <h3 class="text-lg font-semibold text-[var(--text-primary)] mb-4">Confirm Delete</h3>
                 <p v-if="recordingToDelete" class="mb-2 text-[var(--text-secondary)]">Are you sure you want to permanently delete the recording titled "<strong>${ recordingToDelete.title }</strong>"?</p>
                <p class="text-sm text-[var(--text-danger)] mb-6">This action cannot be undone and will delete the record, transcription, summary, and the audio file.</p>
                <div class="flex justify-end space-x-3">
                    <button @click="cancelDelete" class="px-4 py-2 bg-[var(--bg-tertiary)] text-[var(--text-secondary)] rounded-md hover:bg-[var(--border-secondary)]">Cancel</button>
                    <button @click="deleteRecording" class="px-4 py-2 bg-[var(--bg-danger)] text-[var(--text-button)] rounded-md hover:bg-[var(--bg-danger-hover)]">Delete Permanently</button>
                </div>
            </div>
        </div>

        <div v-if="(uploadQueue.length > 0 || currentlyProcessingFile) && !progressPopupClosed"
             class="progress-popup bg-[var(--bg-secondary)] rounded-lg shadow-xl border border-[var(--border-primary)] overflow-hidden"
             :class="{ 'minimized': progressPopupMinimized }">

            <div class="flex justify-between items-center p-2 bg-[var(--bg-tertiary)] border-b border-[var(--border-primary)]">
                <h4 class="text-sm font-semibold text-[var(--text-secondary)] cursor-pointer" @click="progressPopupMinimized = !progressPopupMinimized">
                    <i class="fas fa-upload mr-2 text-[var(--text-accent)]"></i>
                    Upload & Process Progress (${ completedInQueue }/${ totalInQueue } completed)
                </h4>
                <div class="flex items-center">
                    <button class="text-[var(--text-muted)] hover:text-[var(--text-secondary)] mr-2" @click="progressPopupMinimized = !progressPopupMinimized">
                        <i :class="progressPopupMinimized ? 'fa-chevron-up' : 'fa-chevron-down'" class="fas fa-fw"></i>
                    </button>
                    <button class="text-[var(--text-muted)] hover:text-[var(--text-danger)]" @click="progressPopupClosed = true" title="Close">
                        <i class="fas fa-times fa-fw"></i>
                    </button>
                </div>
            </div>

             <div class="p-3 max-h-60 overflow-y-auto custom-scrollbar" v-show="!progressPopupMinimized">
                 <div v-if="currentlyProcessingFile" class="mb-3 pb-3 border-b border-dashed border-[var(--border-secondary)]">
                    <div class="flex items-center justify-between mb-1">
                        <p class="text-xs font-medium text-[var(--text-primary)] truncate mr-2" :title="currentlyProcessingFile.file.name">
                            <i class="fas fa-spinner fa-spin text-[var(--text-accent)] mr-1"></i> Processing: ${ currentlyProcessingFile.file.name }
                        </p>
                        <span class="text-xs text-[var(--text-muted)] flex-shrink-0">${ formatFileSize(currentlyProcessingFile.file.size) }</span>
                    </div>
                     <p class="text-xs text-[var(--text-secondary)] mb-1 italic">${ processingMessage }</p>
                    <div class="w-full bg-[var(--border-primary)] rounded-full h-1.5">
                         <div class="bg-[var(--text-accent)] h-1.5 rounded-full transition-all duration-300" :style="{ width: processingProgress + '%' }"></div>
                    </div>
                </div>

                 <div v-if="queuedFiles.length > 0" class="mb-2">
                    <p class="text-xs font-semibold text-[var(--text-muted)] mb-1">${ queuedFiles.length } file(s) queued:</p>
                    <ul class="space-y-1">
                        <li v-for="item in queuedFiles" :key="item.clientId" class="text-xs text-[var(--text-secondary)] progress-list-item">
                            <i class="fas fa-clock text-[var(--text-light)] fa-fw"></i>
                            <span class="truncate" :title="item.file.name">${ item.file.name }</span>
                            <span class="text-[var(--text-light)] flex-shrink-0">${ formatFileSize(item.file.size) }</span>
                        </li>
                    </ul>
                </div>

                <div v-if="finishedFilesInQueue.length > 0" class="mt-2 pt-2 border-t border-dashed border-[var(--border-secondary)]">
                    <p class="text-xs font-semibold text-[var(--text-muted)] mb-1">Recently finished:</p>
                    <ul class="space-y-1">
                         <li v-for="item in finishedFilesInQueue.slice(-5)" :key="item.clientId" class="text-xs progress-list-item">
                             <i v-if="item.status === 'completed'" class="fas fa-check-circle text-[var(--text-success-strong)] fa-fw"></i>
                            <i v-else-if="item.status === 'failed'" class="fas fa-exclamation-triangle text-[var(--text-danger-strong)] fa-fw"></i>
                             <i v-else class="fas fa-question-circle text-[var(--text-light)] fa-fw"></i>
                            <span class="truncate text-[var(--text-secondary)]" :title="item.file.name">${ item.file.name }</span>
                             <span v-if="item.status === 'failed'" class="text-[var(--text-danger)] text-xs italic flex-shrink-0">Failed</span>
                            <span v-else class="text-[var(--text-light)] flex-shrink-0">${ formatFileSize(item.file.size) }</span>
                        </li>
                    </ul>
                </div>

                 <div v-if="uploadQueue.length > 0 && queuedFiles.length === 0 && !currentlyProcessingFile" class="text-xs text-center text-[var(--text-muted)] py-2">
                    All uploads processed.
                </div>
            </div>
        </div>

    </div>
    
    <!-- Toast container for notifications -->
    <div class="toast-container" id="toastContainer"></div>
    
    {% raw %}
    <script>
        const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue

        createApp({
            setup() {
                const currentView = ref('gallery');
                const dragover = ref(false);
                const recordings = ref([]);
                const selectedRecording = ref(null);
                const selectedTab = ref('summary'); // For Summary/Notes tabs

                // --- Multi-Upload State ---
                // Status: 'queued'|'uploading'|'processing'|'summarizing'|'completed'|'failed'
                const uploadQueue = ref([]);
                const currentlyProcessingFile = ref(null);
                const processingProgress = ref(0);
                const processingMessage = ref('');
                const isProcessingActive = ref(false);
                const pollInterval = ref(null);
                const progressPopupMinimized = ref(false);
                const progressPopupClosed = ref(false);

                const showEditModal = ref(false);
                const showDeleteModal = ref(false);
                const editingRecording = ref(null); // Holds a *copy* for the modal
                const recordingToDelete = ref(null);
                // const autoSaveTimeout = ref(null); // Autosave not implemented for modal
                const isLoadingRecordings = ref(true);
                const globalError = ref(null);
                const maxFileSizeMB = ref(250); // Default, could fetch from config if needed
                const isDarkMode = ref(false); // Dark mode state
                const isSidebarCollapsed = ref(false); // Sidebar state
                const isUserMenuOpen = ref(false); // User dropdown menu state
                
                // Inline editing state
                const editingParticipants = ref(false);
                const editingMeetingDate = ref(false);
                const editingSummary = ref(false);
                const editingNotes = ref(false);

                // --- Computed Properties ---
                const groupedRecordings = computed(() => {
                    const sortedRecordings = [...recordings.value].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
                    // Grouping logic (same as before)...
                    const groups = { today: [], yesterday: [], thisWeek: [], older: [] };
                    const now = new Date();
                    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                    const yesterdayStart = new Date(todayStart);
                    yesterdayStart.setDate(todayStart.getDate() - 1);
                    const currentDayOfWeek = now.getDay();
                    const daysToSubtract = currentDayOfWeek === 0 ? 6 : currentDayOfWeek - 1;
                    const weekStart = new Date(todayStart);
                    weekStart.setDate(todayStart.getDate() - daysToSubtract);

                    sortedRecordings.forEach(r => {
                         const date = new Date(r.created_at);
                        if (date >= todayStart) groups.today.push(r);
                        else if (date >= yesterdayStart) groups.yesterday.push(r);
                        else if (date >= weekStart) groups.thisWeek.push(r);
                        else groups.older.push(r);
                    });
                     return [
                        { title: 'Today', items: groups.today },
                        { title: 'Yesterday', items: groups.yesterday },
                        { title: 'This Week', items: groups.thisWeek },
                        { title: 'Older', items: groups.older }
                    ].filter(g => g.items.length > 0);
                });

                 const totalInQueue = computed(() => uploadQueue.value.length);
                const completedInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'completed' || item.status === 'failed').length);
                const queuedFiles = computed(() => uploadQueue.value.filter(item => item.status === 'queued'));
                // Filter finished: includes completed and failed
                 const finishedFilesInQueue = computed(() => uploadQueue.value.filter(item => ['completed', 'failed'].includes(item.status)));


                // --- Methods ---
                const setGlobalError = (message, duration = 7000) => {
                     globalError.value = message;
                     if (duration > 0) {
                        setTimeout(() => { if (globalError.value === message) globalError.value = null; }, duration);
                    }
                };

                 const formatFileSize = (bytes) => {
                     if (bytes == null || bytes === 0) return '0 Bytes';
                    const k = 1024;
                    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
                     if (bytes < 0) bytes = 0;
                    const i = bytes === 0 ? 0 : Math.max(0, Math.floor(Math.log(bytes) / Math.log(k)));
                     const size = i === 0 ? bytes : parseFloat((bytes / Math.pow(k, i)).toFixed(2));
                    return size + ' ' + sizes[i];
                };

                // --- Dark Mode ---
                const toggleDarkMode = () => {
                    isDarkMode.value = !isDarkMode.value;
                    if (isDarkMode.value) {
                        document.documentElement.classList.add('dark');
                        localStorage.setItem('darkMode', 'true');
                    } else {
                        document.documentElement.classList.remove('dark');
                        localStorage.setItem('darkMode', 'false');
                    }
                };

                const initializeDarkMode = () => {
                    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                    const savedMode = localStorage.getItem('darkMode');
                    if (savedMode === 'true' || (savedMode === null && prefersDark)) {
                        isDarkMode.value = true;
                        document.documentElement.classList.add('dark');
                    } else {
                        isDarkMode.value = false;
                        document.documentElement.classList.remove('dark');
                    }
                };
                // --- End Dark Mode ---

                // --- Sidebar Toggle ---
                const toggleSidebar = () => {
                    isSidebarCollapsed.value = !isSidebarCollapsed.value;
                    // Optional: Save state to localStorage if persistence is desired
                    // localStorage.setItem('sidebarCollapsed', isSidebarCollapsed.value);
                };
                // --- End Sidebar Toggle ---
                
                // Helper to format date for display (e.g., "May 4, 2025")
                const formatDisplayDate = (dateString) => {
                    if (!dateString) return '';
                    try {
                        // Input is expected as 'YYYY-MM-DD'
                        const date = new Date(dateString + 'T00:00:00'); // Add time to avoid timezone issues
                        return date.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
                    } catch (e) {
                        console.error("Error formatting date:", e);
                        return dateString; // Return original string if formatting fails
                    }
                };

                // Helper for status display
                 const formatStatus = (status) => {
                     if (!status) return 'Unknown';
                     // Capitalize first letter, handle 'SUMMARIZING' specifically
                     return status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
                };

                // Helper for status badge class
                const getStatusClass = (status) => {
                    switch(status) {
                        case 'PENDING': return 'status-pending';
                        case 'PROCESSING': return 'status-processing';
                        case 'SUMMARIZING': return 'status-summarizing';
                        case 'COMPLETED': return 'status-completed';
                        case 'FAILED': return 'status-failed';
                        default: return 'status-pending'; // Default or unknown
                    }
                };


                const resetCurrentFileProcessingState = () => {
                    if (pollInterval.value) clearInterval(pollInterval.value);
                    pollInterval.value = null;
                    currentlyProcessingFile.value = null;
                    processingProgress.value = 0;
                    processingMessage.value = '';
                };

                const switchToUploadView = () => {
                    currentView.value = 'upload';
                    selectedRecording.value = null;
                 };

                const switchToGalleryView = async () => {
                    currentView.value = 'gallery';
                     await loadRecordings(); // Refresh recordings when switching back
                };

                const handleDragLeave = (e) => {
                    if (e.relatedTarget && e.currentTarget.contains(e.relatedTarget)) {
                         return;
                    }
                    dragover.value = false;
                }

                const handleDrop = (e) => {
                    dragover.value = false;
                    addFilesToQueue(e.dataTransfer.files);
                 };

                const handleFileSelect = (e) => {
                    addFilesToQueue(e.target.files);
                    e.target.value = null; // Reset input
                };

                // --- Queue Management ---
                const addFilesToQueue = (files) => {
                    let filesAdded = 0;
                    for (const file of files) {
                        if (file && file.type.startsWith('audio/')) {
                             if (file.size > maxFileSizeMB.value * 1024 * 1024) {
                                setGlobalError(`File "${file.name}" exceeds the maximum size of ${maxFileSizeMB.value} MB and was skipped.`);
                                continue;
                            }
                             const clientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
                            uploadQueue.value.push({
                                file: file, status: 'queued', recordingId: null, clientId: clientId, error: null
                            });
                            filesAdded++;
                        } else if (file) {
                            setGlobalError(`Invalid file type "${file.name}". Only audio files are accepted. File skipped.`);
                        }
                    }
                     if(filesAdded > 0) {
                        console.log(`Added ${filesAdded} file(s) to the queue.`);
                        progressPopupMinimized.value = false; // Show popup
                        progressPopupClosed.value = false; // Reset closed state to make popup reappear
                        if (!isProcessingActive.value) {
                            startProcessingQueue();
                        }
                    }
                };

                 const startProcessingQueue = async () => {
                    console.log("Attempting to start processing queue...");
                    if (isProcessingActive.value) {
                        console.log("Queue processor already active.");
                        return;
                    }

                    isProcessingActive.value = true;
                    resetCurrentFileProcessingState();

                     const nextFileItem = uploadQueue.value.find(item => item.status === 'queued');

                    if (nextFileItem) {
                        console.log(`Processing next file: ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId})`);
                        currentlyProcessingFile.value = nextFileItem;
                        nextFileItem.status = 'uploading';
                        processingMessage.value = 'Preparing upload...';
                        processingProgress.value = 5;

                        try {
                            const formData = new FormData();
                            formData.append('file', nextFileItem.file);

                            processingMessage.value = 'Uploading file...';
                            processingProgress.value = 10;

                            const response = await fetch('/upload', { method: 'POST', body: formData });
                            const data = await response.json();

                            if (!response.ok) {
                                let errorMsg = data.error || `Upload failed with status ${response.status}`;
                                if (response.status === 413) errorMsg = data.error || `File too large. Max: ${data.max_size_mb?.toFixed(0) || maxFileSizeMB.value} MB.`;
                                throw new Error(errorMsg);
                            }

                             if (response.status === 202 && data.id) {
                                console.log(`File ${nextFileItem.file.name} uploaded. Recording ID: ${data.id}. Starting status poll.`);
                                // Status is now 'PENDING' on backend, will be updated by poll
                                nextFileItem.status = 'pending'; // Reflect initial backend status
                                nextFileItem.recordingId = data.id;
                                processingMessage.value = 'Upload complete. Waiting for processing...';
                                processingProgress.value = 30;

                                 // Add preliminary record to gallery immediately
                                recordings.value.unshift(data);
                                // Optionally select the new item
                                // selectRecording(data);

                                pollProcessingStatus(nextFileItem); // Start polling

                            } else {
                                throw new Error('Unexpected success response from server after upload.');
                            }

                        } catch (error) {
                            console.error(`Upload/Processing Error for ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId}):`, error);
                            nextFileItem.status = 'failed';
                            nextFileItem.error = error.message;
                             // Find the potentially added preliminary record and mark it failed
                             const failedRecordIndex = recordings.value.findIndex(r => r.id === nextFileItem.recordingId);
                             if(failedRecordIndex !== -1) {
                                recordings.value[failedRecordIndex].status = 'FAILED';
                                recordings.value[failedRecordIndex].transcription = `Upload/Processing failed: ${error.message}`;
                             } else {
                                // If record wasn't even created, add a note
                                setGlobalError(`Failed to process "${nextFileItem.file.name}": ${error.message}`);
                             }

                             // Reset state and try next file
                             resetCurrentFileProcessingState();
                             isProcessingActive.value = false;
                             await nextTick();
                             startProcessingQueue();
                         }
                    } else {
                        console.log("Upload queue is empty or no files are queued.");
                        isProcessingActive.value = false;
                        // Optional: Auto-minimize popup after a delay
                         // setTimeout(() => {
                        //     if (!isProcessingActive.value && uploadQueue.value.every(f => ['completed', 'failed'].includes(f.status))) {
                         //         progressPopupMinimized.value = true;
                        //     }
                         // }, 5000);
                    }
                };

                const pollProcessingStatus = (fileItem) => {
                     if (pollInterval.value) clearInterval(pollInterval.value);

                    const recordingId = fileItem.recordingId;
                    if (!recordingId) {
                        console.error("Cannot poll status without recording ID for", fileItem.file.name);
                        fileItem.status = 'failed';
                        fileItem.error = 'Internal error: Missing recording ID for polling.';
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;
                        nextTick(startProcessingQueue); // Try next file
                        return;
                    }

                    // Initial message based on fileItem status (should be 'pending' initially)
                    processingMessage.value = 'Waiting for transcription...';
                    processingProgress.value = 40;

                    pollInterval.value = setInterval(async () => {
                        // Check if the item is still the one being processed and hasn't finished/failed
                         if (!currentlyProcessingFile.value || currentlyProcessingFile.value.clientId !== fileItem.clientId || ['completed', 'failed'].includes(fileItem.status)) {
                             console.log(`Polling stopped for ${fileItem.clientId} as it's no longer active or finished.`);
                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                             // If this was the active file, allow queue to restart
                            if (currentlyProcessingFile.value && currentlyProcessingFile.value.clientId === fileItem.clientId) {
                                resetCurrentFileProcessingState();
                                isProcessingActive.value = false;
                                await nextTick();
                                startProcessingQueue();
                            }
                            return;
                        }

                        try {
                            console.log(`Polling status for recording ID: ${recordingId} (${fileItem.file.name})`);
                            const response = await fetch(`/status/${recordingId}`);
                            if (!response.ok) throw new Error(`Status check failed with status ${response.status}`);

                            const data = await response.json();
                             const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);

                            // Update item in the main recordings list
                            if (galleryIndex !== -1) {
                                recordings.value[galleryIndex] = data;
                                if(selectedRecording.value?.id === recordingId) {
                                    selectedRecording.value = data; // Update selection if viewing details
                                }
                            }

                            // Update the status in the uploadQueue item as well
                            fileItem.status = data.status;

                            // Update progress display based on backend status
                            if (data.status === 'COMPLETED') {
                                console.log(`Processing COMPLETED for ${fileItem.file.name} (ID: ${recordingId})`);
                                processingMessage.value = 'Processing complete!';
                                processingProgress.value = 100;
                                fileItem.status = 'completed'; // Final status for queue item
                                // Stop polling, reset state, and trigger next item
                                clearInterval(pollInterval.value);
                                pollInterval.value = null;
                                resetCurrentFileProcessingState();
                                isProcessingActive.value = false;
                                await nextTick();
                                startProcessingQueue();

                            } else if (data.status === 'FAILED') {
                                console.log(`Processing FAILED for ${fileItem.file.name} (ID: ${recordingId})`);
                                processingMessage.value = 'Processing failed.';
                                processingProgress.value = 100; // Show 100% but failed state
                                fileItem.status = 'failed'; // Final status for queue item
                                fileItem.error = data.transcription || data.summary || 'Processing failed on server.';
                                setGlobalError(`Processing failed for "${data.title || fileItem.file.name}".`);
                                // Stop polling, reset state, and trigger next item
                                clearInterval(pollInterval.value);
                                pollInterval.value = null;
                                resetCurrentFileProcessingState();
                                isProcessingActive.value = false;
                                await nextTick();
                                startProcessingQueue();

                            } else if (data.status === 'PROCESSING') {
                                processingMessage.value = 'Transcription in progress...';
                                processingProgress.value = Math.min(65, processingProgress.value + Math.random() * 5); // Mid-range progress
                            } else if (data.status === 'SUMMARIZING') {
                                processingMessage.value = 'Generating title & summary...';
                                processingProgress.value = Math.min(95, processingProgress.value + Math.random() * 5); // Higher progress
                            } else { // PENDING
                                processingMessage.value = 'Waiting in queue...';
                                processingProgress.value = 45; // Keep progress indication while pending
                            }
                        } catch (error) {
                            console.error(`Polling Error for ${fileItem.file.name} (ID: ${recordingId}):`, error);
                             // Assume failed if polling error occurs
                            fileItem.status = 'failed';
                            fileItem.error = `Error checking status: ${error.message}`;
                            setGlobalError(`Error checking status for "${fileItem.file.name}": ${error.message}.`);
                             // Update record in gallery if found
                             const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);
                            if (galleryIndex !== -1) recordings.value[galleryIndex].status = 'FAILED';

                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            await nextTick();
                            startProcessingQueue(); // Process the next file
                        }
                    }, 5000); // Poll every 5 seconds (adjust as needed)
                };

                // --- Gallery and Detail Methods ---
                // AutoSave removed in favor of explicit save in modal
                // const autoSave = () => { ... };

                 const saveMetadata = async (recordingDataToSave) => {
                    globalError.value = null;
                    if (!recordingDataToSave || !recordingDataToSave.id) return null;
                    console.log('Saving metadata for:', recordingDataToSave.id);
                    try {
                        const payload = {
                            id: recordingDataToSave.id,
                            title: recordingDataToSave.title,
                            participants: recordingDataToSave.participants,
                            notes: recordingDataToSave.notes,
                            summary: recordingDataToSave.summary, // <-- ADDED: Include summary
                            meeting_date: recordingDataToSave.meeting_date // <-- ADDED: Include meeting_date (should be YYYY-MM-DD)
                        };
                        const response = await fetch('/save', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });
                        const data = await response.json();
                        if (!response.ok) throw new Error(data.error || 'Failed to save metadata');

                        console.log('Save successful:', data.recording.id);
                        // Update the master recordings list
                        const index = recordings.value.findIndex(r => r.id === data.recording.id);
                        if (index !== -1) {
                            // Update only the editable fields, preserve others like status/transcription from backend
                             recordings.value[index].title = payload.title;
                             recordings.value[index].participants = payload.participants;
                             recordings.value[index].notes = payload.notes;
                             recordings.value[index].notes_html = data.recording.notes_html;
                             recordings.value[index].summary = payload.summary;
                             recordings.value[index].summary_html = data.recording.summary_html;
                             recordings.value[index].meeting_date = payload.meeting_date; // <-- ADDED: Update meeting_date
                         }
                         // Update selected if it's the one being saved
                         if (selectedRecording.value?.id === data.recording.id) {
                            selectedRecording.value.title = payload.title;
                            selectedRecording.value.participants = payload.participants;
                            selectedRecording.value.notes = payload.notes;
                            selectedRecording.value.notes_html = data.recording.notes_html;
                            selectedRecording.value.summary = payload.summary;
                            selectedRecording.value.summary_html = data.recording.summary_html;
                            selectedRecording.value.meeting_date = payload.meeting_date; // <-- ADDED: Update meeting_date
                         }
                        return data.recording; // Return the full updated object from backend
                    } catch (error) {
                        console.error('Save Metadata Error:', error);
                        setGlobalError(`Save failed: ${error.message}`);
                        return null;
                    }
                };

                const loadRecordings = async () => {
                    globalError.value = null;
                    isLoadingRecordings.value = true;
                    try {
                        const response = await fetch('/recordings');
                        const data = await response.json();
                        if (!response.ok) throw new Error(data.error || 'Failed to load recordings');
                        recordings.value = data;


                        // On load, check for any recordings stuck in PENDING, PROCESSING, or SUMMARIZING
                        // and ensure polling is active if the queue processor isn't running.
                        const incompleteRecordings = recordings.value.filter(r => ['PENDING', 'PROCESSING', 'SUMMARIZING'].includes(r.status));

                        if (incompleteRecordings.length > 0 && !isProcessingActive.value) {
                            console.warn(`Found ${incompleteRecordings.length} incomplete recording(s) on load. Attempting to resume polling if not already queued.`);
                            for (const recording of incompleteRecordings) {
                                // Is this item already being tracked in the queue?
                                let queueItem = uploadQueue.value.find(item => item.recordingId === recording.id);
                                if (!queueItem) {
                                    // If not in queue (likely page reload), create a placeholder to track polling
                                    console.log(`Re-attaching poll for recording ${recording.id}.`);
                                    queueItem = {
                                        // Mock file object, size might be useful for display
                                        file: { name: recording.title || `Recording ${recording.id}`, size: recording.file_size },
                                        status: recording.status, // Use current status from DB
                                        recordingId: recording.id,
                                        clientId: `reload-${recording.id}`, // Unique ID for tracking
                                        error: null
                                    };
                                    // Add to the *beginning* of the visual queue *if* we want to show it there
                                    // Or just manage polling without adding to queue visually?
                                    // Let's add it to the queue for consistency in management.
                                    uploadQueue.value.unshift(queueItem);
                                    // Start processing queue if it wasn't active
                                    if (!isProcessingActive.value) {
                                        startProcessingQueue();
                                    } else if (currentlyProcessingFile.value?.recordingId !== recording.id) {
                                        // If processor is active but on a different file, just ensure polling starts
                                        // Note: startProcessingQueue handles finding the next 'queued'. This item
                                        // might not be 'queued'. We need direct polling.
                                        // Let's rethink: The queue processor should handle finding PENDING/PROCESSING items.
                                        // For simplicity, let's just trigger the queue processor. It will find this item if it's next.
                                        // If something else is truly processing, this won't interrupt.
                                        console.log("Triggering queue processor check due to reloaded incomplete item.")
                                        // Ensure it's marked conceptually 'queued' for the processor to pick up
                                        queueItem.status = 'queued';
                                        startProcessingQueue(); // Let the queue logic handle it
                                    }

                                } else if (queueItem.status !== recording.status && !['completed', 'failed'].includes(queueItem.status)) {
                                    // If queue status differs from DB, update queue status
                                    console.log(`Correcting queue status for ${queueItem.clientId} from ${queueItem.status} to ${recording.status}`);
                                    queueItem.status = recording.status;
                                    // Restart queue processing if needed
                                    if (!isProcessingActive.value) startProcessingQueue();
                                }
                            }
                        }

                    } catch (error) {
                        console.error('Load Recordings Error:', error);
                        setGlobalError(`Failed to load recordings: ${error.message}`);
                        recordings.value = [];
                    } finally {
                        isLoadingRecordings.value = false;
                    }
                };

                 const selectRecording = (recording) => {
                     selectedRecording.value = recording;
                     // Optional: Check if polling needs to be restarted if user selects an incomplete item
                     // This logic is complex and might be redundant with the loadRecordings check.
                     // Let's rely on loadRecordings and the queue processor for robustness.
                 };

                const editRecording = (recording) => {
                     // Create a deep copy for the modal to prevent modifying original object directly
                    editingRecording.value = JSON.parse(JSON.stringify(recording));
                    showEditModal.value = true;
                };

                const cancelEdit = () => {
                    showEditModal.value = false;
                    editingRecording.value = null;
                };

                 const saveEdit = async () => {
                    // Save using the saveMetadata function which updates the main list & selected item
                     const success = await saveMetadata(editingRecording.value);
                    if (success) {
                        cancelEdit(); // Close modal on success
                     }
                     // Keep modal open on failure, error shown via globalError
                };

                const confirmDelete = (recording) => {
                    recordingToDelete.value = recording;
                    showDeleteModal.value = true;
                };

                const cancelDelete = () => {
                    showDeleteModal.value = false;
                    recordingToDelete.value = null;
                };

                const deleteRecording = async () => {
                    globalError.value = null;
                    if (!recordingToDelete.value) return;
                    const idToDelete = recordingToDelete.value.id;
                    const titleToDelete = recordingToDelete.value.title; // For logging/messaging
                    try {
                        const response = await fetch(`/recording/${idToDelete}`, { method: 'DELETE' });
                        const data = await response.json();
                        if (!response.ok) throw new Error(data.error || 'Failed to delete recording');

                         // Remove from gallery list
                        recordings.value = recordings.value.filter(r => r.id !== idToDelete);

                        // Find and remove from upload queue
                        const queueIndex = uploadQueue.value.findIndex(item => item.recordingId === idToDelete);
                         if (queueIndex !== -1) {
                            const deletedItem = uploadQueue.value.splice(queueIndex, 1)[0];
                             console.log(`Removed item ${deletedItem.clientId} from queue.`);
                             // If deleting the file currently being processed, stop polling and move to next
                            if (currentlyProcessingFile.value?.clientId === deletedItem.clientId) {
                                console.log(`Deleting currently processing file: ${titleToDelete}. Stopping poll and moving to next.`);
                                clearInterval(pollInterval.value); // Stop polling explicitly
                                pollInterval.value = null;
                                resetCurrentFileProcessingState();
                                isProcessingActive.value = false; // Allow queue to restart
                                await nextTick();
                                startProcessingQueue();
                            }
                        }

                        if (selectedRecording.value?.id === idToDelete) selectedRecording.value = null;
                        cancelDelete();
                        console.log(`Successfully deleted recording ${idToDelete} (${titleToDelete})`);

                    } catch (error) {
                        console.error('Delete Error:', error);
                        setGlobalError(`Failed to delete recording "${titleToDelete}": ${error.message}`);
                        cancelDelete(); // Still close modal on error
                    }
                };
                
                // Inline editing methods
                const toggleEditParticipants = () => {
                    editingParticipants.value = !editingParticipants.value;
                    if (!editingParticipants.value) {
                        saveInlineEdit('participants');
                    }
                };
                
                const toggleEditMeetingDate = () => {
                    editingMeetingDate.value = !editingMeetingDate.value;
                    if (!editingMeetingDate.value) {
                        saveInlineEdit('meeting_date');
                    }
                };
                
                const toggleEditSummary = () => {
                    editingSummary.value = !editingSummary.value;
                    if (!editingSummary.value) {
                        saveInlineEdit('summary');
                    }
                };
                
                const toggleEditNotes = () => {
                    editingNotes.value = !editingNotes.value;
                    if (!editingNotes.value) {
                        saveInlineEdit('notes');
                    }
                };
                
                const saveInlineEdit = async (field) => {
                    if (!selectedRecording.value) return;
                    
                    // Create a payload with just the field being edited
                    const payload = {
                        id: selectedRecording.value.id,
                        [field]: selectedRecording.value[field]
                    };
                    
                    // For completeness, include all editable fields in the payload
                    // This ensures the backend has all the data it needs
                    const fullPayload = {
                        id: selectedRecording.value.id,
                        title: selectedRecording.value.title,
                        participants: selectedRecording.value.participants,
                        notes: selectedRecording.value.notes,
                        summary: selectedRecording.value.summary,
                        meeting_date: selectedRecording.value.meeting_date
                    };
                    
                    try {
                        const updatedRecording = await saveMetadata(fullPayload);
                        if (updatedRecording) {
                            // Update the HTML versions from the response
                            if (field === 'notes') {
                                selectedRecording.value.notes_html = updatedRecording.notes_html;
                            } else if (field === 'summary') {
                                selectedRecording.value.summary_html = updatedRecording.summary_html;
                            }
                            
                            // Reset the editing state for the field
                            switch(field) {
                                case 'participants':
                                    editingParticipants.value = false;
                                    break;
                                case 'meeting_date':
                                    editingMeetingDate.value = false;
                                    break;
                                case 'summary':
                                    editingSummary.value = false;
                                    break;
                                case 'notes':
                                    editingNotes.value = false;
                                    break;
                            }
                            showToast(`${field.charAt(0).toUpperCase() + field.slice(1).replace('_', ' ')} updated successfully`);
                        }
                    } catch (error) {
                        console.error(`Save ${field} Error:`, error);
                        setGlobalError(`Failed to save ${field}: ${error.message}`);
                    }
                };

                // --- Lifecycle Hooks ---
                onMounted(() => {
                    loadRecordings();
                    initializeDarkMode(); // Initialize dark mode on load
                });

                // --- Watchers ---
                 watch(uploadQueue, (newQueue, oldQueue) => {
                    if (newQueue.length === 0 && oldQueue.length > 0 && !isProcessingActive.value) {
                        console.log("Upload queue processing finished.");
                        // Auto-minimize after delay
                        setTimeout(() => progressPopupMinimized.value = true, 1000);
                        // Auto-hide popup after all uploads are complete
                        setTimeout(() => {
                            if (completedInQueue.value === totalInQueue.value && !isProcessingActive.value) {
                                progressPopupClosed.value = true;
                            }
                        }, 5000);
                     }
                 }, { deep: true });


                // --- Chat functionality ---
                const showChat = ref(false);
                const chatMessages = ref([]);
                const chatInput = ref('');
                const isChatLoading = ref(false);
                
                // Create a ref for the chat messages container
                const chatMessagesRef = ref(null);
                
                const sendChatMessage = async () => {
                    if (!chatInput.value.trim() || isChatLoading.value || !selectedRecording.value || selectedRecording.value.status !== 'COMPLETED') {
                        return;
                    }
                    
                    const message = chatInput.value.trim();

                    // Ensure chatMessages.value is an array before pushing
                    if (!Array.isArray(chatMessages.value)) {
                        console.warn('chatMessages.value was not an array! Resetting. Value was:', chatMessages.value);
                        chatMessages.value = []; // Reset if corrupted
                    }

                    chatMessages.value.push({ role: 'user', content: message });
                    chatInput.value = '';
                    isChatLoading.value = true;
                    
                    // Scroll to bottom of chat
                    await nextTick();
                    // Use the correctly named ref
                    if (chatMessagesRef.value) { 
                        chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
                    }
                    
                    try {
                        // Prepare message history for the API call
                        // We need to convert our UI messages to the format expected by the API
                        const messageHistory = chatMessages.value
                            .slice(0, -1) // Exclude the message we just added (it will be sent separately)
                            .map(msg => ({
                                role: msg.role,
                                content: msg.content
                            }));
                        
                        const response = await fetch('/chat', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                recording_id: selectedRecording.value.id,
                                message: message,
                                message_history: messageHistory
                            })
                        });
                        
                        const data = await response.json();
                        if (!response.ok) throw new Error(data.error || 'Failed to get chat response');
                        
                        chatMessages.value.push({ 
                            role: 'assistant', 
                            content: data.response,
                            html: data.response_html 
                        });
                    } catch (error) {
                        console.error('Chat Error:', error);
                        setGlobalError(`Chat error: ${error.message}`);
                        chatMessages.value.push({ role: 'assistant', content: `Error: ${error.message}` });
                    } finally {
                        isChatLoading.value = false;
                        // Scroll to bottom of chat
                        await nextTick();
                         // Use the correctly named ref
                        if (chatMessagesRef.value) {
                            chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
                        }
                    }
                };
                
                // Toast notification system
                const showToast = (message, icon = 'fa-check-circle', duration = 2000) => {
                    const toastContainer = document.getElementById('toastContainer');
                    
                    // Create toast element
                    const toast = document.createElement('div');
                    toast.className = 'toast';
                    toast.innerHTML = `<i class="fas ${icon}"></i> ${message}`;
                    
                    // Add to container
                    toastContainer.appendChild(toast);
                    
                    // Trigger animation
                    setTimeout(() => {
                        toast.classList.add('show');
                    }, 10);
                    
                    // Remove after duration
                    setTimeout(() => {
                        toast.classList.remove('show');
                        setTimeout(() => {
                            toastContainer.removeChild(toast);
                        }, 300);
                    }, duration);
                };
                
                // Add animation to copy button
                const animateCopyButton = (button) => {
                    button.classList.add('copy-success');
                    
                    // Store original content
                    const originalContent = button.innerHTML;
                    button.innerHTML = '<i class="fas fa-check"></i>';
                    
                    setTimeout(() => {
                        button.classList.remove('copy-success');
                        button.innerHTML = originalContent;
                    }, 1500);
                };
                
                const copyMessage = (text, event) => {
                    // Get the button that was clicked
                    const button = event.currentTarget;
                    
                    if (navigator.clipboard && window.isSecureContext) {
                        // Use clipboard API if available (secure context)
                        navigator.clipboard.writeText(text)
                            .then(() => {
                                showToast('Copied to clipboard!');
                                animateCopyButton(button);
                            })
                            .catch(err => {
                                console.error('Copy failed:', err);
                                showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                                fallbackCopyTextToClipboard(text, button);
                            });
                    } else {
                        // Fallback for non-secure contexts
                        fallbackCopyTextToClipboard(text, button);
                    }
                };
                
                // Fallback method using document.execCommand
                const fallbackCopyTextToClipboard = (text, button = null) => {
                    try {
                        const textArea = document.createElement("textarea");
                        textArea.value = text;
                        
                        // Make the textarea out of viewport
                        textArea.style.position = "fixed";
                        textArea.style.left = "-999999px";
                        textArea.style.top = "-999999px";
                        document.body.appendChild(textArea);
                        
                        // Select and copy
                        textArea.focus();
                        textArea.select();
                        const successful = document.execCommand('copy');
                        
                        // Clean up
                        document.body.removeChild(textArea);
                        
                        if (successful) {
                            showToast('Copied to clipboard!');
                            if (button) animateCopyButton(button);
                        } else {
                            showToast('Copy failed. Your browser may not support this feature.', 'fa-exclamation-circle');
                        }
                    } catch (err) {
                        console.error('Fallback copy failed:', err);
                        showToast('Unable to copy: ' + err.message, 'fa-exclamation-circle');
                    }
                };
                
                const copyTranscription = (event) => {
                    if (!selectedRecording.value || !selectedRecording.value.transcription) {
                        showToast('No transcription available to copy.', 'fa-exclamation-circle');
                        return;
                    }
                    
                    // Get the button that was clicked
                    const button = event.currentTarget;
                    
                    // Show visual feedback on button
                    animateCopyButton(button);
                    
                    if (navigator.clipboard && window.isSecureContext) {
                        // Use clipboard API if available (secure context)
                        navigator.clipboard.writeText(selectedRecording.value.transcription)
                            .then(() => {
                                showToast('Transcription copied to clipboard!');
                            })
                            .catch(err => {
                                console.error('Copy failed:', err);
                                showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                                fallbackCopyTextToClipboard(selectedRecording.value.transcription);
                            });
                    } else {
                        // Fallback for non-secure contexts
                        fallbackCopyTextToClipboard(selectedRecording.value.transcription);
                    }
                };
                
                // Clear chat messages when recording changes
                watch(selectedRecording, (newVal) => {
                    chatMessages.value = [];
                    showChat.value = false;
                    selectedTab.value = 'summary'; // Reset tab when recording changes
                });

                return {
                    // State
                    currentView, dragover, recordings, selectedRecording, // currentRecording removed
                    showEditModal, showDeleteModal, editingRecording, recordingToDelete,
                    isLoadingRecordings, globalError, maxFileSizeMB, isDarkMode, // <-- Added isDarkMode
                    // Multi-upload State
                    uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
                    isProcessingActive, progressPopupMinimized, progressPopupClosed,
                    // Chat State
                    showChat, chatMessages, chatInput, isChatLoading, chatMessagesRef, // <-- Added chatMessagesRef
                    // Computed
                    groupedRecordings, totalInQueue, completedInQueue, queuedFiles, finishedFilesInQueue,
                    // Inline editing state
                    editingParticipants, editingMeetingDate, editingSummary, editingNotes,
                    // Methods
                    handleDrop, handleFileSelect, /*autoSave removed*/ loadRecordings, selectedTab, // <-- Added selectedTab
                    selectRecording, editRecording, cancelEdit, saveEdit, confirmDelete,
                    cancelDelete, deleteRecording, switchToUploadView, switchToGalleryView,
                    formatFileSize, setGlobalError, handleDragLeave, formatStatus, getStatusClass,
                    formatDisplayDate, // <-- ADDED: Expose date formatting function
                    toggleDarkMode, // <-- Added toggleDarkMode
                    toggleSidebar, isSidebarCollapsed, // <-- Added sidebar state and function
                    // Inline editing methods
                    toggleEditParticipants, toggleEditMeetingDate, toggleEditSummary, toggleEditNotes, saveInlineEdit,
                    // Chat Methods
                    sendChatMessage, copyMessage, copyTranscription,
                    // User menu
                    isUserMenuOpen,
                 }
            },
            delimiters: ['${', '}'] // Keep Vue delimiters distinct from Flask's Jinja
        }).mount('#app');
    </script>
{% endraw %}
</body>
</html>

```


## templates/register.html

```html

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Speakr</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        /* Dark Mode CSS Variables */
        :root {
            /* Light mode variables */
            --bg-primary: #f3f4f6; /* gray-100 */
            --bg-secondary: #ffffff; /* white */
            --bg-tertiary: #f9fafb; /* gray-50 */
            --bg-accent: #dbeafe; /* blue-100 */
            --bg-accent-hover: #bfdbfe; /* blue-200 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #1d4ed8; /* blue-700 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #b91c1c; /* red-700 */
            --bg-danger-light: #fee2e2; /* red-100 */
            --bg-info-light: #dbeafe; /* blue-100 */
            --bg-warn-light: #fef3c7; /* amber-100 */
            --bg-success-light: #d1fae5; /* green-100 */
            --bg-pending-light: #f5f5f4; /* stone-100 */
            --bg-input: #ffffff; /* white */

            --text-primary: #1f2937; /* gray-800 */
            --text-secondary: #374151; /* gray-700 */
            --text-muted: #6b7280; /* gray-500 */
            --text-light: #9ca3af; /* gray-400 */
            --text-accent: #1d4ed8; /* blue-700 */
            --text-button: #ffffff; /* white */
            --text-danger: #b91c1c; /* red-700 */
            --text-danger-strong: #991b1b; /* red-800 */
            --text-info-strong: #1e40af; /* blue-800 */
            --text-warn-strong: #92400e; /* amber-800 */
            --text-success-strong: #065f46; /* green-800 */
            --text-pending-strong: #44403c; /* stone-700 */

            --border-primary: #e5e7eb; /* gray-200 */
            --border-secondary: #d1d5db; /* gray-300 */
            --border-accent: #93c5fd; /* blue-300 */
            --border-danger: #f87171; /* red-400 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #bfdbfe; /* blue-200 */
        }

        .dark {
            /* Dark mode variables */
            --bg-primary: #111827; /* gray-900 */
            --bg-secondary: #1f2937; /* gray-800 */
            --bg-tertiary: #374151; /* gray-700 */
            --bg-accent: #1e3a8a; /* blue-900 */
            --bg-accent-hover: #1e40af; /* blue-800 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #3b82f6; /* blue-500 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #ef4444; /* red-500 */
            --bg-danger-light: #7f1d1d; /* red-900 */
            --bg-info-light: #1e3a8a; /* blue-900 */
            --bg-warn-light: #78350f; /* amber-900 */
            --bg-success-light: #064e3b; /* green-900 */
            --bg-pending-light: #292524; /* stone-800 */
            --bg-input: #374151; /* gray-700 */

            --text-primary: #f3f4f6; /* gray-100 */
            --text-secondary: #d1d5db; /* gray-300 */
            --text-muted: #9ca3af; /* gray-400 */
            --text-light: #6b7280; /* gray-500 */
            --text-accent: #60a5fa; /* blue-400 */
            --text-button: #ffffff; /* white */
            --text-danger: #f87171; /* red-400 */
            --text-danger-strong: #fca5a5; /* red-300 */
            --text-info-strong: #93c5fd; /* blue-300 */
            --text-warn-strong: #fcd34d; /* amber-300 */
            --text-success-strong: #6ee7b7; /* green-300 */
            --text-pending-strong: #d6d3d1; /* stone-300 */

            --border-primary: #374151; /* gray-700 */
            --border-secondary: #4b5563; /* gray-600 */
            --border-accent: #1d4ed8; /* blue-700 */
            --border-danger: #ef4444; /* red-500 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #1e40af; /* blue-800 */
        }

        html {
            background-color: var(--bg-primary); 
            color: var(--text-primary); 
            transition: background-color 0.3s, color 0.3s; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        html, body { height: 100%; margin: 0; }
    </style>
</head>
<body class="bg-[var(--bg-primary)] text-[var(--text-primary)]">
    <div class="container mx-auto px-4 sm:px-6 lg:px-8 py-6 flex flex-col min-h-screen">
        <header class="flex justify-between items-center mb-6 pb-4 border-b border-[var(--border-primary)]">
            <h1 class="text-3xl font-bold text-[var(--text-primary)]">
                <a href="{{ url_for('index') }}">Speakr</a>
            </h1>
        </header>

        <main class="flex-grow flex items-center justify-center">
            <div class="w-full max-w-md bg-[var(--bg-secondary)] p-8 rounded-xl shadow-lg border border-[var(--border-primary)]">
                <h2 class="text-2xl font-semibold text-[var(--text-primary)] mb-6 text-center">Create an Account</h2>
                
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="mb-4 p-3 rounded-lg {% if category == 'success' %}bg-[var(--bg-success-light)] text-[var(--text-success-strong)]{% elif category == 'danger' %}bg-[var(--bg-danger-light)] text-[var(--text-danger-strong)]{% else %}bg-[var(--bg-info-light)] text-[var(--text-info-strong)]{% endif %}">
                                {{ message }}
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                <form method="POST" action="{{ url_for('register') }}">
                    {{ form.hidden_tag() }}
                    
                    <div class="mb-4">
                        {{ form.username.label(class="block text-sm font-medium text-[var(--text-secondary)] mb-1") }}
                        {% if form.username.errors %}
                            {{ form.username(class="mt-1 block w-full rounded-md border-[var(--border-danger)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                            <div class="text-[var(--text-danger)] text-xs mt-1">
                                {% for error in form.username.errors %}
                                    <span>{{ error }}</span>
                                {% endfor %}
                            </div>
                        {% else %}
                            {{ form.username(class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                        {% endif %}
                    </div>
                    
                    <div class="mb-4">
                        {{ form.email.label(class="block text-sm font-medium text-[var(--text-secondary)] mb-1") }}
                        {% if form.email.errors %}
                            {{ form.email(class="mt-1 block w-full rounded-md border-[var(--border-danger)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                            <div class="text-[var(--text-danger)] text-xs mt-1">
                                {% for error in form.email.errors %}
                                    <span>{{ error }}</span>
                                {% endfor %}
                            </div>
                        {% else %}
                            {{ form.email(class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                        {% endif %}
                    </div>
                    
                    <div class="mb-4">
                        {{ form.password.label(class="block text-sm font-medium text-[var(--text-secondary)] mb-1") }}
                        {% if form.password.errors %}
                            {{ form.password(class="mt-1 block w-full rounded-md border-[var(--border-danger)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                            <div class="text-[var(--text-danger)] text-xs mt-1">
                                {% for error in form.password.errors %}
                                    <span>{{ error }}</span>
                                {% endfor %}
                            </div>
                        {% else %}
                            {{ form.password(class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                        {% endif %}
                        <p class="text-xs text-[var(--text-muted)] mt-1">Password must be at least 8 characters long.</p>
                    </div>
                    
                    <div class="mb-6">
                        {{ form.confirm_password.label(class="block text-sm font-medium text-[var(--text-secondary)] mb-1") }}
                        {% if form.confirm_password.errors %}
                            {{ form.confirm_password(class="mt-1 block w-full rounded-md border-[var(--border-danger)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                            <div class="text-[var(--text-danger)] text-xs mt-1">
                                {% for error in form.confirm_password.errors %}
                                    <span>{{ error }}</span>
                                {% endfor %}
                            </div>
                        {% else %}
                            {{ form.confirm_password(class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                        {% endif %}
                    </div>
                    
                    <div class="flex flex-col space-y-4">
                        {{ form.submit(class="w-full py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-[var(--text-button)] bg-[var(--bg-button)] hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[var(--border-focus)]") }}
                        
                        <div class="text-center text-sm text-[var(--text-muted)]">
                            <span>Already have an account?</span>
                            <a href="{{ url_for('login') }}" class="font-medium text-[var(--text-accent)] hover:underline">Login here</a>
                        </div>
                    </div>
                </form>
            </div>
        </main>

        <footer class="text-center py-4 mt-8 text-xs text-[var(--text-light)] border-t border-[var(--border-primary)]">
            Speakr &copy; {{ now.year }}
        </footer>
    </div>
</body>
</html>

```


## templates/login.html

```html

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Speakr</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        /* Dark Mode CSS Variables */
        :root {
            /* Light mode variables */
            --bg-primary: #f3f4f6; /* gray-100 */
            --bg-secondary: #ffffff; /* white */
            --bg-tertiary: #f9fafb; /* gray-50 */
            --bg-accent: #dbeafe; /* blue-100 */
            --bg-accent-hover: #bfdbfe; /* blue-200 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #1d4ed8; /* blue-700 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #b91c1c; /* red-700 */
            --bg-danger-light: #fee2e2; /* red-100 */
            --bg-info-light: #dbeafe; /* blue-100 */
            --bg-warn-light: #fef3c7; /* amber-100 */
            --bg-success-light: #d1fae5; /* green-100 */
            --bg-pending-light: #f5f5f4; /* stone-100 */
            --bg-input: #ffffff; /* white */

            --text-primary: #1f2937; /* gray-800 */
            --text-secondary: #374151; /* gray-700 */
            --text-muted: #6b7280; /* gray-500 */
            --text-light: #9ca3af; /* gray-400 */
            --text-accent: #1d4ed8; /* blue-700 */
            --text-button: #ffffff; /* white */
            --text-danger: #b91c1c; /* red-700 */
            --text-danger-strong: #991b1b; /* red-800 */
            --text-info-strong: #1e40af; /* blue-800 */
            --text-warn-strong: #92400e; /* amber-800 */
            --text-success-strong: #065f46; /* green-800 */
            --text-pending-strong: #44403c; /* stone-700 */

            --border-primary: #e5e7eb; /* gray-200 */
            --border-secondary: #d1d5db; /* gray-300 */
            --border-accent: #93c5fd; /* blue-300 */
            --border-danger: #f87171; /* red-400 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #bfdbfe; /* blue-200 */
        }

        .dark {
            /* Dark mode variables */
            --bg-primary: #111827; /* gray-900 */
            --bg-secondary: #1f2937; /* gray-800 */
            --bg-tertiary: #374151; /* gray-700 */
            --bg-accent: #1e3a8a; /* blue-900 */
            --bg-accent-hover: #1e40af; /* blue-800 */
            --bg-button: #2563eb; /* blue-600 */
            --bg-button-hover: #3b82f6; /* blue-500 */
            --bg-danger: #dc2626; /* red-600 */
            --bg-danger-hover: #ef4444; /* red-500 */
            --bg-danger-light: #7f1d1d; /* red-900 */
            --bg-info-light: #1e3a8a; /* blue-900 */
            --bg-warn-light: #78350f; /* amber-900 */
            --bg-success-light: #064e3b; /* green-900 */
            --bg-pending-light: #292524; /* stone-800 */
            --bg-input: #374151; /* gray-700 */

            --text-primary: #f3f4f6; /* gray-100 */
            --text-secondary: #d1d5db; /* gray-300 */
            --text-muted: #9ca3af; /* gray-400 */
            --text-light: #6b7280; /* gray-500 */
            --text-accent: #60a5fa; /* blue-400 */
            --text-button: #ffffff; /* white */
            --text-danger: #f87171; /* red-400 */
            --text-danger-strong: #fca5a5; /* red-300 */
            --text-info-strong: #93c5fd; /* blue-300 */
            --text-warn-strong: #fcd34d; /* amber-300 */
            --text-success-strong: #6ee7b7; /* green-300 */
            --text-pending-strong: #d6d3d1; /* stone-300 */

            --border-primary: #374151; /* gray-700 */
            --border-secondary: #4b5563; /* gray-600 */
            --border-accent: #1d4ed8; /* blue-700 */
            --border-danger: #ef4444; /* red-500 */
            --border-focus: #3b82f6; /* blue-500 */
            --ring-focus: #1e40af; /* blue-800 */
        }

        html {
            background-color: var(--bg-primary); 
            color: var(--text-primary); 
            transition: background-color 0.3s, color 0.3s; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        html, body { height: 100%; margin: 0; }
    </style>
</head>
<body class="bg-[var(--bg-primary)] text-[var(--text-primary)]">
    <div class="container mx-auto px-4 sm:px-6 lg:px-8 py-6 flex flex-col min-h-screen">
        <header class="flex justify-between items-center mb-6 pb-4 border-b border-[var(--border-primary)]">
            <h1 class="text-3xl font-bold text-[var(--text-primary)]">
                <a href="{{ url_for('index') }}">Speakr</a>
            </h1>
        </header>

        <main class="flex-grow flex items-center justify-center">
            <div class="w-full max-w-md bg-[var(--bg-secondary)] p-8 rounded-xl shadow-lg border border-[var(--border-primary)]">
                <h2 class="text-2xl font-semibold text-[var(--text-primary)] mb-6 text-center">Login</h2>
                
                {% with messages = get_flashed_messages(with_categories=true) %}
                    {% if messages %}
                        {% for category, message in messages %}
                            <div class="mb-4 p-3 rounded-lg {% if category == 'success' %}bg-[var(--bg-success-light)] text-[var(--text-success-strong)]{% elif category == 'danger' %}bg-[var(--bg-danger-light)] text-[var(--text-danger-strong)]{% else %}bg-[var(--bg-info-light)] text-[var(--text-info-strong)]{% endif %}">
                                {{ message }}
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                <form method="POST" action="{{ url_for('login') }}">
                    {{ form.hidden_tag() }}
                    
                    <div class="mb-4">
                        {{ form.email.label(class="block text-sm font-medium text-[var(--text-secondary)] mb-1") }}
                        {% if form.email.errors %}
                            {{ form.email(class="mt-1 block w-full rounded-md border-[var(--border-danger)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                            <div class="text-[var(--text-danger)] text-xs mt-1">
                                {% for error in form.email.errors %}
                                    <span>{{ error }}</span>
                                {% endfor %}
                            </div>
                        {% else %}
                            {{ form.email(class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                        {% endif %}
                    </div>
                    
                    <div class="mb-6">
                        {{ form.password.label(class="block text-sm font-medium text-[var(--text-secondary)] mb-1") }}
                        {% if form.password.errors %}
                            {{ form.password(class="mt-1 block w-full rounded-md border-[var(--border-danger)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                            <div class="text-[var(--text-danger)] text-xs mt-1">
                                {% for error in form.password.errors %}
                                    <span>{{ error }}</span>
                                {% endfor %}
                            </div>
                        {% else %}
                            {{ form.password(class="mt-1 block w-full rounded-md border-[var(--border-secondary)] shadow-sm focus:border-[var(--border-focus)] focus:ring-[var(--ring-focus)] focus:ring-opacity-50 bg-[var(--bg-input)] text-[var(--text-primary)]") }}
                        {% endif %}
                    </div>
                    
                    <div class="flex items-center mb-6">
                        {{ form.remember(class="h-4 w-4 text-[var(--text-accent)] focus:ring-[var(--ring-focus)] border-[var(--border-secondary)] rounded") }}
                        {{ form.remember.label(class="ml-2 block text-sm text-[var(--text-secondary)]") }}
                    </div>
                    
                    <div class="flex flex-col space-y-4">
                        {{ form.submit(class="w-full py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-[var(--text-button)] bg-[var(--bg-button)] hover:bg-[var(--bg-button-hover)] focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[var(--border-focus)]") }}
                        
                        <div class="text-center text-sm text-[var(--text-muted)]">
                            <span>Don't have an account?</span>
                            <a href="{{ url_for('register') }}" class="font-medium text-[var(--text-accent)] hover:underline">Register here</a>
                        </div>
                    </div>
                </form>
            </div>
        </main>

        <footer class="text-center py-4 mt-8 text-xs text-[var(--text-light)] border-t border-[var(--border-primary)]">
            Speakr &copy; {{ now.year }}
        </footer>
    </div>
</body>
</html>

```


## deployment/setup.sh

```bash

#!/bin/bash

# Create directory for the application
sudo systemctl stop transcription

sudo mkdir -p /opt/transcription-app
sudo chown $USER:$USER /opt/transcription-app

# Copy application files
cp app.py /opt/transcription-app/
cp -r templates /opt/transcription-app/
cp requirements.txt /opt/transcription-app/
cp reset_db.py /opt/transcription-app/
cp create_admin.py /opt/transcription-app/
cp .env /opt/transcription-app/  # Copy the .env file with API keys

# Add SECRET_KEY to .env file if it doesn't exist
if ! grep -q "SECRET_KEY" /opt/transcription-app/.env; then
    echo "SECRET_KEY=$(openssl rand -hex 32)" >> /opt/transcription-app/.env
    echo "Added SECRET_KEY to .env file"
fi

# Create and activate virtual environment
python3 -m venv /opt/transcription-app/venv
source /opt/transcription-app/venv/bin/activate

# Install requirements
cd /opt/transcription-app
pip install -r requirements.txt

# Create directories for uploads and database with proper permissions
mkdir -p /opt/transcription-app/uploads
mkdir -p /opt/transcription-app/instance
chmod 755 /opt/transcription-app/uploads
chmod 755 /opt/transcription-app/instance

# Initialize or migrate the database
if [ ! -f /opt/transcription-app/instance/transcriptions.db ]; then
    # If database doesn't exist, create it from scratch
    echo "Database doesn't exist. Creating new database..."
    python reset_db.py
else
    # If database exists, migrate it to preserve data
    echo "Database exists. Migrating schema to preserve data..."
    python migrate_db.py
fi

# Set proper ownership for all files
sudo chown -R $USER:$USER /opt/transcription-app

# Create systemd service file
sudo tee /etc/systemd/system/transcription.service << EOF
[Unit]
Description=Transcription Web Application
After=network.target

[Service]
User=$USER
EnvironmentFile=/opt/transcription-app/.env
WorkingDirectory=/opt/transcription-app
Environment="PATH=/opt/transcription-app/venv/bin"
Environment="PYTHONPATH=/opt/transcription-app"
ExecStart=/opt/transcription-app/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:8899 --timeout 600 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd and start service
sudo systemctl daemon-reload
sudo systemctl restart transcription
sudo systemctl enable transcription

# Check service status
echo "Checking service status..."
sleep 3
sudo systemctl status transcription

echo "Installation complete! The application should be running on port 8899."

# Ask if user wants to create an admin user
read -p "Do you want to create an admin user now? (y/n): " create_admin
if [[ $create_admin == "y" || $create_admin == "Y" ]]; then
    echo "Creating admin user..."
    python create_admin.py
else
    echo "You can create an admin user later by running: python create_admin.py"
fi

```
