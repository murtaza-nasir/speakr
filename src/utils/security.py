"""
Security utilities for password validation and other security functions.

This module provides security-related utility functions for the application.
"""

import re
from wtforms.validators import ValidationError
from urllib.parse import urlparse, urljoin
from flask import request


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
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

