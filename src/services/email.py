"""
Email service for verification and password reset.

This module provides email functionality using Python's built-in smtplib.
All email features are opt-in via environment variables.
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask import current_app, url_for

logger = logging.getLogger(__name__)

# Token expiry times
EMAIL_VERIFICATION_EXPIRY = 24 * 60 * 60  # 24 hours in seconds
PASSWORD_RESET_EXPIRY = 1 * 60 * 60  # 1 hour in seconds


def get_email_config():
    """Get email configuration from environment variables."""
    return {
        'enabled': os.environ.get('ENABLE_EMAIL_VERIFICATION', 'false').lower() == 'true',
        'required': os.environ.get('REQUIRE_EMAIL_VERIFICATION', 'false').lower() == 'true',
        'smtp_host': os.environ.get('SMTP_HOST', ''),
        'smtp_port': int(os.environ.get('SMTP_PORT', '587')),
        'smtp_username': os.environ.get('SMTP_USERNAME', ''),
        'smtp_password': os.environ.get('SMTP_PASSWORD', ''),
        'smtp_use_tls': os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true',
        'smtp_use_ssl': os.environ.get('SMTP_USE_SSL', 'false').lower() == 'true',
        'from_address': os.environ.get('SMTP_FROM_ADDRESS', 'noreply@yourdomain.com'),
        'from_name': os.environ.get('SMTP_FROM_NAME', 'Speakr'),
    }


def is_email_verification_enabled() -> bool:
    """Check if email verification is enabled."""
    return get_email_config()['enabled']


def is_email_verification_required() -> bool:
    """Check if email verification is required for login."""
    config = get_email_config()
    return config['enabled'] and config['required']


def is_smtp_configured() -> bool:
    """Check if SMTP settings are properly configured."""
    config = get_email_config()
    return bool(config['smtp_host'] and config['smtp_username'] and config['smtp_password'])


def get_serializer(salt: str) -> URLSafeTimedSerializer:
    """Get a URL-safe timed serializer for token generation."""
    secret_key = current_app.config.get('SECRET_KEY', 'default-dev-key')
    return URLSafeTimedSerializer(secret_key, salt=salt)


def generate_verification_token(user_id: int) -> str:
    """Generate an email verification token."""
    serializer = get_serializer('email-verification')
    return serializer.dumps(user_id)


def generate_password_reset_token(user_id: int) -> str:
    """Generate a password reset token."""
    serializer = get_serializer('password-reset')
    return serializer.dumps(user_id)


def verify_email_token(token: str) -> Optional[int]:
    """
    Verify an email verification token.

    Returns the user_id if valid, None otherwise.
    """
    serializer = get_serializer('email-verification')
    try:
        user_id = serializer.loads(token, max_age=EMAIL_VERIFICATION_EXPIRY)
        return user_id
    except SignatureExpired:
        logger.warning("Email verification token expired")
        return None
    except BadSignature:
        logger.warning("Invalid email verification token")
        return None


def verify_reset_token(token: str) -> Optional[int]:
    """
    Verify a password reset token.

    Returns the user_id if valid, None otherwise.
    """
    serializer = get_serializer('password-reset')
    try:
        user_id = serializer.loads(token, max_age=PASSWORD_RESET_EXPIRY)
        return user_id
    except SignatureExpired:
        logger.warning("Password reset token expired")
        return None
    except BadSignature:
        logger.warning("Invalid password reset token")
        return None


def _send_email(to_email: str, subject: str, html_body: str, text_body: str = None) -> bool:
    """
    Send an email using SMTP.

    Returns True if successful, False otherwise.
    """
    config = get_email_config()

    if not is_smtp_configured():
        logger.error("SMTP is not configured. Cannot send email.")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{config['from_name']} <{config['from_address']}>"
        msg['To'] = to_email

        # Add plain text version
        if text_body:
            part1 = MIMEText(text_body, 'plain')
            msg.attach(part1)

        # Add HTML version
        part2 = MIMEText(html_body, 'html')
        msg.attach(part2)

        # Connect to SMTP server
        if config['smtp_use_ssl']:
            server = smtplib.SMTP_SSL(config['smtp_host'], config['smtp_port'])
        else:
            server = smtplib.SMTP(config['smtp_host'], config['smtp_port'])
            if config['smtp_use_tls']:
                server.starttls()

        server.login(config['smtp_username'], config['smtp_password'])
        server.sendmail(config['from_address'], to_email, msg.as_string())
        server.quit()

        logger.info(f"Email sent successfully to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication failed: {e}")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error sending email: {e}")
        return False
    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return False


def _get_email_template(content_html: str, content_text: str, subject: str) -> tuple[str, str]:
    """
    Wrap content in the Speakr email template.

    Returns (html_body, text_body)
    """
    # Get the base URL for the logo
    try:
        logo_url = url_for('static', filename='img/icon-192x192.png', _external=True)
    except RuntimeError:
        # Outside of request context, use a placeholder
        logo_url = ""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #1f2937; margin: 0; padding: 0; background-color: #e8eaed;">
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #e8eaed;">
        <tr>
            <td style="padding: 40px 20px;">
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="600" style="max-width: 600px; margin: 0 auto;">
                    <!-- Header -->
                    <tr>
                        <td style="background-color: #2563eb; padding: 32px 40px; border-radius: 12px 12px 0 0;">
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                <tr>
                                    <td>
                                        <!-- Logo and Brand -->
                                        <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                                            <tr>
                                                <td style="vertical-align: middle; padding-right: 12px;">
                                                    <img src="{logo_url}" alt="Speakr" width="44" height="44" style="display: block; border-radius: 8px;">
                                                </td>
                                                <td style="vertical-align: middle;">
                                                    <h1 style="color: #ffffff; margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.5px;">Speakr</h1>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding-top: 8px;">
                                        <p style="color: rgba(255,255,255,0.85); margin: 0; font-size: 14px;">AI-Powered Audio Transcription</p>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- Content -->
                    <tr>
                        <td style="background-color: #ffffff; padding: 40px; border-left: 1px solid #e5e7eb; border-right: 1px solid #e5e7eb;">
                            {content_html}
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="background-color: #f8f9fa; padding: 24px 40px; border-radius: 0 0 12px 12px; border: 1px solid #e5e7eb; border-top: none;">
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                <tr>
                                    <td style="text-align: center;">
                                        <p style="color: #6b7280; font-size: 12px; margin: 0 0 8px 0;">
                                            This email was sent by Speakr. If you have questions, please contact your administrator.
                                        </p>
                                        <p style="color: #9ca3af; font-size: 11px; margin: 0;">
                                            &copy; {datetime.utcnow().year} Speakr &middot; AI-Powered Audio Transcription
                                        </p>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

    text_body = f"""
{subject}
{'=' * len(subject)}

