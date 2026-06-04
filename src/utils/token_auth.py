"""
Token authentication utilities.

This module provides token-based authentication for API access,
allowing users to authenticate with Bearer tokens instead of session cookies.
"""

import hashlib
from datetime import datetime
from flask import request
from src.models import APIToken, User


def extract_token_from_request(headers_only=False):
    """
    Extract API token from various possible locations in the request.

    Checks in order:
    1. Authorization header with Bearer scheme
    2. X-API-Token header
    3. API-Token header
    4. 'token' query parameter (only when ``headers_only=False``)

    The ``headers_only`` flag exists because the query-string token can
    be triggered by a Simple Cross-Origin Request without CORS preflight
    (see GHSA-x4q4-3ww4-h329). Code paths that make security decisions
    based on whether a request is API-token-authenticated MUST pass
    ``headers_only=True`` so an attacker cannot place ``?token=...`` on
    a victim-browser URL to fake API authentication.

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

    if headers_only:
        return None

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


def load_user_from_token_headers_only():
    """Validate a header-only API token against the database.

    Same DB lookup as :func:`load_user_from_token` but ignores the
    ``?token=`` query parameter. Used by the CSRF protection hook
    because Simple Cross-Origin Requests can carry an attacker-supplied
    query string without triggering CORS preflight, while the three
    accepted headers (Authorization, X-API-Token, API-Token) all do
    trigger preflight and so cannot be set by a CSRF attack page.

    Returns the authenticated User, or None.
    """
    token = extract_token_from_request(headers_only=True)
    if not token:
        return None

    token_hash = hash_token(token)
    api_token = APIToken.query.filter_by(token_hash=token_hash).first()
    if not api_token or not api_token.is_valid():
        return None

    api_token.last_used_at = datetime.utcnow()
    from src.database import db
    db.session.commit()
    return api_token.user


