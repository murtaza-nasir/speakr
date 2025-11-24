"""
Database models package for the Speakr application.

This package contains all database models organized by domain:
- User and authentication models
- Recording and transcript models
- Sharing models (public and internal)
- Organization models (groups and tags)
- Event, template, and search session models
- System configuration models
"""

# Import database instance
from src.database import db

# Import all models
from .user import User, Speaker
from .speaker_snippet import SpeakerSnippet
from .recording import Recording, TranscriptChunk
from .sharing import Share, InternalShare, SharedRecordingState
from .organization import Group, GroupMembership, Tag, RecordingTag
from .events import Event
from .templates import TranscriptTemplate
from .inquire import InquireSession
from .system import SystemSetting
from .audit import ShareAuditLog

# Export all models
__all__ = [
    # Database instance
    'db',
    # User models
    'User',
    'Speaker',
    'SpeakerSnippet',
    # Recording models
    'Recording',
    'TranscriptChunk',
    # Sharing models
    'Share',
    'InternalShare',
    'SharedRecordingState',
    'ShareAuditLog',
    # Organization models
    'Group',
    'GroupMembership',
    'Tag',
    'RecordingTag',
    # Other models
    'Event',
    'TranscriptTemplate',
    'InquireSession',
    'SystemSetting',
]
