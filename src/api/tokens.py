"""
API Token management routes.

This blueprint handles creating, listing, and revoking API tokens
for user authentication.
"""

import os
import secrets
from datetime import datetime, timedelta
from flask import Blueprint, jsonify
from flask_login import login_required, current_user

from src.database import db
from src.models import APIToken
from src.utils.token_auth import hash_token
from src.utils.request_body import is_int, json_object_body
from src.utils.localization import api_error, translate

# Create blueprint
tokens_bp = Blueprint('tokens', __name__, url_prefix='/api/tokens')

# Active tokens an account may hold (#484); 0 means no limit. Tokens an
# account already holds above the limit stay valid.
try:
    API_MAX_TOKENS_PER_USER = max(0, int(os.environ.get('API_MAX_TOKENS_PER_USER', '10')))
except ValueError:
    API_MAX_TOKENS_PER_USER = 10

# Extensions (injected after app initialization)
bcrypt = None
csrf = None
limiter = None


def init_tokens_helpers(_bcrypt, _csrf, _limiter):
    """Initialize extensions after app creation."""
    global bcrypt, csrf, limiter
    bcrypt = _bcrypt
    csrf = _csrf
    limiter = _limiter


def rate_limit(limit_string):
    """Decorator that applies a per-endpoint Flask-Limiter limit.

    Applied lazily because the limiter is injected after this module is
    imported (see the matching helper in src/api/auth.py). Previously a no-op.
    """
    def decorator(f):
        from functools import wraps
        state = {'limited': None}

        @wraps(f)
        def wrapper(*args, **kwargs):
            if limiter is not None and getattr(limiter, 'enabled', True):
                if state['limited'] is None:
                    state['limited'] = limiter.limit(limit_string)(f)
                return state['limited'](*args, **kwargs)
            return f(*args, **kwargs)

        wrapper._rate_limit = limit_string
        return wrapper
    return decorator


def generate_token():
    """
    Generate a secure random API token.

    Returns:
        str: A cryptographically secure random token
    """
    return secrets.token_urlsafe(32)


@tokens_bp.route('', methods=['GET'])
@login_required
def list_tokens():
    """
    List all API tokens for the current user.

    Returns:
        JSON: List of token objects (without the actual token values)
    """
    tokens = APIToken.query.filter_by(user_id=current_user.id).all()
    return jsonify({
        'tokens': [token.to_dict() for token in tokens]
    })


@tokens_bp.route('', methods=['POST'])
@login_required
@rate_limit("10 per hour")
def create_token():
    """
    Create a new API token for the current user.

    Request JSON:
        name (str, optional): A friendly name for the token
        expires_in_days (int, optional): Number of days until expiration (0 = no expiration)

    Returns:
        JSON: The new token object including the plaintext token (shown only once)
    """
    # Every field is optional, so no body is a token with the defaults;
    # a body that is present is an object (#687)
    data, body_error = json_object_body(optional=True)
    if body_error:
        return body_error

    # Validate input: name is a string of at most 100 characters (the
    # column), expires_in_days a whole number of at most 100 years, so the
    # expiry date cannot overflow
    name = data.get('name', 'Unnamed Token')
    if name is None:
        name = 'Unnamed Token'
    if not isinstance(name, str):
        return jsonify(api_error('field_must_be_string', field='name')), 400
    if len(name) > 100:
        return jsonify(api_error('token_name_too_long', max=100)), 400
    expires_in_days = data.get('expires_in_days', 0)

    # Validate expiration
    if not is_int(expires_in_days) or not 0 <= expires_in_days <= 36500:
        return jsonify(api_error('invalid_expires_in_days')), 400

    # Scopes (mailr spec G1). Absent = a full token, as before scopes existed.
    scopes_value = None
    if 'scopes' in data:
        from src.models.api_token import parse_scopes
        scopes_value, error = parse_scopes(data.get('scopes'))
        if error:
            return jsonify({'error': error, 'code': 'invalid_parameter'}), 400

    return issue_token(current_user.id, name, expires_in_days, scopes_value)


