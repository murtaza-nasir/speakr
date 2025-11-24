"""
Sharing models for public and internal recording shares.

This module defines models for sharing recordings both publicly (via links)
and internally (between users).
"""

import os
import secrets
from datetime import datetime
from src.database import db
from src.utils import local_datetime_filter


# Get sharing configuration from environment
SHOW_USERNAMES_IN_UI = os.environ.get('SHOW_USERNAMES_IN_UI', 'false').lower() == 'true'


class Share(db.Model):
    """Public sharing via shareable links."""

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(16))
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    share_summary = db.Column(db.Boolean, default=True)
    share_notes = db.Column(db.Boolean, default=True)

    user = db.relationship('User', backref=db.backref('shares', lazy=True, cascade='all, delete-orphan'))
    recording = db.relationship('Recording', backref=db.backref('shares', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'public_id': self.public_id,
            'recording_id': self.recording_id,
            'created_at': local_datetime_filter(self.created_at),
            'share_summary': self.share_summary,
            'share_notes': self.share_notes,
            'recording_title': self.recording.title if self.recording else "N/A",
            'audio_available': self.recording.audio_deleted_at is None if self.recording else True
        }


class InternalShare(db.Model):
    """Tracks internal sharing of recordings between users (independent of teams)."""

    __tablename__ = 'internal_share'

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='CASCADE'), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)  # User who shared
    shared_with_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)  # User it was shared with

    # Permissions
    can_edit = db.Column(db.Boolean, default=False)  # Can edit notes/metadata
    can_reshare = db.Column(db.Boolean, default=False)  # Can share with others

    # Source tracking for share cleanup
    source_type = db.Column(db.String(20), default='manual')  # 'manual' or 'group_tag'
    source_tag_id = db.Column(db.Integer, db.ForeignKey('tag.id', ondelete='SET NULL'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship for source tag
    source_tag = db.relationship('Tag', foreign_keys=[source_tag_id], backref=db.backref('created_shares', lazy=True))

    # Relationships
    recording = db.relationship('Recording', backref=db.backref('internal_shares', lazy=True, cascade='all, delete-orphan'))
    owner = db.relationship('User', foreign_keys=[owner_id], backref=db.backref('shared_recordings', lazy=True))
    shared_with = db.relationship('User', foreign_keys=[shared_with_user_id], backref=db.backref('received_shares', lazy=True))

    # Unique constraint: can't share same recording with same user twice
    __table_args__ = (db.UniqueConstraint('recording_id', 'shared_with_user_id', name='unique_recording_share'),)

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'recording_id': self.recording_id,
            'owner_id': self.owner_id,
            'owner_username': self.owner.username if SHOW_USERNAMES_IN_UI else None,
            'user_id': self.shared_with_user_id,  # For frontend compatibility
            'username': self.shared_with.username,  # Always include username
            'can_edit': self.can_edit,
            'can_reshare': self.can_reshare,
            'source_type': self.source_type,
            'source_tag_id': self.source_tag_id,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    @staticmethod
    def get_user_max_permissions(recording, user):
        """
        Get the maximum permissions a user can grant for a recording.

        Args:
            recording: Recording object
            user: User object attempting to share

        Returns:
            Dict with 'can_edit' and 'can_reshare' boolean flags
        """
        # Owner has unlimited permissions
        if recording.user_id == user.id:
            return {'can_edit': True, 'can_reshare': True}

        # Get user's share for this recording
        user_share = InternalShare.query.filter_by(
            recording_id=recording.id,
            shared_with_user_id=user.id
        ).first()

        if not user_share:
            # User has no access
            return {'can_edit': False, 'can_reshare': False}

        # User can only grant what they have
        return {
            'can_edit': user_share.can_edit,
            'can_reshare': user_share.can_reshare
        }

    @staticmethod
    def validate_reshare_permissions(recording, grantor_user, requested_permissions):
        """
        Validate that a user can grant the requested permissions.

        Args:
            recording: Recording object being shared
            grantor_user: User attempting to share (current_user)
            requested_permissions: Dict with 'can_edit' and 'can_reshare' flags

        Returns:
            Tuple of (is_valid: bool, error_message: str or None)
        """
        # Owner can grant anything
        if recording.user_id == grantor_user.id:
            return True, None

        # Get grantor's permissions
        max_permissions = InternalShare.get_user_max_permissions(recording, grantor_user)

        # Validate edit permission
        if requested_permissions.get('can_edit', False) and not max_permissions['can_edit']:
            return False, "You cannot grant edit permission because you do not have edit access"

        # Validate reshare permission
        if requested_permissions.get('can_reshare', False) and not max_permissions['can_reshare']:
            return False, "You cannot grant reshare permission because you do not have reshare access"

        return True, None

    @staticmethod
    def find_downstream_shares(recording_id, user_id):
        """
        Find all shares created by a specific user for a recording.
        Used for cascade revocation.

        Args:
            recording_id: ID of the recording
            user_id: ID of the user whose downstream shares to find

        Returns:
            List of InternalShare objects
        """
        return InternalShare.query.filter_by(
            recording_id=recording_id,
            owner_id=user_id
        ).all()

    @staticmethod
    def has_alternate_access_path(recording_id, user_id, excluding_grantor_id=None):
        """
        Check if a user has alternate access to a recording through other shares.
        Used to prevent cascade revocation when user has multiple access paths (diamond pattern).

        Args:
            recording_id: ID of the recording
            user_id: ID of the user to check
            excluding_grantor_id: Exclude shares from this grantor (the one being revoked)

        Returns:
            Boolean - True if user has alternate access path
        """
        query = InternalShare.query.filter(
            InternalShare.recording_id == recording_id,
            InternalShare.shared_with_user_id == user_id
        )

        if excluding_grantor_id is not None:
            query = query.filter(InternalShare.owner_id != excluding_grantor_id)

        return query.count() > 0


class SharedRecordingState(db.Model):
    """Tracks per-user state for shared recordings (notes, highlights, etc)."""

    __tablename__ = 'shared_recording_state'

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)

    # User-specific state
    personal_notes = db.Column(db.Text, nullable=True)  # Private notes only this user can see
    is_inbox = db.Column(db.Boolean, default=True)  # User's personal inbox status
    is_highlighted = db.Column(db.Boolean, default=False)  # User's personal highlight/favorite status
    last_viewed = db.Column(db.DateTime, default=datetime.utcnow)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    recording = db.relationship('Recording', backref=db.backref('user_states', lazy=True, cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('recording_states', lazy=True))

    # Unique constraint: one state per user per recording
    __table_args__ = (db.UniqueConstraint('recording_id', 'user_id', name='unique_user_recording_state'),)

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'recording_id': self.recording_id,
            'user_id': self.user_id,
            'personal_notes': self.personal_notes,
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'last_viewed': self.last_viewed.isoformat() if self.last_viewed else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