{content_text}

---
This email was sent by Speakr - AI-Powered Audio Transcription.
If you have questions, please contact your administrator.
"""

    return html_body, text_body


def send_verification_email(user) -> bool:
    """
    Send a verification email to a user.

    Args:
        user: User model instance

    Returns True if email was sent successfully, False otherwise.
    """
    from src.database import db

    if not is_email_verification_enabled():
        logger.debug("Email verification is disabled")
        return False

    if not is_smtp_configured():
        logger.warning("Cannot send verification email: SMTP not configured")
        return False

    # Generate token and store it
    token = generate_verification_token(user.id)
    user.email_verification_token = token
    user.email_verification_sent_at = datetime.utcnow()
    db.session.commit()

    # Build verification URL
    verify_url = url_for('auth.verify_email', token=token, _external=True)

    subject = "Verify your email address - Speakr"

    content_html = f"""
<h2 style="color: #1f2937; margin: 0 0 24px 0; font-size: 24px; font-weight: 600;">Verify Your Email Address</h2>

<p style="color: #374151; margin: 0 0 16px 0; font-size: 16px;">Hi {user.username},</p>

<p style="color: #374151; margin: 0 0 24px 0; font-size: 16px;">
    Welcome to Speakr! To complete your registration and start transcribing your audio recordings, please verify your email address.
</p>

<div style="text-align: center; margin: 32px 0;">
    <a href="{verify_url}" style="display: inline-block; background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 14px 32px; border-radius: 8px; font-weight: 600; font-size: 16px;">Verify Email Address</a>
</div>

<p style="color: #6b7280; font-size: 14px; margin: 24px 0 8px 0;">Or copy and paste this link into your browser:</p>
<p style="word-break: break-all; color: #2563eb; font-size: 14px; margin: 0; padding: 12px; background-color: #f3f4f6; border-radius: 6px;">{verify_url}</p>

