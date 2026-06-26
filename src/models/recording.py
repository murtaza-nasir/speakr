"""
Recording and TranscriptChunk database models.

This module defines models for audio recordings and their chunked transcriptions.
"""

import logging
import os
from datetime import datetime
from sqlalchemy import func
from src.database import db
from src.utils import md_to_html

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
    audio_duration_seconds = db.Column(db.Float, nullable=True)  # Cached audio duration to avoid materializing remote storage during serialization
    completed_at = db.Column(db.DateTime, nullable=True)
    processing_time_seconds = db.Column(db.Integer, nullable=True)
    transcription_duration_seconds = db.Column(db.Integer, nullable=True)  # Time taken for transcription
    # Cached audio duration in seconds, populated at transcription
    # completion. Lets to_dict() / API list views avoid an ffprobe
    # subprocess per row. Falls back to live ffprobe in
    # get_audio_duration() only when the column is empty.
    audio_duration_seconds = db.Column(db.Float, nullable=True)
    summarization_duration_seconds = db.Column(db.Integer, nullable=True)  # Time taken for summarization
    processing_source = db.Column(db.String(50), default='upload')  # upload, auto_process, recording
    error_message = db.Column(db.Text, nullable=True)  # Store detailed error messages
    file_hash = db.Column(db.String(64), nullable=True)  # SHA-256 hash for duplicate detection

    # Per-upload override for VIDEO_RETENTION. When True, the processing
    # pipeline extracts audio and discards the video stream even if the
    # server's VIDEO_RETENTION is on. Also signals that the upload was
    # allowed to exceed max_file_size_mb because only the extracted audio
    # has to fit that limit. See docs/admin-guide/configuration.md.
    keep_audio_only = db.Column(db.Boolean, default=False, nullable=False)

    # Auto-deletion and archival fields
    audio_deleted_at = db.Column(db.DateTime, nullable=True)  # When audio file was deleted (null = not deleted)
    deletion_exempt = db.Column(db.Boolean, default=False)  # Manual exemption from auto-deletion

    # Speaker embeddings from diarization (JSON dict mapping speaker IDs to 256-dimensional vectors)
    speaker_embeddings = db.Column(db.JSON, nullable=True)

    # Per-recording prompt-template variables (e.g. {"agenda": "...", "attendees": "..."}).
    # Substituted into {{name}} placeholders in the resolved summary prompt at summarisation time.
    prompt_variables = db.Column(db.JSON, nullable=True)

    # Effective transcription hints actually sent to the ASR for this recording,
    # captured at transcription time after tag/folder/user and admin-default
    # resolution. Persisted so the UI can show which hotwords/initial prompt were
    # used and pre-fill them on reprocess (issue #309). Null = none were applied.
    resolved_hotwords = db.Column(db.Text, nullable=True)
    resolved_initial_prompt = db.Column(db.Text, nullable=True)

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

    def get_audio_duration(self, allow_probe_fallback=False):
        """
        Get cached audio duration in seconds.

        By default this avoids materializing audio from storage (especially S3)
        during normal API serialization (which would otherwise turn a list
        serialization into an ffprobe subprocess per row). Optional probe
        fallback is kept for explicit callers that need best-effort backfill.

        Args:
            allow_probe_fallback: When True, probe the stored file if cached
                duration is missing.

        Returns:
            Float duration in seconds, or None if unavailable.
        """
        if self.audio_deleted_at is not None:
            return None
        if self.audio_duration_seconds is not None:
            try:
                return float(self.audio_duration_seconds)
            except (TypeError, ValueError):
                return None

        if not allow_probe_fallback:
            return None

        if not self.audio_path:
            return None
        try:
            from src.services.storage import get_storage_service
            from src.utils.ffprobe import get_duration
            storage = get_storage_service()
            if not storage.exists(self.audio_path):
                return None

            with storage.materialize(self.audio_path) as materialized:
                # Allow longer timeout for packet scanning fallback on files without duration metadata
                duration = get_duration(materialized.local_path, timeout=30)
                if duration is not None:
                    self.audio_duration_seconds = float(duration)
                return duration
        except Exception as e:
            logger.warning(f"Failed to get duration for recording {self.id}: {e}")
            return None

    @classmethod
    def get_duplicate_info_map(cls, user_id):
        """Pre-compute duplicate-info groupings for one user in a single query.

        Returns a dict keyed by file_hash. Each value is the
        ``{'total_copies': N, 'copies': [...]}`` payload that
        ``get_duplicate_info`` returns. List endpoints pass this map
        into ``to_dict`` / ``to_list_dict`` to avoid the per-row query.
        """
        rows = (
            cls.query
            .filter(cls.user_id == user_id, cls.file_hash != None)  # noqa: E711
            .with_entities(cls.id, cls.title, cls.created_at, cls.file_hash)
            .order_by(cls.created_at)
            .all()
        )
        grouped = {}
        for rid, title, created, fh in rows:
            grouped.setdefault(fh, []).append((rid, title, created))
        out = {}
        for fh, items in grouped.items():
            if len(items) <= 1:
                continue
            out[fh] = {
                'total_copies': len(items),
                'copies': [
                    {
                        'id': rid,
                        'title': title,
                        'created_at': created.isoformat() if created else None,
                    }
                    for (rid, title, created) in items
                ],
            }
        return out

    def get_duplicate_info(self):
        """Check if other recordings share the same file_hash for this user.

        Returns:
            Dict with total copy count and list of copies, or None.
        """
        if not self.file_hash:
            return None
        dupes = Recording.query.filter(
            Recording.user_id == self.user_id,
            Recording.file_hash == self.file_hash,
        ).with_entities(
            Recording.id, Recording.title, Recording.created_at
        ).order_by(Recording.created_at).all()
        if len(dupes) > 1:
            return {
                'total_copies': len(dupes),
                'copies': [
                    {
                        'id': d.id,
                        'title': d.title or f'#{d.id}',
                        'created_at': d.created_at.isoformat() if d.created_at else None,
                        'is_self': d.id == self.id
                    }
                    for d in dupes
                ]
            }
        return None

    def _serialize_tags_and_folder(self, viewer_user, share_map=None):
        """Build (tags, folder, folder_id) for a viewer, applying #314 rules.

        For a recording the viewer does NOT own, only the tag/folder that GRANTED
        access (the share-reason, from the InternalShare row) is surfaced, each
        flagged ``is_foreign: True`` so the UI can mark it and keep it
        non-filterable. The owner's other tags/folder are never leaked. Owners
        (and the no-viewer backward-compat case) see their tags/folder normally.

        share_map: optional {recording_id: InternalShare} to avoid an N+1 query
        in list views; falls back to a per-recording lookup when not provided.
        """
        visible_tags = self.get_visible_tags(viewer_user)
        is_foreign = viewer_user is not None and self.user_id != viewer_user.id

        if not is_foreign:
            tags = [tag.to_dict() for tag in visible_tags] if visible_tags else []
            folder = self.folder.to_dict() if self.folder else None
            return tags, folder, self.folder_id

        from src.models.sharing import InternalShare
        if share_map is not None:
            share = share_map.get(self.id)
        else:
            share = InternalShare.query.filter_by(
                recording_id=self.id, shared_with_user_id=viewer_user.id
            ).first()
        reason_tag_id = share.source_tag_id if share else None
        reason_folder_id = getattr(share, 'source_folder_id', None) if share else None

        tags = []
        if reason_tag_id:
            for tag in visible_tags:
                if tag.id == reason_tag_id:
                    td = tag.to_dict()
                    td['is_foreign'] = True
                    tags.append(td)
        if reason_folder_id and self.folder_id == reason_folder_id and self.folder:
            folder = self.folder.to_dict()
            folder['is_foreign'] = True
            return tags, folder, self.folder_id
        return tags, None, None

    def to_list_dict(self, viewer_user=None, duplicate_info_map=None, share_map=None):
        """
        Lightweight dict for list views - excludes expensive HTML conversions.

        Args:
            viewer_user: User viewing the recording (for tag visibility filtering)
            share_map: optional {recording_id: InternalShare} for #314 share-reason
                resolution without an N+1 query.
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

        tags_ser, folder_ser, folder_id_ser = self._serialize_tags_and_folder(viewer_user, share_map)

        return {
            'id': self.id,
            'title': self.title,
            'participants': self.participants,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'meeting_date': self.meeting_date.isoformat() if self.meeting_date else None,
            'file_size': self.file_size,
            'original_filename': self.original_filename,
            'mime_type': self.mime_type,  # cheap column read; lets the sidebar mark video recordings without opening them
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'audio_deleted_at': self.audio_deleted_at.isoformat() if self.audio_deleted_at else None,
            'audio_available': self.audio_deleted_at is None,
            'deletion_exempt': self.deletion_exempt,
            'folder_id': folder_id_ser,
            'folder': folder_ser,
            'tags': tags_ser,
            'duplicate_info': self._dup_from_map_or_query(duplicate_info_map),
            'shared_with_count': shared_with_count,
            'public_share_count': public_share_count,
            'audio_duration': self.get_audio_duration(),
            'keep_audio_only': self.keep_audio_only,
        }

    def _dup_from_map_or_query(self, duplicate_info_map):
        """Use a pre-batched duplicate-info map when provided, else fall
        back to the per-row query. The map is keyed by file_hash and is
        produced by ``get_duplicate_info_map`` for list endpoints; the
        legacy path stays correct for single-row callers (detail views,
        share endpoints, etc.)."""
        if duplicate_info_map is not None:
            if not self.file_hash:
                return None
            entry = duplicate_info_map.get(self.file_hash)
            if not entry:
                return None
            # Annotate is_self for the current row, matching the
            # legacy per-row payload shape.
            copies = []
            for c in entry['copies']:
                copies.append({**c, 'is_self': c['id'] == self.id})
            return {'total_copies': entry['total_copies'], 'copies': copies}
        return self.get_duplicate_info()

    def to_dict(self, include_html=True, viewer_user=None, duplicate_info_map=None, share_map=None):
        """
        Full dict with optional HTML conversion for notes/summary.

        Args:
            include_html: Whether to include HTML-rendered markdown fields
            viewer_user: User viewing the recording (for tag visibility filtering)
            share_map: optional {recording_id: InternalShare} for #314 share-reason
                resolution without an N+1 query.
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

        tags_ser, folder_ser, folder_id_ser = self._serialize_tags_and_folder(viewer_user, share_map)

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
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'processing_time_seconds': self.processing_time_seconds,
            'transcription_duration_seconds': self.transcription_duration_seconds,
            'summarization_duration_seconds': self.summarization_duration_seconds,
            'meeting_date': self.meeting_date.isoformat() if self.meeting_date else None,
            'file_size': self.file_size,
            'original_filename': self.original_filename,
            'user_id': self.user_id,
            'is_inbox': self.is_inbox,
            'is_highlighted': self.is_highlighted,
            'mime_type': self.mime_type,
            'audio_deleted_at': self.audio_deleted_at.isoformat() if self.audio_deleted_at else None,
            'audio_available': self.audio_deleted_at is None,
            'audio_duration': self.get_audio_duration(allow_probe_fallback=False),
            'deletion_exempt': self.deletion_exempt,
            'folder_id': folder_id_ser,
            'folder': folder_ser,
            'tags': tags_ser,
            'events': [event.to_dict() for event in self.events] if self.events else [],
            'prompt_variables': self.prompt_variables or {},
            'resolved_hotwords': self.resolved_hotwords,
            'resolved_initial_prompt': self.resolved_initial_prompt,
            'duplicate_info': self._dup_from_map_or_query(duplicate_info_map),
            'shared_with_count': shared_with_count,
            'public_share_count': public_share_count,
            'keep_audio_only': self.keep_audio_only,
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
