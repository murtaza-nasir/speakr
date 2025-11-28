"""
API Token database model.

This module defines the APIToken model for managing user API tokens
that allow authentication via Bearer tokens for automation tools.
"""

from datetime import datetime
from src.database import db


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
            'revoked': self.revoked
        }

    def is_expired(self):
        """Check if token has expired."""
        if not self.expires_at:
            return False
        return self.expires_at < datetime.utcnow()

    def is_valid(self):
        """Check if token is valid (not revoked and not expired)."""
        return not self.revoked and not self.is_expired()
