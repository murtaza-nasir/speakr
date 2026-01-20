"""
Authentication and user management routes.

This blueprint handles user registration, login, logout, account management,
and password changes.
"""

import os
import re
import mimetypes
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, current_app
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
from src.auth.sso import (
    init_sso_client,
    is_sso_enabled,
    get_sso_config,
    get_sso_client,
    create_or_update_sso_user,
    is_domain_allowed,
    link_sso_to_existing_user,
    update_user_profile_from_claims,
)

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


def is_registration_domain_allowed(email: str) -> bool:
    """Check if email domain is allowed for registration.

    Returns True if no domain restrictions are configured or if the
    email domain is in the allowed list.
    """
    if not email:
        return False

    domains_env = os.environ.get('REGISTRATION_ALLOWED_DOMAINS', '')
    if not domains_env or not domains_env.strip():
        return True  # No restriction configured

    allowed = [d.strip().lower() for d in domains_env.split(',') if d.strip()]
    if not allowed:
        return True  # Empty after parsing

    parts = email.lower().rsplit('@', 1)
    if len(parts) != 2:
        return False  # Invalid email format

    domain = parts[1]
    return domain in allowed


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
        # Check if email domain is allowed
        if not is_registration_domain_allowed(form.email.data):
            flash('Registration is restricted. Please contact the administrator.', 'danger')
            return render_template('register.html', title='Register', form=form)

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

    sso_enabled = is_sso_enabled()
    sso_config = get_sso_config()
    if sso_enabled:
        init_sso_client(current_app)

    password_login_disabled = sso_enabled and sso_config.get('disable_password_login', False)

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.password:
            # Check if password login is disabled for non-admins
            if password_login_disabled and not user.is_admin:
                flash('Password login is disabled. Please sign in with SSO.', 'warning')
            elif bcrypt.check_password_hash(user.password, form.password.data):
                login_user(user, remember=form.remember.data)
                next_page = request.args.get('next')
                if not is_safe_url(next_page):
                    return redirect(url_for('recordings.index'))
                return redirect(next_page) if next_page else redirect(url_for('recordings.index'))
            else:
                flash('Login unsuccessful. Please check email and password.', 'danger')
        elif user and not user.password:
            flash('This account uses SSO login. Please sign in with SSO.', 'warning')
        else:
            flash('Login unsuccessful. Please check email and password.', 'danger')

    return render_template(
        'login.html',
        title='Login',
        form=form,
        sso_enabled=sso_enabled,
        sso_provider_name=sso_config.get('provider_name', 'SSO'),
        password_login_disabled=password_login_disabled
    )


@auth_bp.route('/auth/sso/login')
@rate_limit("10 per minute")
def sso_login():
    if not is_sso_enabled():
        flash('SSO is not configured. Please contact the administrator.', 'danger')
        return redirect(url_for('auth.login'))

    oauth = get_sso_client() or init_sso_client(current_app)
    if not oauth:
        flash('Failed to initialize SSO client. Check server logs.', 'danger')
        return redirect(url_for('auth.login'))

    next_url = request.args.get('next')
    if next_url and is_safe_url(next_url):
        session['sso_next'] = next_url
    else:
        session.pop('sso_next', None)

    return oauth.sso.authorize_redirect(redirect_uri=get_sso_config().get('redirect_uri'))


@auth_bp.route('/auth/sso/callback')
@rate_limit("20 per minute")
def sso_callback():
    if not is_sso_enabled():
        flash('SSO is not configured. Please contact the administrator.', 'danger')
        return redirect(url_for('auth.login'))

    oauth = get_sso_client() or init_sso_client(current_app)
    if not oauth:
        flash('Failed to initialize SSO client. Check server logs.', 'danger')
        return redirect(url_for('auth.login'))

    try:
        token = oauth.sso.authorize_access_token()
        userinfo = token.get('userinfo') or oauth.sso.userinfo()
    except Exception as e:
        current_app.logger.warning(f"SSO callback error: {e}")
        flash('SSO login failed. Please try again.', 'danger')
        return redirect(url_for('auth.login'))

    subject = userinfo.get('sub')
    if not subject:
        flash('SSO response did not include a subject identifier.', 'danger')
        return redirect(url_for('auth.login'))

    link_user_id = session.pop('sso_link_user_id', None)
    next_url = session.pop('sso_next', None)
    cfg = get_sso_config()

    if link_user_id:
        target_user = db.session.get(User, int(link_user_id))
        if not target_user:
            flash('Could not link account: user not found.', 'danger')
            return redirect(url_for('auth.account'))

        existing = User.query.filter_by(sso_subject=subject).first()
        if existing and existing.id != target_user.id:
            flash('This SSO account is already linked to another user.', 'danger')
            return redirect(url_for('auth.account'))

        update_user_profile_from_claims(target_user, userinfo)
        target_user.sso_provider = cfg.get('provider_name', 'SSO')
        target_user.sso_subject = subject
        db.session.commit()
        flash('SSO account linked successfully.', 'success')
        return redirect(url_for('auth.account'))

    try:
        user = create_or_update_sso_user(userinfo)
    except PermissionError as e:
        flash(str(e), 'danger')
        return redirect(url_for('auth.login'))
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('auth.login'))
    except Exception as e:
        current_app.logger.warning(f"SSO login error: {e}")
        flash('Could not complete SSO login. Please try again.', 'danger')
        return redirect(url_for('auth.login'))

    login_user(user, remember=True)

    if next_url and is_safe_url(next_url):
        return redirect(next_url)
    return redirect(url_for('recordings.index'))


