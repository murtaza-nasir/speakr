"""
Recording and TranscriptChunk database models.

This module defines models for audio recordings and their chunked transcriptions.
"""

import logging
import os
from datetime import datetime
from sqlalchemy import func
from src.database import db
from src.utils import local_datetime_filter, md_to_html

logger = logging.getLogger(__name__)


class Recording(db.Model):
    """Main recording model storing audio files and their metadata."""

    # Add user_id foreign key to associate recordings with users
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    id = db.Column(db.Integer, primary_key=True)
    # Title will now often be AI-generated, maybe start with filename?
    title = db.Column(db.String(200), nullable=True)  # Allow Null initially
    participants = db.Column(db.String(500))
    notes = db.Column(db.Text)
    transcription = db.Column(db.Text, nullable=True)
    summary = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='PENDING')  # PENDING, PROCESSING, SUMMARIZING, COMPLETED, FAILED
    audio_path = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    meeting_date = db.Column(db.DateTime, nullable=True)
    file_size = db.Column(db.Integer)  # Store file size in bytes
    original_filename = db.Column(db.String(500), nullable=True)  # Store the original uploaded filename
    is_inbox = db.Column(db.Boolean, default=True)  # New recordings are marked as inbox by default
    is_highlighted = db.Column(db.Boolean, default=False)  # Recordings can be highlighted by the user
    mime_type = db.Column(db.String(100), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    processing_time_seconds = db.Column(db.Integer, nullable=True)
    transcription_duration_seconds = db.Column(db.Integer, nullable=True)  # Time taken for transcription
    summarization_duration_seconds = db.Column(db.Integer, nullable=True)  # Time taken for summarization
    processing_source = db.Column(db.String(50), default='upload')  # upload, auto_process, recording
    error_message = db.Column(db.Text, nullable=True)  # Store detailed error messages

    # Auto-deletion and archival fields
    audio_deleted_at = db.Column(db.DateTime, nullable=True)  # When audio file was deleted (null = not deleted)
    deletion_exempt = db.Column(db.Boolean, default=False)  # Manual exemption from auto-deletion

    # Speaker embeddings from diarization (JSON dict mapping speaker IDs to 256-dimensional vectors)
    speaker_embeddings = db.Column(db.JSON, nullable=True)

    # Folder relationship (one-to-many: a recording belongs to at most one folder)
    folder_id = db.Column(db.Integer, db.ForeignKey('folder.id', ondelete='SET NULL'), nullable=True, index=True)

    # Relationships
    folder = db.relationship('Folder', back_populates='recordings')
    tag_associations = db.relationship('RecordingTag', back_populates='recording', cascade='all, delete-orphan', order_by='RecordingTag.order')

    @property
    def tags(self):
        """Get tags ordered by the order they were added to this recording."""
        return [assoc.tag for assoc in sorted(self.tag_associations, key=lambda x: x.order)]

    def get_visible_tags(self, viewer_user):
        """
        Get tags that are visible to a specific user viewing this recording.

        Visibility rules:
        - Group tags: visible if viewer is a member of the tag's group
        - Personal tags: visible only to the tag creator

        Note: These rules apply to ALL users, including the recording owner.
        Personal tags are private to their creator regardless of recording ownership.

        Args:
            viewer_user: User object viewing the recording (or None for backward compatibility)

        Returns:
            List of Tag objects visible to the viewer
        """
        # If no viewer specified, return all tags (backward compatibility)
        if viewer_user is None:
            return self.tags

        if not self.tags:
            return []

        # Import here to avoid circular dependencies
        from src.models.organization import GroupMembership

        visible_tags = []
        for tag in self.tags:
            # Group tags: visible if viewer is a member of the group
            if tag.group_id:
                membership = GroupMembership.query.filter_by(
                    group_id=tag.group_id,
                    user_id=viewer_user.id
                ).first()
                if membership:
                    visible_tags.append(tag)
            # Personal tags: visible only to tag creator
            else:
                if tag.user_id == viewer_user.id:
                    visible_tags.append(tag)

        return visible_tags

    def get_user_notes(self, user):
        """
        Get notes from user's perspective (owner or shared recipient).

        - Recording owner sees Recording.notes
        - Shared users see their personal_notes from SharedRecordingState

        Args:
            user: User object viewing the recording

        Returns:
            String notes content or None
        """
        if user is None:
            return self.notes

        if self.user_id == user.id:
            return self.notes  # Owner sees Recording.notes
        else:
            # Shared user sees their personal notes
            from src.models.sharing import SharedRecordingState
            state = SharedRecordingState.query.filter_by(
                recording_id=self.id,
                user_id=user.id
            ).first()
            return state.personal_notes if state else None

    def get_audio_duration(self):
        """
        Get the audio duration in seconds using ffprobe.

        Returns:
            Float duration in seconds, or None if unavailable
        """
        if self.audio_deleted_at is not None:
            return None

        if not self.audio_path or not os.path.exists(self.audio_path):
            return None

        try:
            from src.utils.ffprobe import get_duration
            # Allow longer timeout for packet scanning fallback on files without duration metadata
            duration = get_duration(self.audio_path, timeout=30)
            return duration
        except Exception as e:
            logger.warning(f"Failed to get duration for recording {self.id}: {e}")
            return None

    def to_list_dict(self, viewer_user=None):
        """
        Lightweight dict for list views - excludes expensive HTML conversions.

        Args:
            viewer_user: User viewing the recording (for tag visibility filtering)
        """
        # Import here to avoid circular dependencies
        from src.models.sharing import InternalShare, Share

        # Count internal shares for this recording
        shared_with_count = db.session.query(func.count(InternalShare.id)).filter(
            InternalShare.recording_id == self.id
        ).scalar() or 0

        # Count public shares (link shares) for this recording
        public_share_count = db.session.query(func.count(Share.id)).filter(
            Share.recording_id == self.id
        ).scalar() or 0

        # Get visible tags for this viewer
        visible_tags = self.get_visible_tags(viewer_user)

        return {
            'id': self.id,
            'title': self.title,
            'participants': self.participants,
            'status': self.status,
            'created_at': local_datetime_filter(self.created_at),
            'completed_at': local_datetime_filter(self.completed_at),
            'meeting_date': local_datetime_filter(self.meeting_date),
            'file_size': self.file_size,
            'original_filename': self.original_filename,
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'audio_deleted_at': local_datetime_filter(self.audio_deleted_at),
            'audio_available': self.audio_deleted_at is None,
            'deletion_exempt': self.deletion_exempt,
            'folder_id': self.folder_id,
            'folder': self.folder.to_dict() if self.folder else None,
            'tags': [tag.to_dict() for tag in visible_tags] if visible_tags else [],
            'shared_with_count': shared_with_count,
            'public_share_count': public_share_count
        }

    def to_dict(self, include_html=True, viewer_user=None):
        """
        Full dict with optional HTML conversion for notes/summary.

        Args:
            include_html: Whether to include HTML-rendered markdown fields
            viewer_user: User viewing the recording (for tag visibility filtering)
        """
        # Import here to avoid circular dependencies
        from src.models.sharing import InternalShare, Share

        # Count internal shares for this recording
        shared_with_count = db.session.query(func.count(InternalShare.id)).filter(
            InternalShare.recording_id == self.id
        ).scalar() or 0

        # Count public shares (link shares) for this recording
        public_share_count = db.session.query(func.count(Share.id)).filter(
            Share.recording_id == self.id
        ).scalar() or 0

        # Get visible tags for this viewer
        visible_tags = self.get_visible_tags(viewer_user)

        # Get user-specific notes
        user_notes = self.get_user_notes(viewer_user)

        data = {
            'id': self.id,
            'title': self.title,
            'participants': self.participants,
            'notes': user_notes,
            'transcription': self.transcription,
            'summary': self.summary,
            'status': self.status,
            'created_at': local_datetime_filter(self.created_at),
            'completed_at': local_datetime_filter(self.completed_at),
            'processing_time_seconds': self.processing_time_seconds,
            'transcription_duration_seconds': self.transcription_duration_seconds,
            'summarization_duration_seconds': self.summarization_duration_seconds,
            'meeting_date': local_datetime_filter(self.meeting_date),
            'file_size': self.file_size,
            'original_filename': self.original_filename,
            'user_id': self.user_id,
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'mime_type': self.mime_type,
            'audio_deleted_at': local_datetime_filter(self.audio_deleted_at),
            'audio_available': self.audio_deleted_at is None,
            'audio_duration': self.get_audio_duration(),
            'deletion_exempt': self.deletion_exempt,
            'folder_id': self.folder_id,
            'folder': self.folder.to_dict() if self.folder else None,
            'tags': [tag.to_dict() for tag in visible_tags] if visible_tags else [],
            'events': [event.to_dict() for event in self.events] if self.events else [],
            'shared_with_count': shared_with_count,
            'public_share_count': public_share_count
        }

        # Only compute expensive HTML conversions when explicitly requested
        if include_html:
            data['notes_html'] = md_to_html(user_notes) if user_notes else ""
            data['summary_html'] = md_to_html(self.summary) if self.summary else ""
        else:
            data['notes_html'] = ""
            data['summary_html'] = ""

        return data


class TranscriptChunk(db.Model):
    """Stores chunked transcription segments for efficient retrieval and embedding."""

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    chunk_index = db.Column(db.Integer, nullable=False)  # Order within the recording
    content = db.Column(db.Text, nullable=False)  # The actual text chunk
    start_time = db.Column(db.Float, nullable=True)  # Start time in seconds (if available)
    end_time = db.Column(db.Float, nullable=True)  # End time in seconds (if available)
    speaker_name = db.Column(db.String(100), nullable=True, index=True)  # Speaker for this chunk (indexed for speaker rename operations)
    embedding = db.Column(db.LargeBinary, nullable=True)  # Stored as binary vector
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Composite index for efficient speaker name lookups scoped to user
    __table_args__ = (
        db.Index('idx_user_speaker_name', 'user_id', 'speaker_name'),
    )

    # Relationships
    recording = db.relationship('Recording', backref=db.backref('chunks', lazy=True, cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('transcript_chunks', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'recording_id': self.recording_id,
            'chunk_index': self.chunk_index,
            'content': self.content,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'speaker_name': self.speaker_name,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
