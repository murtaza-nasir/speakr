"""
Audit models.

``AuditEvent`` is the access audit log: one row per recorded event (sign-in,
access to a recording, change, share, administration). It is written only
through ``src/services/audit.py``.

``ShareAuditLog`` is the older table for internal shares. Nothing writes it
any more; its rows were copied into ``audit_event`` and the table is kept for
one release.
"""

from datetime import datetime

from sqlalchemy import event as sa_event

from src.database import db


class AuditEvent(db.Model):
    """One event of the access audit log.

    No foreign keys and no relationships on purpose: a row has to remain after
    the recording, the account or the group it names is deleted. The ids of
    deleted rows can be given out again on SQLite, so a row is read together
    with the deletion events that follow it (docs/admin-guide/audit-log.md).

    Rows are never updated and are deleted only by the pruning statement in
    ``src/services/audit.py``; the listeners below refuse both through the ORM.
    """

    __tablename__ = 'audit_event'

    # AUTOINCREMENT on SQLite, a sequence on PostgreSQL: an id is never given
    # out again, so removed rows remain visible as gaps.
    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    event = db.Column(db.String(48), nullable=False)
    event_group = db.Column(db.String(12), nullable=False)
    actor_kind = db.Column(db.String(12), nullable=False)
    actor_user_id = db.Column(db.Integer, nullable=True)
    actor_token_id = db.Column(db.Integer, nullable=True)
    ip = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(160), nullable=True)
    recording_id = db.Column(db.Integer, nullable=True)
    recording_owner_id = db.Column(db.Integer, nullable=True)
    target_user_id = db.Column(db.Integer, nullable=True)
    group_id = db.Column(db.Integer, nullable=True)
    outcome = db.Column(db.String(8), nullable=False, default='success')
    details = db.Column(db.JSON, nullable=True)
    dedup_key = db.Column(db.String(80), nullable=True)

    __table_args__ = (
        db.Index('ix_audit_event_created_at', 'created_at'),
        db.Index('ix_audit_event_actor_created', 'actor_user_id', 'created_at'),
        db.Index('ix_audit_event_recording_created', 'recording_id', 'created_at'),
        db.Index('ix_audit_event_target_created', 'target_user_id', 'created_at'),
        db.Index('ix_audit_event_group_created', 'event_group', 'created_at'),
        db.Index('ux_audit_event_dedup_key', 'dedup_key', unique=True),
        {'sqlite_autoincrement': True},
    )

    def to_dict(self):
        return {
            'id': self.id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'event': self.event,
            'event_group': self.event_group,
            'actor_kind': self.actor_kind,
            'actor_user_id': self.actor_user_id,
            'actor_token_id': self.actor_token_id,
            'ip': self.ip,
            'user_agent': self.user_agent,
            'recording_id': self.recording_id,
            'recording_owner_id': self.recording_owner_id,
            'target_user_id': self.target_user_id,
            'group_id': self.group_id,
            'outcome': self.outcome,
            'details': self.details,
        }


@sa_event.listens_for(AuditEvent, 'before_update')
def _refuse_audit_update(mapper, connection, target):
    raise RuntimeError('Audit events are append-only and cannot be updated')


@sa_event.listens_for(AuditEvent, 'before_delete')
def _refuse_audit_delete(mapper, connection, target):
    raise RuntimeError('Audit events are append-only and cannot be deleted')


class ShareAuditLog(db.Model):
    """Former audit trail of internal shares. No longer written."""

    __tablename__ = 'share_audit_log'

    id = db.Column(db.Integer, primary_key=True)

    # Action details
    action = db.Column(db.String(20), nullable=False)  # 'created', 'modified', 'revoked', 'cascade_revoked'
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='CASCADE'), nullable=False)

    # Actor (who performed the action)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    actor = db.relationship('User', foreign_keys=[actor_id], backref=db.backref('audit_actions_performed', cascade='all, delete-orphan'))

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
    recording = db.relationship('Recording', backref=db.backref('share_audit_logs', cascade='all, delete-orphan'))

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