@auth_bp.route('/auth/sso/link', methods=['POST'])
@login_required
def sso_link():
    if not is_sso_enabled():
        flash('SSO is not configured. Please contact the administrator.', 'danger')
        return redirect(url_for('auth.account'))

    session['sso_link_user_id'] = current_user.id
    session['sso_next'] = url_for('auth.account')

    return redirect(url_for('auth.sso_login'))


@auth_bp.route('/auth/sso/unlink', methods=['POST'])
@login_required
def sso_unlink():
    if not current_user.sso_subject:
        flash('Your account is not linked to SSO.', 'warning')
        return redirect(url_for('auth.account'))

    if not current_user.password:
        flash('Cannot unlink SSO - you have no password set. Please set a password first.', 'danger')
        return redirect(url_for('auth.account'))

    current_user.sso_provider = None
    current_user.sso_subject = None
    db.session.commit()
    flash('SSO account unlinked successfully.', 'success')
    return redirect(url_for('auth.account'))


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
    USE_NEW_TRANSCRIPTION_ARCHITECTURE = os.environ.get('USE_NEW_TRANSCRIPTION_ARCHITECTURE', 'true').lower() == 'true'
    ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
    ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'

    # Get connector diarization support (new architecture)
    connector_supports_diarization = USE_ASR_ENDPOINT  # Default to USE_ASR_ENDPOINT for backwards compat
    if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
        try:
            from src.services.transcription import get_registry
            registry = get_registry()
            connector = registry.get_active_connector()
            if connector:
                connector_supports_diarization = connector.supports_diarization
        except Exception as e:
            current_app.logger.warning(f"Could not get connector diarization support: {e}")

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

    sso_config = get_sso_config()
    sso_enabled = is_sso_enabled()
    if sso_enabled:
        init_sso_client(current_app)
    sso_linked = bool(current_user.sso_subject)

    password_login_disabled = sso_enabled and sso_config.get('disable_password_login', False)

    return render_template('account.html',
                           title='Account',
                           default_summary_prompt_text=default_summary_prompt_text,
                           use_asr_endpoint=USE_ASR_ENDPOINT,
                           connector_supports_diarization=connector_supports_diarization,
                           enable_auto_deletion=ENABLE_AUTO_DELETION,
                           enable_internal_sharing=ENABLE_INTERNAL_SHARING,
                           user_admin_groups=user_admin_groups,
                           asr_diarize_locked=asr_diarize_locked,
                           asr_diarize_env_value=ASR_DIARIZE,
                           is_team_admin=is_team_admin,
                           sso_enabled=sso_enabled,
                           sso_provider_name=sso_config.get('provider_name', 'SSO'),
                           sso_linked=sso_linked,
                           sso_subject=current_user.sso_subject,
                           has_password=bool(current_user.password),
                           password_login_disabled=password_login_disabled)


@auth_bp.route('/change_password', methods=['POST'])
@login_required
@rate_limit("10 per minute")
def change_password():
    # Check if password management is disabled for non-admins
    sso_config = get_sso_config()
    password_login_disabled = is_sso_enabled() and sso_config.get('disable_password_login', False)
    if password_login_disabled and not current_user.is_admin:
        flash('Password management is disabled. Please use SSO to sign in.', 'warning')
        return redirect(url_for('auth.account'))

    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    # Check if user has an existing password
    has_existing_password = bool(current_user.password)

    # Validate form data - current password only required if user has one
    if has_existing_password and not current_password:
        flash('Current password is required.', 'danger')
        return redirect(url_for('auth.account'))

    if not new_password or not confirm_password:
        flash('New password and confirmation are required.', 'danger')
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

    # Verify current password only if user has one
    if has_existing_password:
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