<div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #e5e7eb;">
    <p style="color: #9ca3af; font-size: 13px; margin: 0;">
        <strong>This link will expire in 24 hours.</strong><br>
        If you didn't create an account on Speakr, you can safely ignore this email.
    </p>
</div>
"""

    content_text = f"""Hi {user.username},

Welcome to Speakr! To complete your registration and start transcribing your audio recordings, please verify your email address.

Click here to verify: {verify_url}

This link will expire in 24 hours.

If you didn't create an account on Speakr, you can safely ignore this email."""

    html_body, text_body = _get_email_template(content_html, content_text, subject)
    return _send_email(user.email, subject, html_body, text_body)


def send_password_reset_email(user) -> bool:
    """
    Send a password reset email to a user.

    Args:
        user: User model instance

    Returns True if email was sent successfully, False otherwise.
    """
    from src.database import db

    if not is_smtp_configured():
        logger.warning("Cannot send password reset email: SMTP not configured")
        return False

    # Generate token and store it
    token = generate_password_reset_token(user.id)
    user.password_reset_token = token
    user.password_reset_sent_at = datetime.utcnow()
    db.session.commit()

    # Build reset URL
    reset_url = url_for('auth.reset_password', token=token, _external=True)

    subject = "Reset your password - Speakr"

    content_html = f"""
<h2 style="color: #1f2937; margin: 0 0 24px 0; font-size: 24px; font-weight: 600;">Reset Your Password</h2>

<p style="color: #374151; margin: 0 0 16px 0; font-size: 16px;">Hi {user.username},</p>

<p style="color: #374151; margin: 0 0 24px 0; font-size: 16px;">
    We received a request to reset your Speakr account password. Click the button below to create a new password.
</p>

<div style="text-align: center; margin: 32px 0;">
    <a href="{reset_url}" style="display: inline-block; background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 14px 32px; border-radius: 8px; font-weight: 600; font-size: 16px;">Reset Password</a>
</div>

<p style="color: #6b7280; font-size: 14px; margin: 24px 0 8px 0;">Or copy and paste this link into your browser:</p>
<p style="word-break: break-all; color: #2563eb; font-size: 14px; margin: 0; padding: 12px; background-color: #f3f4f6; border-radius: 6px;">{reset_url}</p>

<div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #e5e7eb;">
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
        <tr>
            <td style="width: 24px; vertical-align: top; padding-right: 12px;">
                <span style="font-size: 18px;">⚠️</span>
            </td>
            <td>
                <p style="color: #9ca3af; font-size: 13px; margin: 0;">
                    <strong style="color: #6b7280;">This link will expire in 1 hour.</strong><br>
                    If you didn't request a password reset, you can safely ignore this email. Your password will remain unchanged.
                </p>
            </td>
        </tr>
    </table>
</div>
"""

    content_text = f"""Hi {user.username},

We received a request to reset your Speakr account password. Click the link below to create a new password:

{reset_url}

This link will expire in 1 hour.

If you didn't request a password reset, you can safely ignore this email. Your password will remain unchanged."""

    html_body, text_body = _get_email_template(content_html, content_text, subject)
    return _send_email(user.email, subject, html_body, text_body)


def can_resend_verification(user) -> tuple[bool, Optional[int]]:
    """
    Check if a verification email can be resent.

    Returns (can_resend, seconds_until_can_resend)
    """
    if not user.email_verification_sent_at:
        return True, None

    # Allow resend after 60 seconds
    cooldown = timedelta(seconds=60)
    time_since_last = datetime.utcnow() - user.email_verification_sent_at

    if time_since_last >= cooldown:
        return True, None

    remaining = (cooldown - time_since_last).seconds
    return False, remaining


def can_resend_password_reset(user) -> tuple[bool, Optional[int]]:
    """
    Check if a password reset email can be resent.

    Returns (can_resend, seconds_until_can_resend)
    """
    if not user.password_reset_sent_at:
        return True, None

    # Allow resend after 60 seconds
    cooldown = timedelta(seconds=60)
    time_since_last = datetime.utcnow() - user.password_reset_sent_at

    if time_since_last >= cooldown:
        return True, None

    remaining = (cooldown - time_since_last).seconds
    return False, remaining
