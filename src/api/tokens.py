"""
API Token management routes.

This blueprint handles creating, listing, and revoking API tokens
for user authentication.
"""

import secrets
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from src.database import db
from src.models import APIToken
from src.utils.token_auth import hash_token

# Create blueprint
tokens_bp = Blueprint('tokens', __name__, url_prefix='/api/tokens')

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
    """Decorator that applies rate limiting if limiter is available."""
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
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
    data = request.get_json()

    # Validate input
    name = data.get('name', 'Unnamed Token')
    expires_in_days = data.get('expires_in_days', 0)

    # Validate expiration
    if not isinstance(expires_in_days, int) or expires_in_days < 0:
        return jsonify({'error': 'expires_in_days must be a non-negative integer'}), 400

    # Generate the token
    plaintext_token = generate_token()
    token_hash = hash_token(plaintext_token)

    # Calculate expiration date
    expires_at = None
    if expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

    # Create the token record
    api_token = APIToken(
        user_id=current_user.id,
        token_hash=token_hash,
        name=name,
        expires_at=expires_at
    )

    db.session.add(api_token)
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
        return jsonify({'error': 'Token not found'}), 404

    # Delete the token
    db.session.delete(api_token)
    db.session.commit()

    return jsonify({'message': 'Token revoked successfully'}), 200


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
        return jsonify({'error': 'Token not found'}), 404

    # Update the name
    data = request.get_json()
    new_name = data.get('name')

    if not new_name:
        return jsonify({'error': 'name is required'}), 400

    api_token.name = new_name
    db.session.commit()

    return jsonify(api_token.to_dict()), 200
