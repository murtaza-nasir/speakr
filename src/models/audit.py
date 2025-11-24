"""
Audit logging models for tracking share operations.

Provides comprehensive audit trail for security and compliance.
"""

from datetime import datetime
from src.database import db


class ShareAuditLog(db.Model):
    """Audit trail for share operations."""

    __tablename__ = 'share_audit_log'

    id = db.Column(db.Integer, primary_key=True)

    # Action details
    action = db.Column(db.String(20), nullable=False)  # 'created', 'modified', 'revoked', 'cascade_revoked'
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)

    # Actor (who performed the action)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    actor = db.relationship('User', foreign_keys=[actor_id], backref='audit_actions_performed')

    # Target (who was affected - optional for some actions)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    target_user = db.relationship('User', foreign_keys=[target_user_id])

    # Permission snapshot at time of action
    permissions_granted = db.Column(db.JSON, nullable=True)  # What was granted/revoked
    actor_permissions = db.Column(db.JSON, nullable=True)    # What actor had at time

    # Metadata
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    share_id = db.Column(db.Integer, nullable=True)  # Reference to share if applicable

    # Context and notes
    notes = db.Column(db.Text, nullable=True)  # System-generated notes (e.g., "Permission constrained", "Cascade revocation")
    ip_address = db.Column(db.String(45), nullable=True)  # Actor's IP address

    # Recording relationship
    recording = db.relationship('Recording', backref='share_audit_logs')

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'action': self.action,
            'recording_id': self.recording_id,
            'actor_id': self.actor_id,
            'actor_username': self.actor.username if self.actor else None,
            'target_user_id': self.target_user_id,
            'target_username': self.target_user.username if self.target_user else None,
            'permissions_granted': self.permissions_granted,
            'actor_permissions': self.actor_permissions,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'share_id': self.share_id,
            'notes': self.notes,
            'ip_address': self.ip_address
        }

    @staticmethod
    def log_share_created(recording_id, actor_id, target_user_id, permissions, actor_permissions=None, notes=None, ip_address=None):
        """Log share creation."""
        log = ShareAuditLog(
            action='created',
            recording_id=recording_id,
            actor_id=actor_id,
            target_user_id=target_user_id,
            permissions_granted=permissions,
            actor_permissions=actor_permissions,
            notes=notes,
            ip_address=ip_address
        )
        db.session.add(log)
        return log

    @staticmethod
    def log_share_modified(share_id, recording_id, actor_id, target_user_id, old_permissions, new_permissions, notes=None, ip_address=None):
        """Log share modification."""
        log = ShareAuditLog(
            action='modified',
            recording_id=recording_id,
            actor_id=actor_id,
            target_user_id=target_user_id,
            permissions_granted={'old': old_permissions, 'new': new_permissions},
            share_id=share_id,
            notes=notes,
            ip_address=ip_address
        )
        db.session.add(log)
        return log

    @staticmethod
    def log_share_revoked(share_id, recording_id, actor_id, target_user_id, was_cascade=False, notes=None, ip_address=None):
        """Log share revocation."""
        action = 'cascade_revoked' if was_cascade else 'revoked'
        log = ShareAuditLog(
            action=action,
            recording_id=recording_id,
            actor_id=actor_id,
            target_user_id=target_user_id,
            share_id=share_id,
            notes=notes,
            ip_address=ip_address
        )
        db.session.add(log)
        return log
