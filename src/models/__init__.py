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
from .api_token import APIToken
from .speaker_snippet import SpeakerSnippet
from .recording import Recording, TranscriptChunk
from .sharing import Share, InternalShare, SharedRecordingState
from .organization import Group, GroupMembership, Tag, RecordingTag, Folder
from .events import Event
from .templates import TranscriptTemplate
from .naming_template import NamingTemplate
from .export_template import ExportTemplate
from .initial_prompt_template import InitialPromptTemplate
from .inquire import InquireSession
from .system import SystemSetting
from .audit import ShareAuditLog
from .push_subscription import PushSubscription
from .processing_job import ProcessingJob
from .recording_session import RecordingSession, RECORDING_SESSION_STATUSES
from .token_usage import TokenUsage
from .transcription_usage import TranscriptionUsage
from .webhook import (
    Webhook,
    WebhookDelivery,
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_DELIVERY_STATUSES,
    generate_webhook_secret,
)

# Export all models
__all__ = [
    # Database instance
    'db',
    # User models
    'User',
    'Speaker',
    'APIToken',
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
    'Folder',
    # Other models
    'Event',
    'TranscriptTemplate',
    'NamingTemplate',
    'ExportTemplate',
    'InitialPromptTemplate',
    'InquireSession',
    'SystemSetting',
    'PushSubscription',
    'ProcessingJob',
    'RecordingSession',
    'RECORDING_SESSION_STATUSES',
    'TokenUsage',
    'TranscriptionUsage',
    'Webhook',
    'WebhookDelivery',
    'WEBHOOK_EVENT_TYPES',
    'WEBHOOK_DELIVERY_STATUSES',
    'generate_webhook_secret',
]
