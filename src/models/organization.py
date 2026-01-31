"""
Organization models for groups, tags, and related structures.

This module defines models for organizing users into groups and tagging recordings.
"""

from datetime import datetime
from src.database import db


class Group(db.Model):
    """Groups for organizing users and sharing recordings."""

    __tablename__ = 'group'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    memberships = db.relationship('GroupMembership', back_populates='group', cascade='all, delete-orphan')

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'member_count': len(self.memberships),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class GroupMembership(db.Model):
    """Tracks user membership in groups with roles."""

    __tablename__ = 'group_membership'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    role = db.Column(db.String(20), default='member')  # 'admin' or 'member'
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    group = db.relationship('Group', back_populates='memberships')
    user = db.relationship('User', backref=db.backref('group_memberships', lazy=True))

    # Unique constraint: user can only be in a group once
    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='unique_group_membership'),)

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'group_id': self.group_id,
            'group_name': self.group.name if self.group else None,
            'user_id': self.user_id,
            'username': self.user.username if self.user else None,
            'role': self.role,
            'joined_at': self.joined_at.isoformat() if self.joined_at else None
        }


class RecordingTag(db.Model):
    """Many-to-many relationship table for recordings and tags."""

    __tablename__ = 'recording_tags'

    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), primary_key=True)
    tag_id = db.Column(db.Integer, db.ForeignKey('tag.id'), primary_key=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    order = db.Column(db.Integer, nullable=False, default=0)

    # Relationships
    recording = db.relationship('Recording', back_populates='tag_associations')
    tag = db.relationship('Tag', back_populates='recording_associations')


class Folder(db.Model):
    """Folders for organizing recordings (one-to-many relationship)."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id', ondelete='CASCADE'), nullable=True)  # Group-scoped folder
    color = db.Column(db.String(7), default='#10B981')  # Hex color for UI (green to differentiate from tags)

    # Custom settings for this folder
    custom_prompt = db.Column(db.Text, nullable=True)  # Custom summarization prompt
    default_language = db.Column(db.String(10), nullable=True)  # Default transcription language
    default_min_speakers = db.Column(db.Integer, nullable=True)  # Default min speakers for ASR
    default_max_speakers = db.Column(db.Integer, nullable=True)  # Default max speakers for ASR

    # Retention and deletion settings
    protect_from_deletion = db.Column(db.Boolean, default=False)  # Exempt recordings in folder from auto-deletion
    retention_days = db.Column(db.Integer, nullable=True)  # Folder-specific retention override

    # Group folder settings
    auto_share_on_apply = db.Column(db.Boolean, default=True)  # Auto-share recording with group when moved to folder
    share_with_group_lead = db.Column(db.Boolean, default=True)  # Share with group admins when moved to folder

    # Naming template for recordings in this folder
    naming_template_id = db.Column(db.Integer, db.ForeignKey('naming_template.id', ondelete='SET NULL'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('folders', lazy=True, cascade='all, delete-orphan'))
    group = db.relationship('Group', backref=db.backref('folders', lazy=True))
    naming_template = db.relationship('NamingTemplate', foreign_keys=[naming_template_id])
    # One-to-many relationship with recordings
    recordings = db.relationship('Recording', back_populates='folder', lazy=True)

    # Unique constraint: folder name must be unique per user
    __table_args__ = (db.UniqueConstraint('name', 'user_id', name='_user_folder_uc'),)

    @property
    def is_group_folder(self):
        """Check if this is a group-scoped folder."""
        return self.group_id is not None

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'color': self.color,
            'group_id': self.group_id,
            'is_group_folder': self.is_group_folder,
            'group_name': self.group.name if self.group else None,
            'custom_prompt': self.custom_prompt,
            'default_language': self.default_language,
            'default_min_speakers': self.default_min_speakers,
            'default_max_speakers': self.default_max_speakers,
            'protect_from_deletion': self.protect_from_deletion,
            'retention_days': self.retention_days,
            'auto_share_on_apply': self.auto_share_on_apply,
            'share_with_group_lead': self.share_with_group_lead,
            'naming_template_id': self.naming_template_id,
            'naming_template_name': self.naming_template.name if self.naming_template else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'recording_count': len(self.recordings) if self.recordings else 0
        }


class Tag(db.Model):
    """Tags for organizing and categorizing recordings."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id', ondelete='CASCADE'), nullable=True)  # Group-scoped tag
    color = db.Column(db.String(7), default='#3B82F6')  # Hex color for UI

    # Custom settings for this tag
    custom_prompt = db.Column(db.Text, nullable=True)  # Custom summarization prompt
    default_language = db.Column(db.String(10), nullable=True)  # Default transcription language
    default_min_speakers = db.Column(db.Integer, nullable=True)  # Default min speakers for ASR
    default_max_speakers = db.Column(db.Integer, nullable=True)  # Default max speakers for ASR

    # Retention and deletion settings
    protect_from_deletion = db.Column(db.Boolean, default=False)  # Exempt tagged recordings from auto-deletion
    retention_days = db.Column(db.Integer, nullable=True)  # Group-specific retention override (overrides global)

    # Group tag settings
    auto_share_on_apply = db.Column(db.Boolean, default=True)  # Auto-share recording with group when this tag is applied
    share_with_group_lead = db.Column(db.Boolean, default=True)  # Share with group admins when this tag is applied

    # Naming template for recordings with this tag
    naming_template_id = db.Column(db.Integer, db.ForeignKey('naming_template.id', ondelete='SET NULL'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('tags', lazy=True, cascade='all, delete-orphan'))
    group = db.relationship('Group', backref=db.backref('tags', lazy=True))
    naming_template = db.relationship('NamingTemplate', foreign_keys=[naming_template_id])
    # Use association object for many-to-many with order tracking
    recording_associations = db.relationship('RecordingTag', back_populates='tag', cascade='all, delete-orphan')

    # Unique constraint: tag name must be unique per user (or per group if group_id is set)
    __table_args__ = (db.UniqueConstraint('name', 'user_id', name='_user_tag_uc'),)

    @property
    def is_group_tag(self):
        """Check if this is a group-scoped tag."""
        return self.group_id is not None

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'color': self.color,
            'group_id': self.group_id,
            'is_group_tag': self.is_group_tag,
            'group_name': self.group.name if self.group else None,
            'custom_prompt': self.custom_prompt,
            'default_language': self.default_language,
            'default_min_speakers': self.default_min_speakers,
            'default_max_speakers': self.default_max_speakers,
            'protect_from_deletion': self.protect_from_deletion,
            'retention_days': self.retention_days,
            'auto_share_on_apply': self.auto_share_on_apply,
            'share_with_group_lead': self.share_with_group_lead,
            'naming_template_id': self.naming_template_id,
            'naming_template_name': self.naming_template.name if self.naming_template else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'recording_count': len(self.recording_associations)
        }
