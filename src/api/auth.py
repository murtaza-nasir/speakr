"""
Authentication and user management routes.

This blueprint handles user registration, login, logout, account management,
and password changes.
"""

import os
import re
import mimetypes
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlparse, urljoin
import markdown

from src.database import db
from src.models import User, SystemSetting, GroupMembership
from src.utils import password_check

# Create blueprint
auth_bp = Blueprint('auth', __name__)

# Import these from app after initialization
bcrypt = None
csrf = None
limiter = None

def init_auth_extensions(_bcrypt, _csrf, _limiter):
    """Initialize extensions after app creation."""
    global bcrypt, csrf, limiter
    bcrypt = _bcrypt
    csrf = _csrf
    limiter = _limiter


def rate_limit(limit_string):
    """Decorator that applies rate limiting if limiter is available."""
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)
        # Store the limit string for later application
        wrapper._rate_limit = limit_string
        return wrapper
    return decorator


def csrf_exempt(f):
    """Decorator placeholder for CSRF exemption - applied after initialization."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    wrapper._csrf_exempt = True
    return wrapper


# --- Forms ---

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


# --- Helper Functions ---

def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc


# --- Routes ---

@auth_bp.route('/register', methods=['GET', 'POST'])
@rate_limit("10 per minute")
def register():
    # Check if registration is allowed
    allow_registration = os.environ.get('ALLOW_REGISTRATION', 'true').lower() == 'true'

    if not allow_registration:
        flash('Registration is currently disabled. Please contact the administrator.', 'danger')
        return redirect(url_for('auth.login'))

    if current_user.is_authenticated:
        return redirect(url_for('recordings.index'))

    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        user = User(username=form.username.data, email=form.email.data, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('register.html', title='Register', form=form)


@auth_bp.route('/login', methods=['GET', 'POST'])
@rate_limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('recordings.index'))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user, remember=form.remember.data)
            next_page = request.args.get('next')
            if not is_safe_url(next_page):
                return redirect(url_for('recordings.index'))
            return redirect(next_page) if next_page else redirect(url_for('recordings.index'))
        else:
            flash('Login unsuccessful. Please check email and password.', 'danger')

    return render_template('login.html', title='Login', form=form)


@auth_bp.route('/logout')
@csrf_exempt
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    # Import here to avoid circular imports
    from flask import current_app

    if request.method == 'POST':
        # Only update fields that are present in the form submission
        # This prevents clearing data when switching between tabs

        # Check if this is the account information form (has user_name field)
        if 'user_name' in request.form:
            # Handle personal information updates
            user_name = request.form.get('user_name')
            user_job_title = request.form.get('user_job_title')
            user_company = request.form.get('user_company')
            ui_lang = request.form.get('ui_language')
            transcription_lang = request.form.get('transcription_language')
            output_lang = request.form.get('output_language')

            current_user.name = user_name if user_name else None
            current_user.job_title = user_job_title if user_job_title else None
            current_user.company = user_company if user_company else None
            current_user.ui_language = ui_lang if ui_lang else 'en'
            current_user.transcription_language = transcription_lang if transcription_lang else None
            current_user.output_language = output_lang if output_lang else None

        # Check if this is the custom prompts form (has summary_prompt field)
        elif 'summary_prompt' in request.form:
            # Handle custom prompt updates
            summary_prompt_text = request.form.get('summary_prompt')
            current_user.summary_prompt = summary_prompt_text if summary_prompt_text else None
            # Handle event extraction setting
            current_user.extract_events = 'extract_events' in request.form

        # Only update diarize if it's not locked by env var
        if 'ASR_DIARIZE' not in os.environ:
            current_user.diarize = 'diarize' in request.form

        db.session.commit()

        # Return JSON response for AJAX requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json':
            return jsonify({'success': True, 'message': 'Account details updated successfully!'})

        # Regular form submission with redirect
        flash('Account details updated successfully!', 'success')

        # Preserve the active tab when redirecting
        if 'summary_prompt' in request.form:
            return redirect(url_for('auth.account') + '#prompts')
        else:
            return redirect(url_for('auth.account'))

    # Get admin default prompt from system settings
    admin_default_prompt = SystemSetting.get_setting('admin_default_summary_prompt', None)
    if admin_default_prompt:
        default_summary_prompt_text = admin_default_prompt
    else:
        # Fallback to hardcoded default if admin hasn't set one
        default_summary_prompt_text = """Generate a comprehensive summary that includes the following sections:
