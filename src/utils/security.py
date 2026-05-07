"""
Security utilities for password validation and other security functions.

This module provides security-related utility functions for the application.
"""

import re
from wtforms.validators import ValidationError
from urllib.parse import urlparse


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

