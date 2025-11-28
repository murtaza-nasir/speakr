"""
Token authentication utilities.

This module provides token-based authentication for API access,
allowing users to authenticate with Bearer tokens instead of session cookies.
"""

import hashlib
from datetime import datetime
from flask import request
from src.models import APIToken, User


def extract_token_from_request():
    """
    Extract API token from various possible locations in the request.

    Checks in order:
    1. Authorization header with Bearer scheme
    2. X-API-Token header
    3. API-Token header
    4. 'token' query parameter

    Returns:
        str: The extracted token, or None if not found
    """
    # Check Authorization header (Bearer token)
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]  # Remove 'Bearer ' prefix

    # Check X-API-Token header
    token = request.headers.get('X-API-Token')
    if token:
        return token

    # Check API-Token header
    token = request.headers.get('API-Token')
    if token:
        return token

    # Check query parameter
    token = request.args.get('token')
    if token:
        return token

    return None


def hash_token(token):
    """
    Hash a token using SHA-256.

    Args:
        token (str): The plaintext token to hash

    Returns:
        str: The hexadecimal hash of the token
    """
    return hashlib.sha256(token.encode()).hexdigest()


def load_user_from_token():
    """
    Load a user from an API token in the request.

    This function is used by Flask-Login's request_loader to authenticate
    users via API tokens instead of sessions.

    Returns:
        User: The authenticated user, or None if authentication fails
    """
    # Extract token from request
    token = extract_token_from_request()
    if not token:
        return None

    # Hash the token to look up in database
    token_hash = hash_token(token)

    # Find the token in the database
    api_token = APIToken.query.filter_by(token_hash=token_hash).first()

    # Validate token
    if not api_token:
        return None

    if not api_token.is_valid():
        return None

    # Update last used timestamp
    api_token.last_used_at = datetime.utcnow()
    from src.database import db
    db.session.commit()

    # Return the associated user
    return api_token.user


def is_token_authenticated():
    """
    Check if the current request is authenticated via API token.

    Returns:
        bool: True if a valid token was provided, False otherwise
    """
    token = extract_token_from_request()
    return token is not None
