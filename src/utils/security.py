"""
Security utilities for password validation and other security functions.

This module provides security-related utility functions for the application.
"""

import os
import re
import secrets
from wtforms.validators import ValidationError
from urllib.parse import urlparse


# The value the app historically fell back to when SECRET_KEY was unset. It is
# a public constant in the source, so it must never be accepted at runtime.
INSECURE_SECRET_KEY = 'default-dev-key-change-in-production'


def resolve_secret_key(env_key, db_uri, key_file=None,
                       instance_dir_fallback='/data/instance',
                       token_factory=None):
    """Resolve a Flask SECRET_KEY without any Flask/logging dependency.

    This key signs the session cookie and the itsdangerous email/password-reset
    tokens, so a known value allows session and reset-token forgery. Resolution:

    1. A real ``env_key`` (not the insecure default) is used as-is.
    2. The insecure built-in default is refused (raises ``ValueError``).
    3. Otherwise a strong key is generated once and persisted next to the
       instance data (``key_file``, or ``<instance_dir>/secret_key``) so it is
       stable across restarts and captured by a normal data-volume backup.

    Returns ``(key, action)`` with action in
    {'env', 'persisted', 'generated', 'ephemeral'}. Pure and unit-testable;
    ``token_factory`` defaults to a 256-bit hex token and is injectable.
    """
    if token_factory is None:
        token_factory = lambda: secrets.token_hex(32)

    env_key = (env_key or '').strip()
    if env_key and env_key != INSECURE_SECRET_KEY:
        return env_key, 'env'
    if env_key == INSECURE_SECRET_KEY:
        raise ValueError(
            "SECRET_KEY is set to the insecure built-in default "
            f"('{INSECURE_SECRET_KEY}'). This key signs session cookies and "
            "password-reset tokens; a known value lets anyone forge an admin "
            "session. Set SECRET_KEY to a strong random value, e.g. "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"`."
        )

    if not key_file:
        if db_uri and db_uri.startswith('sqlite'):
            db_path = db_uri.split('sqlite:///', 1)[-1]
            instance_dir = os.path.dirname(db_path) or '.'
        else:
            instance_dir = instance_dir_fallback
        key_file = os.path.join(instance_dir, 'secret_key')

    # Reuse a previously-persisted key if present.
    try:
        with open(key_file, 'r') as fh:
            existing = fh.read().strip()
        if existing:
            return existing, 'persisted'
    except OSError:
        pass

    new_key = token_factory()
    try:
        os.makedirs(os.path.dirname(key_file) or '.', exist_ok=True)
        # O_EXCL so concurrent workers don't clobber each other.
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, new_key.encode())
        finally:
            os.close(fd)
        return new_key, 'generated'
    except FileExistsError:
        # Lost the create race to another worker — use the key it wrote.
        try:
            with open(key_file, 'r') as fh:
                persisted = fh.read().strip()
            if persisted:
                return persisted, 'persisted'
        except OSError:
            pass
        return new_key, 'ephemeral'
    except OSError:
        return new_key, 'ephemeral'


def password_check(form, field):
    """
    Custom WTForms validator for password strength.

    Validates that passwords meet security requirements:
    - At least 8 characters long
    - Contains at least one uppercase letter
    - Contains at least one lowercase letter
    - Contains at least one number
    - Contains at least one special character

    Args:
        form: WTForms form object
        field: WTForms field object containing the password

    Raises:
        ValidationError: If password doesn't meet requirements
    """
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


# --- URL Security ---

def is_safe_url(target):
    """Return True only for local relative paths.

    Rejects scheme-relative URLs (``//evil.com``), backslash-prefixed URLs
    (``\\evil.com``), absolute URLs, and anything with a scheme or netloc.
    The validator runs against the raw value so the same string can be passed
    to ``redirect()`` without the parser-mismatch open-redirect class.
    """
    if not target or not isinstance(target, str):
        return False
    if not target.startswith('/'):
        return False
    if target.startswith('//') or target.startswith('/\\'):
        return False
    if '\\' in target:
        return False
    if any(ord(ch) < 0x20 for ch in target):
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return True