def issue_token(user_id, name, expires_in_days, scopes_value, sso_client_id=None):
    """Create a token for a user, within the per-account cap (#484).

    Shared by Account settings and the SSO token exchange
    (POST /api/v1/tokens/exchange). Returns (response, status); the response
    holds the plaintext token, the only time it is shown.
    """
    if API_MAX_TOKENS_PER_USER:
        now = datetime.utcnow()
        active = (APIToken.query.filter_by(user_id=user_id, revoked=False)
                  .filter(db.or_(APIToken.expires_at.is_(None), APIToken.expires_at > now))
                  .count())
        if active >= API_MAX_TOKENS_PER_USER:
            return jsonify({
                'error': translate('server.apiError.token_limit_reached', max=API_MAX_TOKENS_PER_USER),
                'code': 'token_limit_reached',
                'max_tokens': API_MAX_TOKENS_PER_USER,
                'active_tokens': active,
            }), 409

    # Generate the token
    plaintext_token = generate_token()
    token_hash = hash_token(plaintext_token)

    # Calculate expiration date
    expires_at = None
    if expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

    # Create the token record
    api_token = APIToken(
        user_id=user_id,
        token_hash=token_hash,
        name=name,
        expires_at=expires_at,
        scopes=scopes_value,
        sso_client_id=sso_client_id,
    )

    db.session.add(api_token)
    db.session.flush()
    # The id and the scopes; never the name, which the user types.
    from src.services import audit
    audit.record_in_tx('token.created', target_user_id=user_id, details={
        'token_id': api_token.id, 'scopes': api_token.scope_list,
        'expires': expires_at is not None, 'sso_client_id': sso_client_id})
    db.session.commit()

    # Return the token data INCLUDING the plaintext token
    # This is the only time the plaintext token will be shown
    response = api_token.to_dict()
    response['token'] = plaintext_token

    return jsonify(response), 201


@tokens_bp.route('/<int:token_id>', methods=['DELETE'])
@login_required
@rate_limit("20 per hour")
def revoke_token(token_id):
    """
    Revoke (delete) an API token.

    Args:
        token_id (int): The ID of the token to revoke

    Returns:
        JSON: Success message
    """
    # Find the token
    api_token = APIToken.query.filter_by(
        id=token_id,
        user_id=current_user.id
    ).first()

    if not api_token:
        return jsonify(api_error('token_not_found')), 404

    # Delete the token
    from src.services import audit
    audit.record_in_tx('token.revoked', details={'token_id': api_token.id})
    db.session.delete(api_token)
    db.session.commit()

    return jsonify({'message': translate('server.apiMessage.tokenRevoked')}), 200


@tokens_bp.route('/<int:token_id>', methods=['PATCH'])
@login_required
@rate_limit("20 per hour")
def update_token(token_id):
    """
    Update an API token's metadata (name only).

    Args:
        token_id (int): The ID of the token to update

    Request JSON:
        name (str): The new name for the token

    Returns:
        JSON: Updated token object
    """
    # Find the token
    api_token = APIToken.query.filter_by(
        id=token_id,
        user_id=current_user.id
    ).first()

    if not api_token:
        return jsonify(api_error('token_not_found')), 404

    data, body_error = json_object_body(optional=True)

    if body_error:

        return body_error
    new_name = data.get('name')
    if 'scopes' not in data and not new_name:
        return jsonify(api_error('name_required')), 400

    # Scopes can only be narrowed (mailr spec G1): a wider set needs a new
    # token, so a stolen session cannot silently widen an integration token.
    if 'scopes' in data:
        from src.models.api_token import parse_scopes
        new_value, error = parse_scopes(data.get('scopes'))
        if error:
            return jsonify({'error': error, 'code': 'invalid_parameter'}), 400
        current = api_token.scope_set
        new = None if new_value is None else frozenset(__import__('json').loads(new_value))
        widening = (new is None and current is not None) or (current is not None and not new <= current)
        if widening:
            return jsonify({'error': translate('server.apiError.scope_narrow_only'),
                            'code': 'invalid_parameter'}), 400
        api_token.scopes = new_value

    if new_name:
        api_token.name = new_name
    from src.services import audit
    audit.record_in_tx('token.changed', details={
        'token_id': api_token.id, 'scopes': api_token.scope_list if 'scopes' in data else None})
    db.session.commit()

    return jsonify(api_token.to_dict()), 200
