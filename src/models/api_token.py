"""
API Token database model.

This module defines the APIToken model for managing user API tokens
that allow authentication via Bearer tokens for automation tools.
"""

import json
from datetime import datetime
from src.database import db

# Scopes a token can carry (mailr spec section 3). A token with scopes NULL is a
# full token: every route, as before scopes existed. 'account' covers account
# settings and is separate from 'write' (decision 2026-10-02).
TOKEN_SCOPES = ('read', 'write', 'upload', 'process', 'share', 'delete', 'webhooks', 'account')
FULL_SCOPE = 'full'


def parse_scopes(value):
    """Validate a requested scope list. Returns (stored_value, error).

    stored_value is None for full access, else a JSON list. error is a message
    for a 400 answer, or None.
    """
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        return None, 'scopes must be a non-empty list of scope names'
    requested = {v.strip() for v in value}
    if FULL_SCOPE in requested:
        if len(requested) > 1:
            return None, "'full' cannot be combined with other scopes"
        return None, None
    unknown = sorted(requested - set(TOKEN_SCOPES))
    if unknown:
        return None, f"Unknown scope(s): {', '.join(unknown)}. Allowed: {', '.join(TOKEN_SCOPES)} or full"
    return json.dumps(sorted(requested)), None


class APIToken(db.Model):
    """API Token model for token-based authentication."""

    __tablename__ = 'api_token'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)  # User-friendly label (e.g., "n8n", "CLI")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    revoked = db.Column(db.Boolean, default=False, nullable=False, index=True)
    # JSON list of scopes; NULL = full access (every token created before scopes).
    scopes = db.Column(db.Text, nullable=True)

    # Relationship to User
    user = db.relationship('User', backref=db.backref('api_tokens', lazy=True, cascade='all, delete-orphan'))

    def __repr__(self):
        return f"APIToken(name='{self.name}', user_id={self.user_id}, revoked={self.revoked})"

    def to_dict(self):
        """Convert token to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'revoked': self.revoked,
            'scopes': self.scope_list,
        }

    @property
    def scope_set(self):
        """None for a full token, else the frozenset of its scopes."""
        if self.scopes is None:
            return None
        try:
            return frozenset(json.loads(self.scopes))
        except (TypeError, ValueError):
            return frozenset()

    @property
    def scope_list(self):
        """Scopes for API responses: ['full'] for a full token."""
        scopes = self.scope_set
        return [FULL_SCOPE] if scopes is None else sorted(scopes)

    def is_expired(self):
        """Check if token has expired."""
        if not self.expires_at:
            return False
        return self.expires_at < datetime.utcnow()

    def is_valid(self):
        """Check if token is valid (not revoked and not expired)."""
        return not self.revoked and not self.is_expired()
