"""
Token authentication utilities.

This module provides token-based authentication for API access,
allowing users to authenticate with Bearer tokens instead of session cookies.
"""

import hashlib
import os
from datetime import datetime
from functools import wraps
from flask import request, g, has_request_context
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


def find_api_token(token):
    """The valid APIToken row for a plaintext token, or None."""
    if not token:
        return None
    api_token = APIToken.query.filter_by(token_hash=hash_token(token)).first()
    if not api_token or not api_token.is_valid():
        return None
    return api_token


def _mark_used(api_token):
    api_token.last_used_at = datetime.utcnow()
    from src.database import db
    db.session.commit()


def load_user_from_token_value(token):
    """Validate a plaintext API token and return its associated user.

    The caller controls where the token came from. Keeping this separate from
    request extraction lets narrow integrations authenticate an explicit field
    without making that field a global token source for every route. The
    matched row is kept on flask.g.api_token for scope and rate-limit checks.
    """
    api_token = find_api_token(token)
    if api_token is None:
        return None
    _mark_used(api_token)
    if has_request_context():
        g.api_token = api_token
        request.environ['_speakr_secret_token'] = api_token
    return api_token.user


def resolve_request_token():
    """The API token of this request and whether it came from ?token=.

    Resolved once per request and cached on the request itself (its WSGI
    environ), never on flask.g: g lives as long as the app context, which a
    test or a background job can keep across several requests, and a token
    from an earlier request must never authenticate a later one. The scope
    check, the rate-limit key and Flask-Login's request_loader all use the
    same row. Returns (APIToken or None, via_query).
    """
    env = request.environ
    if '_speakr_api_token' not in env:
        header_value = extract_token_from_request(headers_only=True)
        value = header_value or extract_token_from_request()
        api_token = find_api_token(value)
        env['_speakr_api_token'] = (api_token, bool(api_token is not None and not header_value))
    api_token, via_query = env['_speakr_api_token']
    g.api_token = api_token
    g.api_token_via_query = via_query
    return api_token, via_query


def current_api_token():
    """The API token authenticating this request, or None for a session request."""
    if not has_request_context():
        return None
    from flask import session
    if session.get('_user_id'):
        return None
    return resolve_request_token()[0]


def load_user_from_token():
    """
    Load a user from an API token in the request.

    This function is used by Flask-Login's request_loader to authenticate
    users via API tokens instead of sessions.

    Returns:
        User: The authenticated user, or None if authentication fails
    """
    api_token, _ = resolve_request_token()
    if api_token is None:
        return None
    _mark_used(api_token)
    return api_token.user


# --- Scopes (mailr spec G1) -------------------------------------------------

# Per-token limits by kind of route; configurable by env. Applied only to
# requests authenticated by a token, keyed by the token id.
_RATE_LIMITS = {
    'read': os.environ.get('API_TOKEN_RATE_LIMIT_READ', '120 per minute'),
    'write': os.environ.get('API_TOKEN_RATE_LIMIT_WRITE', '30 per minute'),
    'process': os.environ.get('API_TOKEN_RATE_LIMIT_PROCESS', '10 per minute'),
}


def _rate_category(scopes):
    if 'process' in scopes:
        return 'process'
    if not scopes or scopes == {'read'}:
        return 'read'
    return 'write'


def token_rate_key():
    """Rate-limit key: the token id for token requests, else the client address."""
    api_token = current_api_token()
    if api_token is not None:
        return f"token:{api_token.id}"
    from flask_limiter.util import get_remote_address
    return get_remote_address()


def is_token_request():
    """True when this request is authenticated by an API token (not a session)."""
    return current_api_token() is not None


def require_scope(*scopes):
    """Mark a view with the scopes a scoped token needs, and apply the per-token
    rate limit for its kind (read, write or process).

    require_scope() with no scopes accepts any valid token. The check itself runs
    in the before_request hook in src/app.py, before the view, so a refused
    request has no side effect. Full tokens and sessions are not restricted.
    """
    required = frozenset(scopes)
    limit_string = _RATE_LIMITS[_rate_category(set(scopes))]

    def decorator(f):
        state = {'limited': None}

        @wraps(f)
        def wrapper(*args, **kwargs):
            from src.app import limiter
            if limiter is not None and getattr(limiter, 'enabled', True):
                if state['limited'] is None:
                    state['limited'] = limiter.limit(
                        limit_string, key_func=token_rate_key,
                        exempt_when=lambda: not is_token_request(),
                        scope=lambda endpoint: f"token-{_rate_category(set(scopes))}",
                    )(f)
                return state['limited'](*args, **kwargs)
            return f(*args, **kwargs)

        wrapper._required_scopes = required
        return wrapper
    return decorator


def scope_error_response(missing, api_token):
    """The 403 answer for a scoped token without the needed scope."""
    from flask import jsonify
    names = sorted(missing or [])
    message = (f"This token does not have the '{names[0]}' scope" if len(names) == 1
               else f"This token does not have the scopes {', '.join(names)}" if names
               else "This token cannot be used on this route; use a full-access token or a session")
    resp = jsonify({'error': message, 'code': 'insufficient_scope',
                    'required_scopes': names, 'token_scopes': sorted(api_token.scope_set or [])})
    resp.status_code = 403
    resp.headers['WWW-Authenticate'] = 'Bearer error="insufficient_scope"' + (
        f', scope="{" ".join(names)}"' if names else '')
    return resp


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
    return load_user_from_token_value(extract_token_from_request(headers_only=True))