- **Key Issues Discussed**: A bulleted list of the main topics
- **Key Decisions Made**: A bulleted list of any decisions reached
- **Action Items**: A bulleted list of tasks assigned, including who is responsible if mentioned"""

    asr_diarize_locked = 'ASR_DIARIZE' in os.environ
    ASR_DIARIZE = os.environ.get('ASR_DIARIZE', 'false').lower() == 'true'
    USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
    ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
    ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'

    # Check if user is a team admin and get their admin groups
    admin_memberships = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).all()

    is_team_admin = len(admin_memberships) > 0

    # Build list of groups where user is admin (for tag assignment)
    user_admin_groups = []
    for membership in admin_memberships:
        if membership.group:
            user_admin_groups.append({
                'id': membership.group.id,
                'name': membership.group.name
            })

    return render_template('account.html',
                           title='Account',
                           default_summary_prompt_text=default_summary_prompt_text,
                           use_asr_endpoint=USE_ASR_ENDPOINT,
                           enable_auto_deletion=ENABLE_AUTO_DELETION,
                           enable_internal_sharing=ENABLE_INTERNAL_SHARING,
                           user_admin_groups=user_admin_groups,
                           asr_diarize_locked=asr_diarize_locked,
                           asr_diarize_env_value=ASR_DIARIZE,
                           is_team_admin=is_team_admin)


@auth_bp.route('/change_password', methods=['POST'])
@login_required
@rate_limit("10 per minute")
def change_password():
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    # Validate form data
    if not current_password or not new_password or not confirm_password:
        flash('All fields are required.', 'danger')
        return redirect(url_for('auth.account'))

    if new_password != confirm_password:
        flash('New password and confirmation do not match.', 'danger')
        return redirect(url_for('auth.account'))

    # Custom validation for new password
    try:
        password_check(None, type('obj', (object,), {'data': new_password}))
    except ValidationError as e:
        flash(str(e), 'danger')
        return redirect(url_for('auth.account'))

    # Verify current password
    if not bcrypt.check_password_hash(current_user.password, current_password):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('auth.account'))

    # Update password
    hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    current_user.password = hashed_password
    db.session.commit()

    flash('Your password has been updated!', 'success')
    return redirect(url_for('auth.account'))


@auth_bp.route('/docs/transcript-templates-guide')
def transcript_templates_guide():
    """Serve the transcript templates documentation."""
    from flask import current_app

    docs_path = os.path.join(current_app.root_path, '..', 'docs', 'transcript-templates-guide.md')

    if not os.path.exists(docs_path):
        return "Documentation not found", 404

    with open(docs_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Convert markdown to HTML
    html_content = markdown.markdown(content, extensions=['tables', 'fenced_code', 'codehilite'])

    # Wrap in basic HTML template with Speakr styling
    html_template = f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Transcript Templates Guide - Speakr</title>
        <link rel="stylesheet" href="/static/css/output.css">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
        <style>
            .markdown-body {{
                max-width: 900px;
                margin: 0 auto;
                padding: 2rem;
                line-height: 1.6;
            }}
            .markdown-body h1 {{ font-size: 2.5rem; margin-bottom: 1rem; }}
            .markdown-body h2 {{ font-size: 2rem; margin-top: 2rem; margin-bottom: 1rem; }}
            .markdown-body h3 {{ font-size: 1.5rem; margin-top: 1.5rem; margin-bottom: 0.75rem; }}
            .markdown-body pre {{ background: #f4f4f4; padding: 1rem; border-radius: 0.5rem; overflow-x: auto; }}
            .markdown-body code {{ background: #f4f4f4; padding: 0.2rem 0.4rem; border-radius: 0.25rem; }}
            .markdown-body pre code {{ background: none; padding: 0; }}
            .markdown-body ul, .markdown-body ol {{ margin-left: 2rem; margin-bottom: 1rem; }}
            .markdown-body li {{ margin-bottom: 0.5rem; }}
            .markdown-body blockquote {{ border-left: 4px solid #ddd; padding-left: 1rem; margin: 1rem 0; }}
            .markdown-body table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
            .markdown-body th, .markdown-body td {{ border: 1px solid #ddd; padding: 0.5rem; }}
            .markdown-body th {{ background: #f4f4f4; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="markdown-body">
            <a href="/" class="btn-primary" style="display: inline-block; margin-bottom: 1rem; padding: 0.5rem 1rem; background: #3b82f6; color: white; text-decoration: none; border-radius: 0.5rem;">← Back to App</a>
            {html_content}
        </div>
    </body>
    </html>
    '''

    return html_template
