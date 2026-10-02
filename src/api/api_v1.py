"""
API v1 - RESTful API for external integrations.

This blueprint provides a comprehensive REST API for:
- Dashboard widgets (gethomepage.dev, etc.)
- Automation tools (n8n, Zapier, etc.)
- Third-party integrations

All endpoints require token authentication via:
- Authorization: Bearer <token>
- X-API-Token: <token>
- API-Token: <token>
- ?token=<token> query parameter
"""

import os
import re
import json
import math
from datetime import datetime, date, timedelta, timezone
from functools import wraps

from src.utils.dates import to_utc_naive
from typing import Optional

from flask import Blueprint, jsonify, request, current_app, send_file, redirect
from flask_login import login_required, current_user
from src.utils.token_auth import require_scope, token_rate_key
from sqlalchemy import func, extract, or_, and_
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from src.database import db
from src.services.recording_state import get_user_archived, set_user_archived, parse_archived_flag
from src.models import Recording, User, Tag, RecordingTag, Speaker, Event
from src.models.processing_job import ProcessingJob
from src.models.token_usage import TokenUsage
from src.models.transcription_usage import TranscriptionUsage
from src.services.token_tracking import TokenTracker
from src.services.transcription_tracking import transcription_tracker
from src.file_exporter import format_transcription_with_template
from src.api.recordings import (
    ingest_uploaded_recording,
    upload_file as _upload_file_ui,
)
from src.services.storage import get_storage_service
from src.utils.token_auth import load_user_from_token_value

# Create blueprint with /api/v1 prefix
api_v1_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')

# Global helpers (will be injected from app)
has_recording_access = None
get_user_recording_status = None
set_user_recording_status = None
enrich_recording_dict_with_user_status = None
bcrypt = None
csrf = None
limiter = None
chunking_service = None

# Token tracker instance
token_tracker = TokenTracker()


def init_api_v1_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, get_user_recording_status, set_user_recording_status
    global enrich_recording_dict_with_user_status, bcrypt, csrf, limiter, chunking_service
    has_recording_access = kwargs.get('has_recording_access')
    get_user_recording_status = kwargs.get('get_user_recording_status')
    set_user_recording_status = kwargs.get('set_user_recording_status')
    enrich_recording_dict_with_user_status = kwargs.get('enrich_recording_dict_with_user_status')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')
    chunking_service = kwargs.get('chunking_service')


def rate_limit(limit_string, **limit_options):
    """Apply an endpoint limit after the app injects Flask-Limiter."""
    def decorator(f):
        state = {'limited': None}

        @wraps(f)
        def wrapper(*args, **kwargs):
            if limiter is not None and getattr(limiter, 'enabled', True):
                if state['limited'] is None:
                    state['limited'] = limiter.limit(limit_string, **limit_options)(f)
                return state['limited'](*args, **kwargs)
            return f(*args, **kwargs)

        wrapper._rate_limit = limit_string
        wrapper._rate_limit_options = limit_options
        return wrapper
    return decorator


def format_bytes(bytes_value: int) -> str:
    """Format bytes to human-readable string."""
    if bytes_value is None:
        bytes_value = 0
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.1f} PB"


# =============================================================================
# OpenAPI Documentation
# =============================================================================

OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Speakr API",
        "description": "REST API for Speakr - Audio transcription and note-taking application.\n\n## Authentication\nMost endpoints require token authentication via one of:\n- `Authorization: Bearer <token>`\n- `X-API-Token: <token>`\n- `API-Token: <token>`\n- `?token=<token>` query parameter\n\nThe ASR Voice Recorder integration is the one exception: its required multipart `secret` field contains the personal Speakr API token because that client cannot set custom authorization headers. Generate tokens in Settings > API Tokens.",
        "version": "1.0.0"
    },
    "servers": [{"url": "/api/v1", "description": "API v1"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer",
                           "description": "A personal API token. Tokens may carry scopes: read, write, upload, "
                                          "process, share, delete, webhooks, account. A token with no scopes "
                                          "(reported as [\"full\"]) has every scope. Each operation lists the "
                                          "scopes it needs in x-required-scopes; a scoped token without them "
                                          "gets 403 insufficient_scope. Scoped tokens work only in a header."},
            "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Token"},
            "apiKeyQuery": {"type": "apiKey", "in": "query", "name": "token"}
        },
        "schemas": {
            "Recording": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["PENDING", "PROCESSING", "SUMMARIZING", "COMPLETED", "FAILED"]},
                    "created_at": {"type": "string", "format": "date-time"},
                    "meeting_date": {"type": "string", "format": "date-time"},
                    "file_size": {"type": "integer"},
                    "audio_duration": {"type": "number", "description": "Audio duration in seconds"},
                    "transcription_duration_seconds": {"type": "integer", "description": "Wall-clock seconds spent transcribing"},
                    "summarization_duration_seconds": {"type": "integer", "description": "Wall-clock seconds spent summarizing"},
                    "participants": {"type": "string"},
                    "is_inbox": {"type": "boolean"},
                    "is_highlighted": {"type": "boolean"},
                    "is_archived": {"type": "boolean", "description": "Hidden from the main list in the web app; nothing is deleted"},
                    "deletion_exempt": {"type": "boolean", "description": "If true, recording is exempt from auto-deletion"},
                    "prompt_variables": {"type": "object", "description": "Per-recording {{name}} substitutions used when summarising"},
                    "folder_id": {"type": "integer", "nullable": True},
                    "folder": {"type": "object", "nullable": True, "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}},
                    "events": {"type": "array", "description": "Calendar events extracted from the recording (detail endpoint only)", "items": {"type": "object"}},
                    "tags": {"type": "array", "items": {"$ref": "#/components/schemas/Tag"}},
                    "keep_audio_only": {"type": "boolean", "description": "True if the upload was processed in audio-only mode (video stream discarded). Set at upload time; immutable via PATCH."},
                    "updated_at": {"type": "string", "format": "date-time", "description": "Last change a client can see; starts at created_at"},
                    "is_shared": {"type": "boolean"},
                    "owner": {"type": "object", "nullable": True},
                    "share": {"type": "object", "nullable": True},
                    "external_refs": {"type": "array", "items": {"type": "object"}}
                }
            },
            "Tag": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "custom_prompt": {"type": "string"},
                    "title_prompt": {"type": "string"},
                    "default_language": {"type": "string"},
                    "default_min_speakers": {"type": "integer"},
                    "default_max_speakers": {"type": "integer"},
                    "default_hotwords": {"type": "string"},
                    "default_initial_prompt": {"type": "string"},
                    "default_transcription_model": {"type": "string"}
                }
            },
            "Folder": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "group_id": {"type": "integer", "nullable": True},
                    "is_group_folder": {"type": "boolean"},
                    "custom_prompt": {"type": "string"},
                    "title_prompt": {"type": "string"},
                    "default_language": {"type": "string"},
                    "default_min_speakers": {"type": "integer"},
                    "default_max_speakers": {"type": "integer"},
                    "default_hotwords": {"type": "string"},
                    "default_initial_prompt": {"type": "string"},
                    "default_transcription_model": {"type": "string"},
                    "protect_from_deletion": {"type": "boolean"},
                    "retention_days": {"type": "integer", "nullable": True},
                    "recording_count": {"type": "integer"}
                }
            },
            "Speaker": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "use_count": {"type": "integer"},
                    "has_voice_profile": {"type": "boolean"}
                }
            },
            "Error": {
                "type": "object",
                "properties": {"error": {"type": "string"},
                               "code": {"type": "string", "description": "Machine-readable error code, e.g. insufficient_scope"}}
            },
            "TokenInfo": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "scopes": {"type": "array", "items": {"type": "string"}},
                    "created_at": {"type": "string", "format": "date-time"},
                    "expires_at": {"type": "string", "format": "date-time", "nullable": True},
                    "last_used_at": {"type": "string", "format": "date-time", "nullable": True},
                    "via": {"type": "string", "enum": ["header", "query"]}
                }
            },
            "Capabilities": {
                "type": "object",
                "properties": {
                    "speakr_version": {"type": "string"},
                    "api_version": {"type": "string"},
                    "features": {"type": "object", "additionalProperties": True,
                                 "description": "Feature flags; a missing key means false"},
                    "models_local": {"type": "boolean"}
                }
            }
        }
    },
    "security": [{"bearerAuth": []}, {"apiKeyHeader": []}, {"apiKeyQuery": []}],
    "paths": {
        "/tokens/current": {
            "get": {
                "tags": ["Tokens"],
                "summary": "The API token used for this request",
                "description": "Any valid token, no scope needed. A session request gets 404.",
                "responses": {"200": {"description": "Token details",
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TokenInfo"}}}},
                              "404": {"description": "No API token in this request"}}
            }
        },
        "/capabilities": {
            "get": {
                "tags": ["Tokens"],
                "summary": "Features this instance supports",
                "description": "Any valid token or session. Clients read features here, never from the version string.",
                "responses": {"200": {"description": "Capabilities",
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Capabilities"}}}}}
            }
        },
        "/search": {
            "get": {
                "tags": ["Recordings"],
                "summary": "Search titles, participants, notes, summaries and transcripts",
                "description": "Keyword search over every recording you own, or semantic search through the Inquire index (mode=semantic). Transcript hits carry segment_index, start_time, end_time and speaker.",
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "mode", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "fields", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "scope", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "recording_ids", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "tag_id", "in": "query", "required": False, "schema": {"type": "integer"}},
                    {"name": "folder_id", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "speaker", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "date_from", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "date_to", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "date_field", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}},
                    {"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "Hits"}, "400": {"description": "invalid_parameter"},
                              "409": {"description": "semantic_unavailable"}}
            }
        },
        "/recordings/changes": {
            "get": {
                "tags": ["Recordings"],
                "summary": "Changes to your recordings since a cursor",
                "description": "Each create, edit and delete once, in latest state. Store next_cursor after every answer. 410 cursor_expired: start again without a cursor.",
                "parameters": [
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500}},
                    {"name": "scope", "in": "query", "schema": {"type": "string", "enum": ["own", "shared", "all"], "default": "own"}}
                ],
                "responses": {
                    "200": {"description": "A page of changes", "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "changes": {"type": "array", "items": {"type": "object", "properties": {
                                "type": {"type": "string", "enum": ["upsert", "delete"]},
                                "recording": {"$ref": "#/components/schemas/Recording"},
                                "id": {"type": "integer"},
                                "deleted_at": {"type": "string", "format": "date-time"},
                                "reason": {"type": "string", "enum": ["deleted", "retention", "access_revoked"]}}}},
                            "next_cursor": {"type": "string"},
                            "has_more": {"type": "boolean"}}}}}},
                    "400": {"description": "invalid_parameter"},
                    "410": {"description": "cursor_expired"}
                }
            }
        },
        "/stats": {
            "get": {
                "tags": ["Stats"],
                "summary": "Get system statistics",
                "description": "Returns stats compatible with gethomepage.dev widgets",
                "parameters": [{"name": "scope", "in": "query", "schema": {"type": "string", "enum": ["user", "all"], "default": "user"}, "description": "user=personal stats, all=global (admin only)"}],
                "responses": {"200": {"description": "Stats object"}}
            }
        },
        "/users/me": {
            "get": {
                "tags": ["Users"],
                "summary": "Get current user profile",
                "description": "Returns the authenticated user's profile (id, username, email, name, role flags, preferences) and group memberships.",
                "responses": {"200": {"description": "User profile object"}}
            }
        },
        "/recordings": {
            "get": {
                "tags": ["Recordings"],
                "summary": "List recordings",
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}},
                    {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 25, "maximum": 100}},
                    {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["all", "pending", "processing", "completed", "failed"]}},
                    {"name": "sort_by", "in": "query", "schema": {"type": "string", "enum": ["created_at", "meeting_date", "title", "file_size", "status", "updated_at"]}},
                    {"name": "updated_since", "in": "query", "schema": {"type": "string", "format": "date-time"}, "description": "Only recordings changed after this time"},
                    {"name": "date_field", "in": "query", "schema": {"type": "string", "enum": ["created_at", "meeting_date"], "default": "created_at"}, "description": "What date_from and date_to filter"},
                    {"name": "scope", "in": "query", "schema": {"type": "string", "enum": ["own", "shared", "all"], "default": "own"}},
                    {"name": "owner_id", "in": "query", "schema": {"type": "integer"}},
                    {"name": "sort_order", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}},
                    {"name": "tag_id", "in": "query", "schema": {"type": "integer"}},
                    {"name": "archived", "in": "query", "schema": {"type": "boolean"}, "description": "true: only archived recordings; false: only unarchived. Omitted: both, as before."},
                    {"name": "folder_id", "in": "query", "schema": {"type": "string"}, "description": "Filter by folder. Pass an integer folder id to list recordings in that folder, or the literal 'none' to list recordings not in any folder. Omit for no filter."},
                    {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Search query"}
                ],
                "responses": {"200": {"description": "Paginated list of recordings"}}
            }
        },
        "/recordings/{id}": {
            "get": {
                "tags": ["Recordings"],
                "summary": "Get recording details",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                    {"name": "format", "in": "query", "schema": {"type": "string", "enum": ["full", "minimal"]}, "description": "minimal excludes large text fields"},
                    {"name": "include", "in": "query", "schema": {"type": "string"}, "description": "Comma-separated: transcription,summary,notes"}
                ],
                "responses": {"200": {"description": "Recording details"}, "404": {"description": "Not found"}}
            },
            "patch": {
                "tags": ["Recordings"],
                "summary": "Update recording",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"title": {"type": "string"}, "participants": {"type": "string"}, "notes": {"type": "string"}, "summary": {"type": "string"}, "meeting_date": {"type": "string"}, "is_inbox": {"type": "boolean"}, "is_highlighted": {"type": "boolean"}, "is_archived": {"type": "boolean"}, "folder_id": {"type": "integer", "nullable": True, "description": "Move recording to this folder, or null to remove from any folder. Caller must have access to the target folder."}}}}}},
                "responses": {"200": {"description": "Updated recording"}, "403": {"description": "No access to target folder"}, "404": {"description": "Recording or folder not found"}}
            },
            "delete": {
                "tags": ["Recordings"],
                "summary": "Delete recording",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Deleted"}, "403": {"description": "Permission denied"}}
            }
        },
        "/recordings/{id}/transcript": {
            "get": {
                "tags": ["Recordings"],
                "summary": "Get transcript",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                    {"name": "format", "in": "query", "schema": {"type": "string", "enum": ["json", "text", "srt", "vtt"], "default": "json"}}
                ],
                "responses": {"200": {"description": "Transcript in requested format"}}
            }
        },
        "/recordings/{id}/summary": {
            "get": {"tags": ["Recordings"], "summary": "Get summary", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Summary markdown"}}},
            "put": {"tags": ["Recordings"], "summary": "Replace summary", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}}}}, "responses": {"200": {"description": "Updated"}}}
        },
        "/recordings/{id}/notes": {
            "get": {"tags": ["Recordings"], "summary": "Get notes", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Notes markdown"}}},
            "put": {"tags": ["Recordings"], "summary": "Replace notes", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["notes"], "properties": {"notes": {"type": "string"}}}}}}, "responses": {"200": {"description": "Updated"}}}
        },
        "/recordings/{id}/delete-audio": {
            "post": {"tags": ["Recordings"], "summary": "Delete audio, keep transcript", "description": "Deletes the media file and keeps the transcript, summary and notes. Same permission as deleting the recording. 409 if the audio is already removed or the recording is still processing.", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Audio removed"}, "403": {"description": "Permission denied"}, "404": {"description": "Recording not found"}, "409": {"description": "Already removed or still processing"}}}
        },
        "/recordings/{id}/status": {
            "get": {"tags": ["Recordings"], "summary": "Get processing status", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Status with queue position"}}}
        },
        "/recordings/{id}/transcribe": {
            "post": {"tags": ["Processing"], "summary": "Queue transcription", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"language": {"type": "string"}, "min_speakers": {"type": "integer"}, "max_speakers": {"type": "integer"}, "hotwords": {"type": "string", "description": "Connectors that accept hotword biasing route this to their native parameter (WhisperX hotwords, Mistral context_bias, OpenAI prompt, etc.). Connectors that ignore it drop it silently."}, "initial_prompt": {"type": "string", "description": "Free-text context hint. Same per-connector behaviour as hotwords."}, "transcription_model": {"type": "string", "description": "Per-request model override. Validated against the admin-curated visible-models list. Falls back to the configured default if absent or invalid."}}}}}}, "responses": {"200": {"description": "Job queued"}}}
        },
        "/recordings/{id}/summarize": {
            "post": {"tags": ["Processing"], "summary": "Queue summarization", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"custom_prompt": {"type": "string"}}}}}}, "responses": {"200": {"description": "Job queued"}}}
        },
        "/recordings/{id}/chat": {
            "post": {"tags": ["Chat"], "summary": "Chat about recording", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["message"], "properties": {"message": {"type": "string"}, "conversation_history": {"type": "array"}}}}}}, "responses": {"200": {"description": "Chat response"}}}
        },
        "/recordings/{id}/events": {
            "get": {"tags": ["Events"], "summary": "Get calendar events", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "List of events"}}}
        },
        "/recordings/{id}/events/ics": {
            "get": {"tags": ["Events"], "summary": "Download events as ICS", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "ICS file", "content": {"text/calendar": {}}}}}
        },
        "/recordings/{id}/audio": {
            "get": {"tags": ["Audio"], "summary": "Download audio", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}, {"name": "download", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": {"description": "Audio file"}}}
        },
        "/recordings/{id}/tags": {
            "post": {"tags": ["Tags"], "summary": "Add tags to recording", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"tag_ids": {"type": "array", "items": {"type": "integer"}}}}}}}, "responses": {"200": {"description": "Tags added"}}}
        },
        "/recordings/{id}/tags/{tag_id}": {
            "delete": {"tags": ["Tags"], "summary": "Remove tag from recording", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}, {"name": "tag_id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Tag removed"}}}
        },
        "/recordings/{id}/speakers": {
            "get": {"tags": ["Speakers"], "summary": "Get speakers in recording", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Speakers with suggestions"}}}
        },
        "/recordings/{id}/speakers/assign": {
            "put": {
                "tags": ["Speakers"],
                "summary": "Assign speaker names to transcription",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["speaker_map"], "properties": {"speaker_map": {"type": "object", "description": "Map of speaker labels to names. Values can be: string (name) or object {name, isMe}."}, "regenerate_summary": {"type": "boolean", "default": False}}}}}},
                "responses": {"200": {"description": "Speakers assigned"}, "404": {"description": "Recording not found"}, "403": {"description": "Permission denied"}}
            }
        },
        "/recordings/{id}/speakers/identify": {
            "post": {
                "tags": ["Speakers"],
                "summary": "Auto-identify speakers via LLM",
                "description": "Analyzes transcript context to suggest speaker names. Returns suggestions only - does not modify the recording.",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Speaker identification suggestions"}, "400": {"description": "Transcription not available or unsupported format"}}
            }
        },
        "/recordings/batch": {
            "patch": {"tags": ["Batch"], "summary": "Batch update recordings", "description": "Apply the same set of updates to multiple recordings in one call. Supported fields inside `updates`: `is_inbox`, `is_highlighted`, `is_archived`, `add_tag_ids` (array of tag ids to add), `remove_tag_ids` (array of tag ids to remove), `folder_id` (move all to this folder, or null to remove from any folder).", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["recording_ids", "updates"], "properties": {"recording_ids": {"type": "array", "items": {"type": "integer"}}, "updates": {"type": "object", "properties": {"is_inbox": {"type": "boolean"}, "is_highlighted": {"type": "boolean"}, "is_archived": {"type": "boolean"}, "add_tag_ids": {"type": "array", "items": {"type": "integer"}}, "remove_tag_ids": {"type": "array", "items": {"type": "integer"}}, "folder_id": {"type": "integer", "nullable": True, "description": "Target folder id, or null to remove all selected recordings from their folders. Caller must have access."}}}}}}}}, "responses": {"200": {"description": "Batch results"}, "403": {"description": "No access to target folder"}, "404": {"description": "Target folder not found"}}},
            "delete": {"tags": ["Batch"], "summary": "Batch delete recordings", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["recording_ids"], "properties": {"recording_ids": {"type": "array", "items": {"type": "integer"}}}}}}}, "responses": {"200": {"description": "Batch results"}}}
        },
        "/recordings/batch/transcribe": {
            "post": {"tags": ["Batch"], "summary": "Batch queue transcriptions", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["recording_ids"], "properties": {"recording_ids": {"type": "array", "items": {"type": "integer"}}}}}}}, "responses": {"200": {"description": "Batch results"}}}
        },
        "/recordings/upload": {
            "post": {
                "tags": ["Recordings"],
                "summary": "Upload a recording (multipart form-data) and queue transcription",
                "requestBody": {
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["file"],
                                "properties": {
                                    "file": {"type": "string", "format": "binary"},
                                    "notes": {"type": "string"},
                                    "title": {"type": "string"},
                                    "meeting_date": {"type": "string", "format": "date-time"},
                                    "file_last_modified": {"type": "string"},
                                    "language": {"type": "string"},
                                    "min_speakers": {"type": "integer"},
                                    "max_speakers": {"type": "integer"},
                                    "hotwords": {"type": "string"},
                                    "initial_prompt": {"type": "string"},
                                    "transcription_model": {"type": "string"},
                                    "prompt_variables": {"type": "string", "description": "JSON object of {variable_name: value} substituted into {{name}} placeholders in the summary prompt"},
                                    "folder_id": {"type": "integer"},
                                    "tag_id": {"type": "integer"},
                                    "tag_ids[0]": {"type": "integer"},
                                    "tag_ids[1]": {"type": "integer"},
                                    "keep_audio_only": {"type": "boolean", "description": "If true, the server discards the video stream and stores only the extracted audio. Allows uploads up to max_audio_only_video_size_mb (vs max_file_size_mb) for video files, as long as the extracted audio still fits the regular limit."}
                                }
                            }
                        }
                    }
                },
                "responses": {"202": {"description": "Upload accepted and queued"}}
            }
        },
        "/integrations/asr-voice-recorder/upload": {
            "post": {
                "tags": ["Integrations"],
                "summary": "Receive a completed ASR Voice Recorder recording",
                "description": (
                    "Accepts ASR Voice Recorder's multipart webhook format and queues the "
                    "recording through Speakr's normal transcription pipeline. It also accepts "
                    "ASR's authenticated, fileless connection test without creating a recording. "
                    "The required `secret` field must contain a personal Speakr API token. Session "
                    "cookies and the API's normal authentication headers do not authenticate this endpoint."
                ),
                "security": [],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["secret"],
                                "properties": {
                                    "file": {"type": "string", "format": "binary", "description": "Required for a completed recording upload; omitted by ASR's connection test"},
                                    "file_name": {"type": "string", "description": "Optional original filename override"},
                                    "secret": {"type": "string", "format": "password", "writeOnly": True, "description": "A personal Speakr API token"},
                                    "date": {"type": "integer", "format": "int64", "description": "Recording time as Unix epoch seconds or milliseconds"},
                                    "duration": {"type": "number", "description": "Accepted for ASR compatibility but ignored; Speakr measures the media duration"},
                                    "note": {"type": "string", "description": "Stored as the recording notes"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Connection test passed, recording accepted, or an idempotent retry matched an existing recording"},
                    "400": {"description": "Upload metadata was provided without a file, or the multipart request is invalid"},
                    "401": {"description": "Missing, invalid, expired, or revoked API token"},
                    "413": {"description": "Upload exceeds the configured size limit"},
                    "429": {"description": "Too many upload attempts"},
                    "500": {"description": "Upload or queueing failed"},
                },
            }
        },
        "/tags": {
            "get": {"tags": ["Tags"], "summary": "List tags", "responses": {"200": {"description": "List of tags"}}},
            "post": {"tags": ["Tags"], "summary": "Create tag", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "custom_prompt": {"type": "string"}, "title_prompt": {"type": "string"}, "default_language": {"type": "string"}, "default_min_speakers": {"type": "integer"}, "default_max_speakers": {"type": "integer"}}}}}}, "responses": {"201": {"description": "Tag created"}}}
        },
        "/tags/{id}": {
            "put": {"tags": ["Tags"], "summary": "Update tag", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "custom_prompt": {"type": "string"}, "title_prompt": {"type": "string"}}}}}}, "responses": {"200": {"description": "Tag updated"}}},
            "delete": {"tags": ["Tags"], "summary": "Delete tag", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Tag deleted"}}}
        },
        "/folders": {
            "get": {"tags": ["Folders"], "summary": "List folders", "responses": {"200": {"description": "Array of folders the user can access", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Folder"}}}}}}},
            "post": {"tags": ["Folders"], "summary": "Create folder", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "group_id": {"type": "integer", "nullable": True}, "custom_prompt": {"type": "string"}, "title_prompt": {"type": "string"}, "default_language": {"type": "string"}, "default_min_speakers": {"type": "integer"}, "default_max_speakers": {"type": "integer"}, "default_hotwords": {"type": "string"}, "default_initial_prompt": {"type": "string"}, "default_transcription_model": {"type": "string"}, "retention_days": {"type": "integer"}}}}}}, "responses": {"201": {"description": "Folder created"}}}
        },
        "/folders/{id}": {
            "get": {"tags": ["Folders"], "summary": "Get folder", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Folder", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Folder"}}}}}},
            "patch": {"tags": ["Folders"], "summary": "Update folder", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "custom_prompt": {"type": "string"}, "title_prompt": {"type": "string"}, "default_language": {"type": "string"}, "default_min_speakers": {"type": "integer"}, "default_max_speakers": {"type": "integer"}, "default_hotwords": {"type": "string"}, "default_initial_prompt": {"type": "string"}, "default_transcription_model": {"type": "string"}, "retention_days": {"type": "integer"}}}}}}, "responses": {"200": {"description": "Folder updated"}}},
            "delete": {"tags": ["Folders"], "summary": "Delete folder", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Folder deleted"}}}
        },
        "/transcription": {
            "get": {
                "tags": ["Transcription"],
                "summary": "Discover the active transcription connector and its accepted models",
                "description": "Returns the active connector name, a map of capability flags, the admin-curated model list, and the configured default model. Use this to drive client UIs and to know which values are valid for the `transcription_model` override on /recordings/{id}/transcribe and /recordings/upload.",
                "responses": {
                    "200": {
                        "description": "Connector info",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "connector": {"type": "string", "nullable": True, "description": "Active connector name (e.g. asr_endpoint, openai_transcribe, mistral, vibevoice)"},
                                        "capabilities": {
                                            "type": "object",
                                            "description": "Boolean flags indicating which optional fields the connector accepts",
                                            "properties": {
                                                "diarization": {"type": "boolean"},
                                                "speaker_count_control": {"type": "boolean"},
                                                "hotwords": {"type": "boolean"},
                                                "initial_prompt": {"type": "boolean"},
                                                "timestamps": {"type": "boolean"},
                                                "language_detection": {"type": "boolean"},
                                                "chunking": {"type": "boolean"}
                                            }
                                        },
                                        "models": {
                                            "type": "array",
                                            "description": "Models the admin has marked visible to users (or TRANSCRIPTION_MODELS_AVAILABLE entries when no DB list is set)",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "value": {"type": "string"},
                                                    "label": {"type": "string"}
                                                }
                                            }
                                        },
                                        "default_model": {"type": "string", "nullable": True}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "/speakers": {
            "get": {"tags": ["Speakers"], "summary": "List speakers", "responses": {"200": {"description": "List of speakers"}}},
            "post": {"tags": ["Speakers"], "summary": "Create speaker", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}}}, "responses": {"201": {"description": "Speaker created"}}}
        },
        "/speakers/{id}": {
            "put": {"tags": ["Speakers"], "summary": "Update speaker", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}}}, "responses": {"200": {"description": "Speaker updated"}}},
            "delete": {"tags": ["Speakers"], "summary": "Delete speaker", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Speaker deleted"}}}
        },
        "/settings/auto-summarization": {
            "put": {
                "tags": ["Settings"],
                "summary": "Toggle auto-summarization",
                "description": "Enable or disable auto-summarization for the current user.",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["enabled"], "properties": {"enabled": {"type": "boolean"}}}}}},
                "responses": {"200": {"description": "Setting updated"}}
            }
        }
    },
    "tags": [
        {"name": "Stats", "description": "System statistics for dashboards"},
        {"name": "Recordings", "description": "Recording CRUD operations"},
        {"name": "Processing", "description": "Transcription and summarization"},
        {"name": "Chat", "description": "Chat with recordings"},
        {"name": "Events", "description": "Calendar events"},
        {"name": "Audio", "description": "Audio file operations"},
        {"name": "Tags", "description": "Tag management"},
        {"name": "Folders", "description": "Folder management"},
        {"name": "Transcription", "description": "Transcription connector and model discovery"},
        {"name": "Speakers", "description": "Speaker management"},
        {"name": "Batch", "description": "Batch operations"},
        {"name": "Settings", "description": "User settings"},
        {"name": "Tokens", "description": "Token introspection and instance capabilities"}
    ]
}


def _openapi_with_scopes():
    """OPENAPI_SPEC with x-required-scopes on every operation, read from the
    routes' require_scope marks so the document cannot drift from the code."""
    import copy
    import re
    from flask import current_app
    spec = copy.deepcopy(OPENAPI_SPEC)
    norm = lambda path: re.sub(r'<[^>]+>|\{[^}]+\}', '{}', path)
    by_route = {}
    for rule in current_app.url_map.iter_rules():
        if not rule.rule.startswith('/api/v1/'):
            continue
        view = current_app.view_functions.get(rule.endpoint)
        scopes = getattr(view, '_required_scopes', None)
        if scopes is None:
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            by_route[(norm(rule.rule[len('/api/v1'):]), method.lower())] = sorted(scopes)
    for path, operations in spec.get('paths', {}).items():
        for method, operation in operations.items():
            scopes = by_route.get((norm(path), method))
            if scopes is not None and isinstance(operation, dict):
                operation['x-required-scopes'] = scopes
    return spec


@api_v1_bp.route('/openapi.json', methods=['GET'])
@require_scope('read')
def get_openapi_spec():
    """Return OpenAPI specification."""
    return jsonify(_openapi_with_scopes())


@api_v1_bp.route('/docs', methods=['GET'])
@require_scope('read')
def get_docs():
    """Serve Swagger UI documentation.

    Assets are served from Speakr's own vendored copy (static/vendor) rather than
    a public CDN so the page works under the default Content-Security-Policy
    (script-src/style-src 'self') without any per-route CSP relaxation.
    """
    from flask import Response, url_for
    css_url = url_for('static', filename='vendor/css/swagger-ui.css')
    bundle_url = url_for('static', filename='vendor/js/swagger-ui-bundle.js')
    html = f'''<!DOCTYPE html>
<html>
<head>
    <title>Speakr API v1 Documentation</title>
    <link rel="stylesheet" href="{css_url}" />
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="{bundle_url}"></script>
    <script>
        SwaggerUIBundle({{
            url: "/api/v1/openapi.json",
            dom_id: '#swagger-ui',
            presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
            layout: "BaseLayout",
            persistAuthorization: true
        }});
    </script>
</body>
</html>'''
    return Response(html, mimetype='text/html')


# =============================================================================
# Stats Endpoint (Homepage Widget Compatible)
# =============================================================================

@api_v1_bp.route('/stats', methods=['GET'])
@require_scope('read')
@login_required
def get_stats():
    """
    Get system/user statistics for dashboard widgets.

    Query params:
        scope: 'user' (default) or 'all' (admin only)

    Returns JSON compatible with gethomepage.dev custom API widget:
    {
        "recordings": {"total": N, "completed": N, "processing": N, "pending": N, "failed": N},
        "storage": {"used_bytes": N, "used_human": "X.X GB"},
        "queue": {"jobs_queued": N, "jobs_processing": N},
        "tokens": {"used_this_month": N, "budget": N, "percentage": N},
        "transcription": {"used_this_month_seconds": N, "used_this_month_minutes": N, "budget_seconds": N, "budget_minutes": N, "percentage": N, "estimated_cost": N},
        "activity": {"recordings_today": N, "last_transcription": "ISO datetime"}
    }
    """
    scope = request.args.get('scope', 'user')

    # Admin-only for global stats
    if scope == 'all' and not current_user.is_admin:
        return jsonify({'error': 'Admin access required for global stats'}), 403

    # Build query filters based on scope
    if scope == 'user':
        recording_filter = Recording.user_id == current_user.id
        job_filter = ProcessingJob.user_id == current_user.id
        user_id_for_tokens = current_user.id
    else:
        recording_filter = True  # No filter = all recordings
        job_filter = True
        user_id_for_tokens = None  # Will aggregate all users

    # Recording counts by status
    total = Recording.query.filter(recording_filter).count()
    completed = Recording.query.filter(recording_filter, Recording.status == 'COMPLETED').count()
    processing = Recording.query.filter(
        recording_filter,
        Recording.status.in_(['PROCESSING', 'SUMMARIZING'])
    ).count()
    pending = Recording.query.filter(recording_filter, Recording.status == 'PENDING').count()
    failed = Recording.query.filter(recording_filter, Recording.status == 'FAILED').count()

    # Storage calculation. Exclude recordings whose audio was removed by
    # audio-only retention (audio_deleted_at set) — the file is gone but
    # file_size is still recorded, so summing it overcounts actual storage.
    storage_query = db.session.query(func.sum(Recording.file_size)).filter(
        recording_filter, Recording.audio_deleted_at.is_(None))
    storage_bytes = storage_query.scalar() or 0

    # Queue status
    jobs_queued = ProcessingJob.query.filter(
        job_filter,
        ProcessingJob.status == 'queued'
    ).count()
    jobs_processing = ProcessingJob.query.filter(
        job_filter,
        ProcessingJob.status == 'processing'
    ).count()

    # Token usage
    tokens_data = {}
    if user_id_for_tokens:
        # Single user stats
        monthly_usage = token_tracker.get_monthly_usage(user_id_for_tokens)
        user = db.session.get(User, user_id_for_tokens)
        budget = user.monthly_token_budget if user else None

        tokens_data = {
            'used_this_month': monthly_usage,
            'budget': budget,
            'percentage': round((monthly_usage / budget * 100), 1) if budget else None
        }
    else:
        # Aggregate all users (admin scope)
        current_year = date.today().year
        current_month = date.today().month
        total_usage = db.session.query(func.sum(TokenUsage.total_tokens)).filter(
            extract('year', TokenUsage.date) == current_year,
            extract('month', TokenUsage.date) == current_month
        ).scalar() or 0

        tokens_data = {
            'used_this_month': total_usage,
            'budget': None,
            'percentage': None
        }

    # Transcription usage
    transcription_data = {}
    if user_id_for_tokens:
        # Single user stats
        monthly_transcription = transcription_tracker.get_monthly_usage(user_id_for_tokens)
        monthly_cost = transcription_tracker.get_monthly_cost(user_id_for_tokens)
        user = db.session.get(User, user_id_for_tokens)
        transcription_budget = user.monthly_transcription_budget if user else None

        transcription_data = {
            'used_this_month_seconds': monthly_transcription,
            'used_this_month_minutes': monthly_transcription // 60,
            'budget_seconds': transcription_budget,
            'budget_minutes': transcription_budget // 60 if transcription_budget else None,
            'percentage': round((monthly_transcription / transcription_budget * 100), 1) if transcription_budget else None,
            'estimated_cost': round(monthly_cost, 4)
        }
    else:
        # Aggregate all users (admin scope)
        current_year = date.today().year
        current_month = date.today().month
        total_seconds = db.session.query(func.sum(TranscriptionUsage.audio_duration_seconds)).filter(
            extract('year', TranscriptionUsage.date) == current_year,
            extract('month', TranscriptionUsage.date) == current_month
        ).scalar() or 0
        total_cost = db.session.query(func.sum(TranscriptionUsage.estimated_cost)).filter(
            extract('year', TranscriptionUsage.date) == current_year,
            extract('month', TranscriptionUsage.date) == current_month
        ).scalar() or 0

        transcription_data = {
            'used_this_month_seconds': total_seconds,
            'used_this_month_minutes': total_seconds // 60,
            'budget_seconds': None,
            'budget_minutes': None,
            'percentage': None,
            'estimated_cost': round(total_cost, 4)
        }

    # Recent activity
    today_start = datetime.combine(date.today(), datetime.min.time())
    recordings_today = Recording.query.filter(
        recording_filter,
        Recording.created_at >= today_start
    ).count()

    # Last completed transcription
    last_completed = Recording.query.filter(
        recording_filter,
        Recording.status == 'COMPLETED',
        Recording.completed_at.isnot(None)
    ).order_by(Recording.completed_at.desc()).first()

    last_transcription = last_completed.completed_at.isoformat() if last_completed and last_completed.completed_at else None

    # Build response
    response = {
        'recordings': {
            'total': total,
            'completed': completed,
            'processing': processing,
            'pending': pending,
            'failed': failed
        },
        'storage': {
            'used_bytes': storage_bytes,
            'used_human': format_bytes(storage_bytes)
        },
        'queue': {
            'jobs_queued': jobs_queued,
            'jobs_processing': jobs_processing
        },
        'tokens': tokens_data,
        'transcription': transcription_data,
        'activity': {
            'recordings_today': recordings_today,
            'last_transcription': last_transcription
        }
    }

    # Add user counts for admin scope
    if scope == 'all' and current_user.is_admin:
        total_users = User.query.count()
        # Active = users with recordings in last 30 days
        cutoff = datetime.utcnow() - timedelta(days=30)
        active_users = db.session.query(func.count(func.distinct(Recording.user_id))).filter(
            Recording.created_at >= cutoff
        ).scalar() or 0

        response['users'] = {
            'total': total_users,
            'active': active_users
        }

    return jsonify(response)


# =============================================================================
# Current User
# =============================================================================

@api_v1_bp.route('/tokens/current', methods=['GET'])
@require_scope()
@login_required
def get_current_token():
    """The API token of this request: id, name, scopes and times (mailr spec G1)."""
    from flask import g
    from src.utils.token_auth import current_api_token
    token = current_api_token()
    if token is None:
        return jsonify({'error': 'No API token in this request', 'code': 'not_found'}), 404

    def _z(dt):
        return dt.isoformat(timespec='microseconds') + 'Z' if dt else None
    return jsonify({
        'id': token.id,
        'name': token.name,
        'scopes': token.scope_list,
        'created_at': _z(token.created_at),
        'expires_at': _z(token.expires_at),
        'last_used_at': _z(token.last_used_at),
        'via': 'query' if g.get('api_token_via_query') else 'header',
    })


# Features this server supports (mailr spec G1). A key appears when the feature
# ships; a missing key means false.
CAPABILITY_FEATURES = {
    'token_scopes': True,
    'changes_feed': True,
    'etags': True,
    'webhook_signature_v2': True,
    'external_refs': True,
    'upload_idempotency': True,
}
# search: {'keyword': True, 'semantic': <Inquire on>} is filled in get_capabilities.


@api_v1_bp.route('/capabilities', methods=['GET'])
@require_scope()
@login_required
def get_capabilities():
    """Feature discovery for API clients; read this instead of the version string."""
    from src.config.version import get_version
    features = dict(CAPABILITY_FEATURES)
    features['internal_sharing'] = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
    features['public_sharing'] = os.environ.get('ENABLE_PUBLIC_SHARING', 'true').lower() == 'true'
    features['can_share_publicly'] = bool(features['public_sharing'] and getattr(current_user, 'can_share_publicly', True))
    from src.services.search_v1 import semantic_available
    features['search'] = {'keyword': True, 'semantic': semantic_available()}
    features['inquire'] = {
        'enabled': os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true',
        'agent': os.environ.get('ENABLE_INQUIRE_AGENT', 'false').lower() == 'true',
    }
    return jsonify({
        'speakr_version': get_version(),
        'api_version': '1.1',
        'features': features,
        # True only when the administrator states that the text, chat and
        # embedding endpoints all run on machines they control.
        'models_local': os.environ.get('MODELS_ARE_LOCAL', 'false').lower() == 'true',
    })


@api_v1_bp.route('/users/me', methods=['GET'])
@require_scope('read')
@login_required
def get_current_user():
    """
    Return the authenticated user's profile.

    Companion apps and automation flows need a way to display the current
    user's identity (issue #281). This endpoint returns a stable subset of the
    profile and preferences fields plus the user's group memberships.
    """
    memberships = []
    for membership in (current_user.group_memberships or []):
        if membership.group is None:
            continue
        memberships.append({
            'group_id': membership.group_id,
            'group_name': membership.group.name,
            'role': membership.role,
            'joined_at': membership.joined_at.isoformat() if membership.joined_at else None,
        })

    return jsonify({
        'id': current_user.id,
        'username': current_user.username,
        'email': current_user.email,
        'name': current_user.name,
        'job_title': current_user.job_title,
        'company': current_user.company,
        'is_admin': bool(current_user.is_admin),
        'email_verified': bool(current_user.email_verified),
        'sso_provider': current_user.sso_provider,
        'can_share_publicly': bool(current_user.can_share_publicly),
        'preferences': {
            'ui_language': current_user.ui_language,
            'transcription_language': current_user.transcription_language,
            'output_language': current_user.output_language,
            'extract_events': bool(current_user.extract_events),
            'auto_speaker_labelling': bool(current_user.auto_speaker_labelling),
            'auto_speaker_labelling_threshold': current_user.auto_speaker_labelling_threshold,
            # None means enabled, as in processing (#412 audit S10).
            'auto_summarization': current_user.auto_summarization is not False,
            'show_timestamps_simple_view': bool(current_user.show_timestamps_simple_view),
            'editor_autosave': bool(current_user.editor_autosave),
            'notify_email_on_completion': bool(current_user.notify_email_on_completion),
            # The per-user diarize preference has had no control since Aug 2025
            # and was never read; report what transcription actually does.
            'diarize': _effective_diarize(),
        },
        'group_memberships': memberships,
    })


# =============================================================================
# Recordings List with Enhanced Filtering
# =============================================================================

def _updated_at_z(recording):
    from src.services.recording_changes import effective_updated_at, iso_z
    return iso_z(effective_updated_at(recording))


def _recording_list_item(r, refs=None, shared=None):
    """One recording as GET /recordings lists it; the changes feed uses the same shape.

    refs: this caller's external references of the recording, when the caller
    loaded them for a whole page (refs_map); otherwise read here.
    """
    if refs is None:
        from src.services.external_refs import refs_for
        refs = [x.to_dict() for x in refs_for(r.id, current_user.id)]
    from src.services.recording_changes import effective_updated_at, iso_z
    return {
        'id': r.id,
        'title': r.title,
        'status': r.status,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'completed_at': r.completed_at.isoformat() if r.completed_at else None,
        'meeting_date': r.meeting_date.isoformat() if r.meeting_date else None,
        'file_size': r.file_size,
        'original_filename': r.original_filename,
        'participants': r.participants,
        'is_inbox': r.is_inbox,
        'is_highlighted': r.is_highlighted,
        'is_archived': bool(r.is_archived),
        'audio_available': r.audio_deleted_at is None,
        'audio_duration': r.get_audio_duration(),
        'has_transcription': bool(r.transcription),
        'has_summary': bool(r.summary),
        'processing_time_seconds': r.processing_time_seconds,
        'transcription_duration_seconds': r.transcription_duration_seconds,
        'summarization_duration_seconds': r.summarization_duration_seconds,
        'folder_id': r.folder_id,
        'folder': {'id': r.folder.id, 'name': r.folder.name} if r.folder else None,
        'deletion_exempt': r.deletion_exempt,
        'error_message': r.error_message if r.status == 'FAILED' else None,
        'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in r.tags],
        'keep_audio_only': r.keep_audio_only,
        'updated_at': iso_z(effective_updated_at(r)),
        'external_refs': refs,
        'is_shared': False,
        'owner': None,
        'share': None,
    } if shared is None else dict(_recording_list_item(r, refs), **shared)


@api_v1_bp.route('/recordings', methods=['GET'])
@require_scope('read')
@login_required
def list_recordings():
    """
    List recordings with filtering and pagination.

    Query params:
        page: Page number (default: 1)
        per_page: Items per page (default: 25, max: 100)
        status: Filter by status (pending, processing, completed, failed, all)
        sort_by: Sort field (created_at, meeting_date, title, file_size, status)
        sort_order: asc or desc (default: desc)
        date_from: Filter from date (ISO format)
        date_to: Filter to date (ISO format)
        tag_id: Filter by tag ID
        q: Search query (title, participants)
        inbox: Filter by inbox status (true/false)
        starred: Filter by starred status (true/false)
        archived: Filter by archive status (true/false); omitted returns both
        scope: own (default), shared or all
        owner_id: Only recordings of this owner
        updated_since: Only recordings changed after this time (ISO 8601)
        date_field: What date_from and date_to filter: created_at (default)
                    or meeting_date
    """
    # Parse query parameters
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 25, type=int), 100)
    status_filter = request.args.get('status', 'all').lower()
    sort_by = request.args.get('sort_by', 'created_at')
    sort_order = request.args.get('sort_order', 'desc').lower()
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    tag_id = request.args.get('tag_id', type=int)
    folder_filter = request.args.get('folder_id', '').strip()
    search_query = request.args.get('q', '').strip()
    inbox_filter = request.args.get('inbox')
    starred_filter = request.args.get('starred')
    archived_filter = request.args.get('archived')
    updated_since = request.args.get('updated_since')
    from src.services.recording_scope import SCOPES, join_personal_state, personal_flag, scope_condition
    scope = request.args.get('scope', 'own')
    if scope not in SCOPES:
        return jsonify({'error': 'scope must be own, shared or all', 'code': 'invalid_parameter'}), 400
    owner_id = request.args.get('owner_id', type=int)
    date_field = request.args.get('date_field', 'created_at')
    if date_field not in ('created_at', 'meeting_date'):
        return jsonify({'error': "date_field must be created_at or meeting_date",
                        'code': 'invalid_parameter'}), 400

    # Base query - user's recordings.
    # Eager-load folder and tag-association+Tag so the list builder
    # doesn't trigger one lazy SELECT per row for folder access (25
    # rows -> 25 extra queries) and one per tag association (another
    # ~50 queries on a typical page). This collapses ~75 queries into
    # the single paginated SELECT plus two joined-table joins.
    from sqlalchemy.orm import joinedload
    from src.models.organization import RecordingTag
    query = (
        Recording.query
        .options(
            joinedload(Recording.folder),
            joinedload(Recording.tag_associations).joinedload(RecordingTag.tag),
        )
        .filter(scope_condition(current_user.id, scope))
    )
    if scope != 'own':
        query = join_personal_state(query, current_user.id)
    if owner_id is not None:
        query = query.filter(Recording.user_id == owner_id)

    # Status filter
    if status_filter == 'pending':
        query = query.filter(Recording.status == 'PENDING')
    elif status_filter == 'processing':
        query = query.filter(Recording.status.in_(['PROCESSING', 'SUMMARIZING']))
    elif status_filter == 'completed':
        query = query.filter(Recording.status == 'COMPLETED')
    elif status_filter == 'failed':
        query = query.filter(Recording.status == 'FAILED')
    # 'all' = no status filter

    # Date filters
    # Aware values are converted to the naive-UTC storage convention; a bare
    # date_to includes that whole day (#412 B8).
    from src.utils.dates import to_utc_naive
    date_column = Recording.meeting_date if date_field == 'meeting_date' else Recording.created_at
    if date_from:
        try:
            from_date = to_utc_naive(datetime.fromisoformat(date_from.replace('Z', '+00:00')))
            query = query.filter(date_column >= from_date)
        except ValueError:
            pass

    if date_to:
        try:
            to_date = to_utc_naive(datetime.fromisoformat(date_to.replace('Z', '+00:00')))
            if len(date_to.strip()) == 10:
                query = query.filter(date_column < to_date + timedelta(days=1))
            else:
                query = query.filter(date_column <= to_date)
        except ValueError:
            pass

    # External references (mailr spec G8): only the caller's own.
    external_system = request.args.get('external_system')
    external_ref = request.args.get('external_ref')
    if external_system or external_ref:
        if not (external_system and external_ref):
            return jsonify({'error': 'external_system and external_ref go together',
                            'code': 'invalid_parameter'}), 400
        from src.models import RecordingExternalRef
        ref_query = db.session.query(RecordingExternalRef.recording_id).filter(
            RecordingExternalRef.user_id == current_user.id,
            RecordingExternalRef.system == external_system,
            RecordingExternalRef.ref == external_ref)
        if request.args.get('external_kind'):
            ref_query = ref_query.filter(RecordingExternalRef.kind == request.args['external_kind'])
        query = query.filter(Recording.id.in_(ref_query))

    changed_column = db.func.coalesce(Recording.updated_at, Recording.created_at)
    if updated_since:
        try:
            since = to_utc_naive(datetime.fromisoformat(updated_since.replace('Z', '+00:00')))
        except ValueError:
            return jsonify({'error': 'updated_since must be an ISO 8601 date-time',
                            'code': 'invalid_parameter'}), 400
        query = query.filter(changed_column > since)

    # Tag filter
    if tag_id:
        query = query.join(RecordingTag).filter(RecordingTag.tag_id == tag_id)

    # Folder filter. Accepts an integer folder id, or the literal "none" to
    # return recordings that are not in any folder. Anything else is ignored.
    if folder_filter:
        if folder_filter.lower() == 'none':
            query = query.filter(Recording.folder_id.is_(None))
        else:
            try:
                folder_id_int = int(folder_filter)
                query = query.filter(Recording.folder_id == folder_id_int)
            except ValueError:
                pass

    # Search filter
    if search_query:
        search_pattern = f'%{search_query}%'
        query = query.filter(
            or_(
                Recording.title.ilike(search_pattern),
                Recording.participants.ilike(search_pattern)
            )
        )

    # Inbox and starred filters: the caller's own value (a recipient's is on
    # SharedRecordingState).
    if inbox_filter is not None:
        is_inbox = inbox_filter.lower() == 'true'
        query = query.filter((personal_flag(current_user.id, 'is_inbox', True) if scope != 'own'
                              else Recording.is_inbox) == is_inbox)

    if starred_filter is not None:
        is_starred = starred_filter.lower() == 'true'
        query = query.filter((personal_flag(current_user.id, 'is_highlighted', False) if scope != 'own'
                              else Recording.is_highlighted) == is_starred)

    # Archive filter (#394). The API keeps returning archived recordings by
    # default so existing integrations see no change.
    if archived_filter is not None:
        is_archived = archived_filter.lower() == 'true'
        query = query.filter((personal_flag(current_user.id, 'is_archived', False) if scope != 'own'
                              else db.func.coalesce(Recording.is_archived, False)) == is_archived)

    # Sorting
    sort_columns = {
        'created_at': Recording.created_at,
        'meeting_date': Recording.meeting_date,
        'title': Recording.title,
        'file_size': Recording.file_size,
        'status': Recording.status,
        'updated_at': changed_column,
    }
    sort_column = sort_columns.get(sort_by, Recording.created_at)

    if sort_order == 'asc':
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())

    # Pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Build response
    from src.services.external_refs import refs_map
    from src.services.recording_scope import share_details
    page_refs = refs_map([r.id for r in pagination.items], current_user.id)
    page_shared = share_details(pagination.items, current_user)
    recordings = [_recording_list_item(r, page_refs.get(r.id, []), page_shared.get(r.id))
                  for r in pagination.items]

    return jsonify({
        'recordings': recordings,
        'pagination': {
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'total_pages': pagination.pages,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev
        }
    })


# =============================================================================
# Recording Detail
# =============================================================================

def _conditional(prefix):
    """Weak ETag and If-None-Match for a GET on one recording (mailr spec G11).

    The tag holds the recording id, its updated_at and a hash of the body, so
    it also changes when something outside the recording changes the answer
    (a tag or folder name, the viewer's transcript template, voice
    suggestions). It runs after the view, so the view's access check comes
    first and a caller without access gets 403 or 404, never 304.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(recording_id, *args, **kwargs):
            import hashlib
            from flask import make_response
            from src.services.recording_changes import etag_matches, updated_at_us
            response = make_response(view(recording_id, *args, **kwargs))
            if response.status_code != 200 or response.direct_passthrough:
                return response
            recording = db.session.get(Recording, recording_id)
            digest = hashlib.sha1(response.get_data()).hexdigest()[:16]
            etag = f'W/"{prefix}{recording_id}-{updated_at_us(recording) if recording else 0}-{digest}"'
            if etag_matches(request, etag):
                response = make_response('', 304)
            response.headers['ETag'] = etag
            response.headers['Cache-Control'] = 'private, no-cache'
            return response
        return wrapper
    return decorator


@api_v1_bp.route('/search', methods=['GET'])
@require_scope('read')
@rate_limit(os.environ.get('API_SEARCH_RATE_LIMIT', '30 per minute'), key_func=token_rate_key)
@login_required
def search_recordings_v1():
    """Search titles, participants, notes, summaries and transcripts (mailr spec G3).

    Query params: q (2 to 500 characters), mode (keyword | semantic | auto),
    fields, scope (own), recording_ids, tag_id, folder_id, speaker,
    date_from, date_to, date_field (meeting_date | created_at), limit (1-50),
    page (keyword mode).
    """
    from src.services import search_v1
    args = request.args
    q = (args.get('q') or '').strip()
    if not 2 <= len(q) <= 500:
        return jsonify({'error': 'q must be 2 to 500 characters', 'code': 'invalid_parameter'}), 400
    mode = args.get('mode', 'keyword')
    if mode not in ('keyword', 'semantic', 'auto'):
        return jsonify({'error': 'mode must be keyword, semantic or auto', 'code': 'invalid_parameter'}), 400
    scope = args.get('scope', 'own')
    if scope not in ('own', 'shared', 'all'):
        return jsonify({'error': 'scope must be own, shared or all', 'code': 'invalid_parameter'}), 400
    fields = tuple(f.strip() for f in args.get('fields', ','.join(search_v1.FIELDS)).split(',') if f.strip())
    unknown = [f for f in fields if f not in search_v1.FIELDS]
    if unknown or not fields:
        return jsonify({'error': f'Unknown field(s): {", ".join(unknown) or "none given"}',
                        'code': 'invalid_parameter'}), 400
    date_field = args.get('date_field', 'meeting_date')
    if date_field not in ('meeting_date', 'created_at'):
        return jsonify({'error': 'date_field must be meeting_date or created_at', 'code': 'invalid_parameter'}), 400
    limit = args.get('limit', 20, type=int)
    page = args.get('page', 1, type=int)
    if limit is None or not 1 <= limit <= 50 or page is None or page < 1:
        return jsonify({'error': 'limit must be 1 to 50 and page at least 1', 'code': 'invalid_parameter'}), 400
    try:
        recording_ids = [int(x) for x in args.get('recording_ids', '').split(',') if x.strip()] or None
    except ValueError:
        return jsonify({'error': 'recording_ids must be a comma list of ids', 'code': 'invalid_parameter'}), 400
    folder = args.get('folder_id', '').strip()
    folder_id = None
    if folder:
        if folder.lower() == 'none':
            folder_id = 'none'
        elif folder.isdigit():
            folder_id = int(folder)
        else:
            return jsonify({'error': 'folder_id must be an id or none', 'code': 'invalid_parameter'}), 400
    filters = dict(recording_ids=recording_ids, tag_id=args.get('tag_id', type=int), folder_id=folder_id,
                   date_from=args.get('date_from') or None, date_to=args.get('date_to') or None,
                   date_field=date_field, scope=scope)
    speaker = (args.get('speaker') or '').strip() or None

    use_semantic = mode == 'semantic' or (mode == 'auto' and search_v1.semantic_available())
    if mode == 'semantic' and not search_v1.semantic_available():
        return jsonify({'error': 'Semantic search is not available on this instance',
                        'code': 'semantic_unavailable'}), 409
    try:
        if use_semantic:
            # The dates are checked the same way in both modes.
            search_v1.candidate_query(current_user, ['x'], ('title',), **filters)
            results = search_v1.semantic_search(current_user, q, limit=limit, speaker=speaker, **filters)
            has_more = False
        else:
            results, has_more = search_v1.keyword_search(current_user, q, fields=fields, speaker=speaker,
                                                         limit=limit, page=page, **filters)
    except search_v1.SearchError as e:
        return jsonify({'error': str(e), 'code': e.code}), e.status
    return jsonify({'query': q, 'mode_used': 'semantic' if use_semantic else 'keyword',
                    'results': results, 'page': page if not use_semantic else 1, 'has_more': has_more})


def _ref_recording(recording_id, require_edit):
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return None, (jsonify({'error': 'Recording not found', 'code': 'not_found'}), 404)
    if not has_recording_access(recording, current_user, require_edit=require_edit):
        return None, (jsonify({'error': 'Permission denied'}), 403)
    return recording, None


@api_v1_bp.route('/recordings/<int:recording_id>/external-refs', methods=['GET'])
@require_scope('read')
@login_required
def list_external_refs(recording_id):
    """Your external references on a recording (mailr spec G8)."""
    from src.services.external_refs import refs_for
    recording, err = _ref_recording(recording_id, require_edit=False)
    if err:
        return err
    return jsonify({'external_refs': [r.to_dict() for r in refs_for(recording.id, current_user.id)]})


@api_v1_bp.route('/recordings/<int:recording_id>/external-refs', methods=['POST'])
@require_scope('write')
@login_required
def add_external_ref(recording_id):
    """Add one reference; an existing (system, kind, ref) answers 200 with it."""
    from src.services import external_refs as xr
    recording, err = _ref_recording(recording_id, require_edit=True)
    if err:
        return err
    try:
        row, created = xr.add(recording, current_user.id, xr.validate(request.get_json(silent=True)))
    except xr.RefError as e:
        return jsonify({'error': str(e), 'code': e.code}), e.status
    db.session.commit()
    return jsonify(row.to_dict()), 201 if created else 200


@api_v1_bp.route('/recordings/<int:recording_id>/external-refs', methods=['PUT'])
@require_scope('write')
@login_required
def replace_external_refs(recording_id):
    """Replace your references of one system (?system=) with the given list."""
    from src.services import external_refs as xr
    recording, err = _ref_recording(recording_id, require_edit=True)
    if err:
        return err
    system = request.args.get('system', '')
    body = request.get_json(silent=True) or {}
    try:
        if not xr._SLUG.match(system):
            raise xr.RefError('?system= is required')
        xr.replace_system(recording, current_user.id, system, xr.validate_list(body.get('external_refs')))
    except xr.RefError as e:
        db.session.rollback()
        return jsonify({'error': str(e), 'code': e.code}), e.status
    db.session.commit()
    return jsonify({'external_refs': [r.to_dict() for r in xr.refs_for(recording.id, current_user.id)]})


@api_v1_bp.route('/recordings/<int:recording_id>/external-refs/<int:ref_id>', methods=['DELETE'])
@require_scope('write')
@login_required
def delete_external_ref(recording_id, ref_id):
    from src.models import RecordingExternalRef
    recording, err = _ref_recording(recording_id, require_edit=True)
    if err:
        return err
    row = db.session.get(RecordingExternalRef, ref_id)
    if row is None or row.recording_id != recording.id or row.user_id != current_user.id:
        return jsonify({'error': 'Reference not found', 'code': 'not_found'}), 404
    db.session.delete(row)
    db.session.commit()
    return '', 204


@api_v1_bp.route('/recordings/changes', methods=['GET'])
@require_scope('read')
@login_required
def list_recording_changes():
    """Changes to the user's recordings since a cursor (mailr spec G2).

    Query params:
        cursor: from a previous answer; absent starts a full pass (every live
                recording, no tombstones)
        limit: 1 to 500 (default 100)
        scope: own (default; shared and all arrive with G4)
    """
    from src.services.recording_changes import (CursorError, cursor_expired, decode_cursor,
                                                iso_z, read_changes)
    cursor = request.args.get('cursor') or None
    limit = request.args.get('limit', 100, type=int)
    if limit is None or not 1 <= limit <= 500:
        return jsonify({'error': 'limit must be between 1 and 500', 'code': 'invalid_parameter'}), 400
    scope = request.args.get('scope', 'own')
    if scope not in ('own', 'shared', 'all'):
        return jsonify({'error': 'scope must be own, shared or all', 'code': 'invalid_parameter'}), 400
    if cursor:
        try:
            changed_at, _, _, full_since = decode_cursor(cursor)
        except CursorError:
            return jsonify({'error': 'The cursor could not be read', 'code': 'invalid_parameter'}), 400
        if cursor_expired(current_user.id, changed_at, full_since):
            return jsonify({'error': 'The cursor is too old; start again without a cursor',
                            'code': 'cursor_expired'}), 410
    items, next_cursor, has_more = read_changes(current_user.id, cursor=cursor, limit=limit, scope=scope)
    from src.services.external_refs import refs_map
    from src.services.recording_scope import share_details
    upserts = [obj for kind, obj in items if kind == 'upsert']
    page_refs = refs_map([obj.id for obj in upserts], current_user.id)
    page_shared = share_details(upserts, current_user)
    changes = []
    for kind, obj in items:
        if kind == 'upsert':
            changes.append({'type': 'upsert', 'recording': _recording_list_item(
                obj, page_refs.get(obj.id, []), page_shared.get(obj.id))})
        else:
            changes.append({'type': 'delete', 'id': obj.recording_id,
                            'deleted_at': iso_z(obj.deleted_at), 'reason': obj.reason})
    return jsonify({'changes': changes, 'next_cursor': next_cursor, 'has_more': has_more})


@api_v1_bp.route('/recordings/<int:recording_id>', methods=['GET'])
@require_scope('read')
@login_required
@_conditional('r')
def get_recording(recording_id):
    """
    Get full recording details.

    Query params:
        include: Comma-separated fields to include (transcription, summary, notes)
                 Default: all fields
        format: 'full' (default) or 'minimal' (excludes large text fields)
    """
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    from src.services.recording_changes import effective_updated_at, iso_z
    include = request.args.get('include', 'transcription,summary,notes')
    include_fields = [f.strip() for f in include.split(',')]
    format_type = request.args.get('format', 'full')

    response = {
        'id': recording.id,
        'title': recording.title,
        'status': recording.status,
        'participants': recording.participants,
        'created_at': recording.created_at.isoformat() if recording.created_at else None,
        'meeting_date': recording.meeting_date.isoformat() if recording.meeting_date else None,
        'completed_at': recording.completed_at.isoformat() if recording.completed_at else None,
        'file_size': recording.file_size,
        'original_filename': recording.original_filename,
        'mime_type': recording.mime_type,
        'is_inbox': recording.is_inbox,
        'is_highlighted': recording.is_highlighted,
        'is_archived': get_user_archived(recording, current_user),
        'audio_available': recording.audio_deleted_at is None,
        'audio_duration': recording.get_audio_duration(),
        'processing_time_seconds': recording.processing_time_seconds,
        'transcription_duration_seconds': recording.transcription_duration_seconds,
        'summarization_duration_seconds': recording.summarization_duration_seconds,
        'folder_id': recording.folder_id,
        'folder': {'id': recording.folder.id, 'name': recording.folder.name} if recording.folder else None,
        'deletion_exempt': recording.deletion_exempt,
        'events': [e.api_dict() for e in recording.events] if hasattr(recording, 'events') else [],
        'error_message': recording.error_message if recording.status == 'FAILED' else None,
        'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in recording.tags],
        'duplicate_info': recording.get_duplicate_info(),
        'keep_audio_only': recording.keep_audio_only,
        'updated_at': iso_z(effective_updated_at(recording)),
        'external_refs': [x.to_dict() for x in __import__('src.services.external_refs', fromlist=['refs_for'])
                          .refs_for(recording.id, current_user.id)],
    }

    # Include large text fields based on params
    if format_type != 'minimal':
        if 'transcription' in include_fields:
            # Format transcription using user's default template
            response['transcription'] = format_transcription_with_template(
                recording.transcription, current_user
            ) if recording.transcription else None
        if 'summary' in include_fields:
            response['summary'] = recording.summary
        if 'notes' in include_fields:
            response['notes'] = recording.get_user_notes(current_user)

    return jsonify(response)


# =============================================================================
# Recording Transcript/Summary/Notes Individual Endpoints
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/transcript', methods=['GET'])
@require_scope('read')
@login_required
@_conditional('t')
def get_transcript(recording_id):
    """
    Get transcript in various formats.

    Query params:
        format: json (default), text, srt, vtt
    """
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    if not recording.transcription:
        return jsonify({'error': 'No transcription available'}), 404

    format_type = request.args.get('format', 'json').lower()

    if format_type == 'json':
        # One documented shape whatever key set the transcript was stored
        # with (mailr spec G12); optional window: start, end, max_segments.
        from src.services.transcript_segments import canonical_segments, window
        segments = canonical_segments(recording)
        if segments is None:
            return jsonify({'format': 'json', 'kind': 'plain', 'segments': [], 'raw': recording.transcription})
        try:
            start = request.args.get('start', type=float)
            end = request.args.get('end', type=float)
            max_segments = request.args.get('max_segments', type=int)
        except ValueError:
            start = end = max_segments = None
        if max_segments is not None and max_segments < 1:
            return jsonify({'error': 'max_segments must be at least 1', 'code': 'invalid_parameter'}), 400
        body = {'format': 'json', 'kind': 'segments', 'duration': recording.get_audio_duration()}
        if start is not None or end is not None or max_segments is not None:
            segments, next_start = window(segments, start, end, max_segments)
            if next_start is not None:
                body['next_start'] = next_start
        body['segments'] = segments
        return jsonify(body)

    elif format_type == 'text':
        # Use user's default template for text format
        formatted = format_transcription_with_template(recording.transcription, current_user)
        return jsonify({
            'format': 'text',
            'content': formatted
        })

    elif format_type in ['srt', 'vtt']:
        try:
            segments = json.loads(recording.transcription)
            lines = []

            if format_type == 'vtt':
                lines.append('WEBVTT')
                lines.append('')

            for i, seg in enumerate(segments, 1):
                start = seg.get('start_time', seg.get('start', 0))
                end = seg.get('end_time', seg.get('end', start + 1))
                text = seg.get('sentence') or seg.get('text', '')
                speaker = seg.get('speaker', '')

                # Format timestamps
                def fmt_time(seconds, use_comma=False):
                    h = int(seconds // 3600)
                    m = int((seconds % 3600) // 60)
                    s = int(seconds % 60)
                    ms = int((seconds - int(seconds)) * 1000)
                    sep = ',' if use_comma else '.'
                    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"

                if format_type == 'srt':
                    lines.append(str(i))
                    lines.append(f"{fmt_time(start, True)} --> {fmt_time(end, True)}")
                else:
                    lines.append(f"{fmt_time(start)} --> {fmt_time(end)}")

                if speaker:
                    lines.append(f"<v {speaker}>{text}")
                else:
                    lines.append(text)
                lines.append('')

            return jsonify({
                'format': format_type,
                'content': '\n'.join(lines)
            })
        except (json.JSONDecodeError, TypeError):
            return jsonify({'error': 'Cannot generate subtitle format from transcript'}), 400

    return jsonify({'error': f'Unknown format: {format_type}'}), 400


@api_v1_bp.route('/recordings/<int:recording_id>/summary', methods=['GET'])
@require_scope('read')
@login_required
@_conditional('s')
def get_summary(recording_id):
    """Get summary markdown."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    return jsonify({
        'summary': recording.summary,
        'has_summary': bool(recording.summary)
    })


@api_v1_bp.route('/recordings/<int:recording_id>/notes', methods=['GET'])
@require_scope('read')
@login_required
@_conditional('n')
def get_notes(recording_id):
    """Get notes markdown."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    # The owner's notes for the owner; a recipient's own personal notes otherwise.
    notes = recording.get_user_notes(current_user)
    return jsonify({
        'notes': notes,
        'has_notes': bool(notes)
    })


# =============================================================================
# Recording Update Operations
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>', methods=['PATCH'])
@require_scope('write')
@login_required
def update_recording(recording_id):
    """
    Update recording metadata, notes, or summary.

    Request body (all fields optional):
    {
        "title": "Updated Title",
        "participants": "Alice, Bob",
        "notes": "Updated notes...",
        "summary": "Updated summary...",
        "meeting_date": "2024-01-15T09:00:00Z",
        "is_inbox": false,
        "is_highlighted": true,
        "folder_id": 5            // or null to remove the recording from its folder
    }
    """
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # `keep_audio_only` is set at upload time and dictates whether the
    # processing pipeline retained the video stream. Allowing PATCH to
    # mutate it after the fact would create a misleading record (the
    # file was already processed one way or the other). Reject explicitly
    # rather than silently dropping so misuse surfaces in client logs.
    if 'keep_audio_only' in data:
        return jsonify({
            'error': 'keep_audio_only is set at upload time and cannot be changed afterwards.'
        }), 400

    # Update fields if provided
    if 'title' in data:
        if data['title'] != recording.title:
            recording.title_source = 'user'
        recording.title = data['title']
    if 'participants' in data:
        recording.participants = data['participants']
    if 'notes' in data:
        from src.services.recording_state import set_user_notes
        set_user_notes(recording, current_user, data['notes'])
    if 'summary' in data:
        recording.summary = data['summary']
    if 'meeting_date' in data:
        try:
            if data['meeting_date']:
                # meeting_date is stored as naive UTC (like created_at): convert
                # zone-aware input to UTC before storing
                recording.meeting_date = to_utc_naive(datetime.fromisoformat(data['meeting_date'].replace('Z', '+00:00')))
            else:
                recording.meeting_date = None
        except ValueError:
            return jsonify({'error': 'Invalid meeting_date format'}), 400
    if 'is_inbox' in data:
        recording.is_inbox = bool(data['is_inbox'])
    if 'is_highlighted' in data:
        recording.is_highlighted = bool(data['is_highlighted'])
    if 'is_archived' in data:
        archived = parse_archived_flag(data['is_archived'])
        if archived is None:
            return jsonify({'error': 'is_archived must be a boolean'}), 400
        # Per user, like the web app: a shared editor archives it for
        # themselves only (#394).
        set_user_archived(recording, current_user, archived, commit=False)
    if 'folder_id' in data:
        new_folder_id = data['folder_id']
        if new_folder_id is None:
            recording.folder_id = None
        else:
            from src.models.organization import Folder, GroupMembership
            target = db.session.get(Folder, new_folder_id)
            if not target:
                return jsonify({'error': f'Folder {new_folder_id} not found'}), 404
            # Personal folders: must own. Group folders: must be a member.
            if target.group_id is None:
                if target.user_id != current_user.id:
                    return jsonify({'error': 'No access to target folder'}), 403
            else:
                membership = GroupMembership.query.filter_by(
                    user_id=current_user.id, group_id=target.group_id
                ).first()
                if not membership:
                    return jsonify({'error': 'No access to target folder'}), 403
            recording.folder_id = new_folder_id

    db.session.commit()

    # recording.updated goes out from the change listener after the commit
    # (src/services/recording_changes.py), the same for every write path.

    return jsonify({
        'success': True,
        'recording': {
            'id': recording.id,
            'title': recording.title,
            'participants': recording.participants,
            'notes': recording.get_user_notes(current_user),
            'summary': recording.summary,
            'meeting_date': recording.meeting_date.isoformat() if recording.meeting_date else None,
            'is_inbox': recording.is_inbox,
            'is_highlighted': recording.is_highlighted,
            'is_archived': get_user_archived(recording, current_user),
            'folder_id': recording.folder_id,
            'updated_at': _updated_at_z(recording),
        }
    })


@api_v1_bp.route('/recordings/<int:recording_id>/notes', methods=['PUT'])
@require_scope('write')
@login_required
def replace_notes(recording_id):
    """Replace notes entirely."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    # Notes are per user, as in the web app: a recipient with view access
    # writes their personal notes, never the owner's.
    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    if not data or 'notes' not in data:
        return jsonify({'error': 'notes field required'}), 400

    from src.services.recording_state import set_user_notes
    set_user_notes(recording, current_user, data['notes'])
    db.session.commit()

    return jsonify({'success': True, 'notes': recording.get_user_notes(current_user)})


@api_v1_bp.route('/recordings/<int:recording_id>/summary', methods=['PUT'])
@require_scope('write')
@login_required
def replace_summary(recording_id):
    """Replace summary entirely."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    if not data or 'summary' not in data:
        return jsonify({'error': 'summary field required'}), 400

    recording.summary = data['summary']
    db.session.commit()

    return jsonify({'success': True, 'summary': recording.summary})


# =============================================================================
# Recording Delete
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>', methods=['DELETE'])
@require_scope('delete')
@login_required
def delete_recording(recording_id):
    """Delete a recording."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    # Check ownership (only owner can delete)
    if recording.user_id != current_user.id:
        return jsonify({'error': 'Permission denied - only owner can delete'}), 403

    # Check if deletion is allowed
    USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
    if not USERS_CAN_DELETE and not current_user.is_admin:
        return jsonify({'error': 'Deletion not allowed'}), 403

    # Same deletion as the web app: storage-aware media removal, snippets and
    # jobs first (NOT NULL recording_id), webhook, export and speaker cleanup.
    from src.services.recording_deletion import delete_recording_completely, cleanup_orphaned_speakers_quietly
    try:
        delete_recording_completely(recording)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"API v1 delete failed for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'Could not delete the recording'}), 500
    cleanup_orphaned_speakers_quietly()

    return jsonify({'success': True, 'message': 'Recording deleted'})


@api_v1_bp.route('/recordings/<int:recording_id>/delete-audio', methods=['POST'])
@require_scope('delete')
@login_required
def delete_recording_audio(recording_id):
    """Delete a recording's media file and keep its transcript, summary and notes."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404
    if recording.user_id != current_user.id:
        return jsonify({'error': 'Permission denied - only owner can delete'}), 403
    USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
    if not USERS_CAN_DELETE and not current_user.is_admin:
        return jsonify({'error': 'Deletion not allowed'}), 403
    if recording.audio_deleted_at:
        return jsonify({'error': 'The audio has already been removed'}), 409
    if recording.status not in ('COMPLETED', 'FAILED'):
        return jsonify({'error': 'Wait until processing has finished before removing the audio'}), 409

    from src.services.retention import remove_recording_audio
    remove_recording_audio(recording)
    return jsonify({'success': True, 'audio_deleted_at': recording.audio_deleted_at.isoformat()})


# =============================================================================
# Recording Status
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/status', methods=['GET'])
@require_scope('read')
@login_required
def get_recording_status(recording_id):
    """Get processing status of a recording."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    # Get queue position if pending/processing
    queue_position = None
    if recording.status in ['PENDING', 'PROCESSING', 'SUMMARIZING']:
        # Count jobs ahead of this one
        job = ProcessingJob.query.filter_by(
            recording_id=recording_id,
            status='queued'
        ).first()

        if job:
            queue_position = ProcessingJob.query.filter(
                ProcessingJob.status == 'queued',
                ProcessingJob.created_at < job.created_at
            ).count() + 1

    return jsonify({
        'id': recording.id,
        'status': recording.status,
        'queue_position': queue_position,
        'error_message': recording.error_message if recording.status == 'FAILED' else None,
        'completed_at': recording.completed_at.isoformat() if recording.completed_at else None
    })


# =============================================================================
# Title Regeneration
# =============================================================================

def _effective_diarize():
    """Whether recordings are diarized: the active connector's default (the
    processing task passes no per-user preference)."""
    try:
        from src.services.transcription import get_registry
        connector = get_registry().get_active_connector()
        return bool(connector.supports_diarization and connector.default_diarize)
    except Exception:
        return False


@api_v1_bp.route('/recordings/<int:recording_id>/regenerate_title', methods=['POST'])
@require_scope('process')
@login_required
def api_regenerate_title(recording_id):
    """Regenerate the AI title for a recording based on its existing transcription."""
    from src.api.recordings import regenerate_title
    return regenerate_title(recording_id)


# =============================================================================
# Tag Management
# =============================================================================

@api_v1_bp.route('/tags', methods=['GET'])
@require_scope('read')
@login_required
def list_tags():
    """List available tags (personal + group tags user has access to)."""
    from src.models.organization import GroupMembership

    # Get user's personal tags
    user_tags = Tag.query.filter_by(user_id=current_user.id, group_id=None).order_by(Tag.name).all()

    # Get user's team memberships
    memberships = GroupMembership.query.filter_by(user_id=current_user.id).all()
    team_roles = {m.group_id: m.role for m in memberships}
    team_ids = list(team_roles.keys())

    # Get group tags
    team_tags = []
    if team_ids:
        team_tags = Tag.query.filter(Tag.group_id.in_(team_ids)).order_by(Tag.name).all()

    result = []

    # Personal tags
    for tag in user_tags:
        result.append({
            'id': tag.id,
            'name': tag.name,
            'color': tag.color,
            'is_group_tag': False,
            'group_id': None,
            'custom_prompt': tag.custom_prompt,
            'title_prompt': tag.title_prompt,
            'default_language': tag.default_language,
            'default_min_speakers': tag.default_min_speakers,
            'default_max_speakers': tag.default_max_speakers,
            'protect_from_deletion': tag.protect_from_deletion,
            'can_edit': True
        })

    # Group tags
    for tag in team_tags:
        user_role = team_roles.get(tag.group_id, 'member')
        result.append({
            'id': tag.id,
            'name': tag.name,
            'color': tag.color,
            'is_group_tag': True,
            'group_id': tag.group_id,
            'custom_prompt': tag.custom_prompt,
            'title_prompt': tag.title_prompt,
            'default_language': tag.default_language,
            'default_min_speakers': tag.default_min_speakers,
            'default_max_speakers': tag.default_max_speakers,
            'protect_from_deletion': tag.protect_from_deletion,
            'can_edit': (user_role == 'admin')
        })

    name = (request.args.get('name') or '').strip().lower()
    if name:
        result = [t for t in result if (t['name'] or '').strip().lower() == name]
    return jsonify({'tags': result})


@api_v1_bp.route('/tags', methods=['POST'])
@require_scope('write')
@login_required
def create_tag():
    """Create a new tag."""
    from src.models.organization import GroupMembership

    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': 'Tag name is required'}), 400

    group_id = data.get('group_id')

    # If group tag, verify admin permission
    if group_id:
        membership = GroupMembership.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()
        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can create group tags'}), 403

        # Check for duplicate
        existing = Tag.query.filter_by(name=data['name'], group_id=group_id).first()
        if existing:
            return jsonify({'error': 'Tag with this name already exists for this group'}), 400
    else:
        # Check for duplicate personal tag
        existing = Tag.query.filter_by(name=data['name'], user_id=current_user.id, group_id=None).first()
        if existing:
            return jsonify({'error': 'Tag with this name already exists'}), 400

    tag = Tag(
        name=data['name'],
        user_id=current_user.id,
        group_id=group_id,
        color=data.get('color', '#3B82F6'),
        custom_prompt=data.get('custom_prompt'),
        title_prompt=data.get('title_prompt'),
        default_language=data.get('default_language'),
        default_min_speakers=data.get('default_min_speakers'),
        default_max_speakers=data.get('default_max_speakers'),
        protect_from_deletion=data.get('protect_from_deletion', False)
    )

    db.session.add(tag)
    db.session.commit()

    return jsonify({
        'id': tag.id,
        'name': tag.name,
        'color': tag.color,
        'is_group_tag': tag.group_id is not None,
        'group_id': tag.group_id,
        'custom_prompt': tag.custom_prompt,
        'title_prompt': tag.title_prompt,
        'default_language': tag.default_language,
        'default_min_speakers': tag.default_min_speakers,
        'default_max_speakers': tag.default_max_speakers,
        'protect_from_deletion': tag.protect_from_deletion
    }), 201


@api_v1_bp.route('/tags/<int:tag_id>', methods=['PUT'])
@require_scope('write')
@login_required
def update_tag(tag_id):
    """Update a tag."""
    from src.models.organization import GroupMembership

    tag = db.session.get(Tag, tag_id)
    if not tag:
        return jsonify({'error': 'Tag not found'}), 404

    # Check permission
    if tag.group_id:
        membership = GroupMembership.query.filter_by(
            group_id=tag.group_id,
            user_id=current_user.id
        ).first()
        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can edit group tags'}), 403
    else:
        if tag.user_id != current_user.id:
            return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    if 'name' in data:
        tag.name = data['name']
    if 'color' in data:
        tag.color = data['color']
    if 'custom_prompt' in data:
        tag.custom_prompt = data['custom_prompt']
    if 'title_prompt' in data:
        tag.title_prompt = data['title_prompt'] or None
    if 'default_language' in data:
        tag.default_language = data['default_language']
    if 'default_min_speakers' in data:
        tag.default_min_speakers = data['default_min_speakers']
    if 'default_max_speakers' in data:
        tag.default_max_speakers = data['default_max_speakers']
    if 'protect_from_deletion' in data:
        tag.protect_from_deletion = data['protect_from_deletion']

    db.session.commit()

    return jsonify({'success': True, 'tag': {
        'id': tag.id,
        'name': tag.name,
        'color': tag.color,
        'custom_prompt': tag.custom_prompt,
        'title_prompt': tag.title_prompt,
        'default_language': tag.default_language,
        'default_min_speakers': tag.default_min_speakers,
        'default_max_speakers': tag.default_max_speakers,
        'protect_from_deletion': tag.protect_from_deletion
    }})


@api_v1_bp.route('/tags/<int:tag_id>', methods=['DELETE'])
@require_scope('delete')
@login_required
def delete_tag(tag_id):
    """Delete a tag."""
    from src.models.organization import GroupMembership

    tag = db.session.get(Tag, tag_id)
    if not tag:
        return jsonify({'error': 'Tag not found'}), 404

    # Check permission
    if tag.group_id:
        membership = GroupMembership.query.filter_by(
            group_id=tag.group_id,
            user_id=current_user.id
        ).first()
        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can delete group tags'}), 403
    else:
        if tag.user_id != current_user.id:
            return jsonify({'error': 'Permission denied'}), 403

    # Remove all recording associations
    RecordingTag.query.filter_by(tag_id=tag_id).delete()

    db.session.delete(tag)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Tag deleted'})



def _tag_problem(recording, tag):
    """Why the caller may not apply tag to recording, or None."""
    from src.models.organization import GroupMembership
    if tag is None:
        return 'not found'
    if tag.group_id:
        membership = GroupMembership.query.filter_by(group_id=tag.group_id, user_id=current_user.id).first()
        if not membership:
            return 'no access'
        if recording.user_id != current_user.id and membership.role != 'admin':
            return 'only the recording owner or a group admin can apply group tags'
        return None
    return None if tag.user_id == current_user.id else 'no access'


def _share_scope_refusal(recording, tags):
    """403 when applying these tags would share the recording and the token
    lacks the share scope (mailr spec section 8); nothing has changed yet."""
    from src.services.tag_sharing import share_targets
    from src.utils.token_auth import current_api_token, scope_error_response
    token = current_api_token()
    if token is None or token.scope_set is None or 'share' in token.scope_set:
        return None
    if any(share_targets(recording, tag) for tag in tags):
        return scope_error_response({'share'}, token)
    return None


@api_v1_bp.route('/recordings/<int:recording_id>/tags', methods=['POST'])
@require_scope('write')
@login_required
def add_tags_to_recording(recording_id):
    """Add tag(s) to a recording. Needs edit access; a group tag that shares
    the recording also needs the share scope on a scoped token."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json() or {}
    tag_ids = data.get('tag_ids', [])
    if not tag_ids:
        # Support single tag_id for backward compatibility
        tag_id = data.get('tag_id')
        if tag_id:
            tag_ids = [tag_id]
        else:
            return jsonify({'error': 'tag_ids or tag_id required'}), 400

    errors = []
    to_add = []
    present = {rt.tag_id for rt in RecordingTag.query.filter_by(recording_id=recording_id)}
    for tag_id in tag_ids:
        tag = db.session.get(Tag, tag_id)
        problem = _tag_problem(recording, tag)
        if problem == 'not found':
            errors.append(f'Tag {tag_id} not found')
        elif problem:
            errors.append(f'No access to tag {tag_id}')
        elif tag.id not in present and tag not in to_add:
            to_add.append(tag)

    refusal = _share_scope_refusal(recording, to_add)
    if refusal is not None:
        return refusal

    from src.services.tag_sharing import apply_tag_shares
    max_order = db.session.query(func.max(RecordingTag.order)).filter_by(recording_id=recording_id).scalar() or 0
    added_tags = []
    for offset, tag in enumerate(to_add, 1):
        db.session.add(RecordingTag(recording_id=recording_id, tag_id=tag.id, order=max_order + offset))
        apply_tag_shares(recording, tag)
        added_tags.append({'id': tag.id, 'name': tag.name})

    db.session.commit()

    return jsonify({
        'success': True,
        'added_tags': added_tags,
        'errors': errors if errors else None
    })


@api_v1_bp.route('/recordings/<int:recording_id>/tags', methods=['PUT'])
@require_scope('write')
@login_required
def replace_recording_tags(recording_id):
    """Set the recording's tags to exactly tag_ids, in that order.

    Tags the caller cannot see (another user's personal tags) stay on the
    recording. An unknown or forbidden id refuses the whole request.
    """
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found', 'code': 'not_found'}), 404
    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403
    data = request.get_json(silent=True) or {}
    tag_ids = data.get('tag_ids')
    if not isinstance(tag_ids, list) or not all(isinstance(t, int) and not isinstance(t, bool) for t in tag_ids):
        return jsonify({'error': 'tag_ids must be a list of tag ids', 'code': 'invalid_parameter'}), 400
    wanted, bad = [], []
    for tag_id in dict.fromkeys(tag_ids):
        tag = db.session.get(Tag, tag_id)
        if _tag_problem(recording, tag):
            bad.append(tag_id)
        else:
            wanted.append(tag)
    if bad:
        return jsonify({'error': 'Unknown or forbidden tag ids', 'code': 'invalid_parameter',
                        'tag_ids': bad}), 400

    visible = {t.id for t in recording.get_visible_tags(current_user)}
    current = RecordingTag.query.filter_by(recording_id=recording_id).order_by(RecordingTag.order).all()
    current_ids = [rt.tag_id for rt in current]
    added = [t for t in wanted if t.id not in current_ids]
    removed = [rt.tag_id for rt in current if rt.tag_id in visible and rt.tag_id not in {t.id for t in wanted}]

    refusal = _share_scope_refusal(recording, added)
    if refusal is not None:
        return refusal

    from src.services.tag_sharing import apply_tag_shares
    for rt in current:
        if rt.tag_id in removed:
            db.session.delete(rt)
    db.session.flush()
    kept_hidden = [rt for rt in current if rt.tag_id not in visible]
    by_id = {rt.tag_id: rt for rt in current if rt.tag_id not in removed}
    order = 0
    for tag in wanted:
        order += 1
        if tag.id in by_id:
            if by_id[tag.id].order != order:
                by_id[tag.id].order = order
        else:
            db.session.add(RecordingTag(recording_id=recording_id, tag_id=tag.id, order=order))
            apply_tag_shares(recording, tag)
    for rt in kept_hidden:
        order += 1
        rt.order = order
    db.session.commit()
    return jsonify({
        'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in wanted],
        'added': [t.id for t in added],
        'removed': removed,
    })


@api_v1_bp.route('/recordings/<int:recording_id>/tags/<int:tag_id>', methods=['DELETE'])
@require_scope('write')
@login_required
def remove_tag_from_recording(recording_id, tag_id):
    """Remove a tag from a recording."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    recording_tag = RecordingTag.query.filter_by(
        recording_id=recording_id,
        tag_id=tag_id
    ).first()

    if not recording_tag:
        return jsonify({'error': 'Tag not on this recording'}), 404

    db.session.delete(recording_tag)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Tag removed'})


# =============================================================================
# Folder Management
#
# These endpoints delegate to the existing web handlers in src/api/folders.py
# so permission semantics (group folders, member vs admin) match the web UI
# exactly. The web handlers already return jsonified, API-shaped responses
# (`folder.to_dict()` for success, `{"error": "..."}` for errors), so the
# delegation produces clean API output.
#
# Contract dependency: the v1 API's response shape on these routes is whatever
# the corresponding handler in folders.py returns. Changing those return
# shapes silently changes this API. A future cleanup should extract a
# services/folder.py layer that returns data dicts, with both the web and v1
# endpoints thin-wrapping with jsonify; that decouples the API contract from
# the web response framing.
# =============================================================================

@api_v1_bp.route('/folders', methods=['GET'])
@require_scope('read')
@login_required
def list_folders():
    """List folders the user can access (personal + group folders)."""
    from src.api.folders import get_folders as _web_get_folders
    return _web_get_folders()


@api_v1_bp.route('/folders', methods=['POST'])
@require_scope('write')
@login_required
def create_folder():
    """Create a new folder. Accepts the same JSON body as the web endpoint."""
    from src.api.folders import create_folder as _web_create_folder
    return _web_create_folder()


@api_v1_bp.route('/folders/<int:folder_id>', methods=['GET'])
@require_scope('read')
@login_required
def get_folder(folder_id):
    """Get a single folder by id."""
    from src.models.organization import Folder, GroupMembership

    folder = db.session.get(Folder, folder_id)
    if not folder:
        return jsonify({'error': 'Folder not found'}), 404

    # Personal folders: must own it. Group folders: must be a member.
    if folder.group_id is None:
        if folder.user_id != current_user.id:
            return jsonify({'error': 'Permission denied'}), 403
    else:
        membership = GroupMembership.query.filter_by(
            user_id=current_user.id, group_id=folder.group_id
        ).first()
        if not membership:
            return jsonify({'error': 'Permission denied'}), 403

    folder_dict = folder.to_dict()
    if folder.group_id is None:
        folder_dict['can_edit'] = True
    else:
        membership = GroupMembership.query.filter_by(
            user_id=current_user.id, group_id=folder.group_id
        ).first()
        folder_dict['can_edit'] = bool(membership and membership.role == 'admin')
    return jsonify(folder_dict)


@api_v1_bp.route('/folders/<int:folder_id>', methods=['PATCH', 'PUT'])
@require_scope('write')
@login_required
def update_folder(folder_id):
    """Update a folder. Same JSON body as the web endpoint."""
    from src.api.folders import update_folder as _web_update_folder
    return _web_update_folder(folder_id)


@api_v1_bp.route('/folders/<int:folder_id>', methods=['DELETE'])
@require_scope('delete')
@login_required
def delete_folder(folder_id):
    """Delete a folder. Recordings in it are unassigned."""
    from src.api.folders import delete_folder as _web_delete_folder
    return _web_delete_folder(folder_id)


# =============================================================================
# Transcription Connector & Model Discovery
# =============================================================================

@api_v1_bp.route('/transcription', methods=['GET'])
@require_scope('read')
@login_required
def get_transcription_info():
    """
    Return information about the active transcription connector so API
    clients can know which fields are accepted and which model values are
    valid for `transcription_model` overrides.

    Response shape:
      {
        "connector": "openai_transcribe",
        "capabilities": {
          "diarization": true,
          "speaker_count_control": false,
          "hotwords": true,
          "initial_prompt": true,
          "timestamps": true,
          "language_detection": true,
          "chunking": false
        },
        "models": [
          {"value": "gpt-4o-transcribe-diarize", "label": "GPT-4o Diarize"},
          ...
        ],
        "default_model": "gpt-4o-transcribe-diarize"
      }
    """
    from src.models import SystemSetting
    from src.config.app_config import (
        TRANSCRIPTION_MODEL,
        USE_NEW_TRANSCRIPTION_ARCHITECTURE,
        USE_ASR_ENDPOINT,
    )

    connector_name = None
    capabilities = {
        'diarization': USE_ASR_ENDPOINT,
        'speaker_count_control': USE_ASR_ENDPOINT,
        'hotwords': USE_ASR_ENDPOINT,
        'initial_prompt': USE_ASR_ENDPOINT,
        'timestamps': True,
        'language_detection': True,
        'chunking': False,
    }
    connector = None
    if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
        try:
            from src.services.transcription import get_registry
            registry = get_registry()
            connector_name = registry.get_active_connector_name()
            connector = registry.get_active_connector()
            if connector:
                capabilities = {
                    'diarization': connector.supports_diarization,
                    'speaker_count_control': connector.supports_speaker_count_control,
                    'hotwords': connector.supports_hotwords,
                    'initial_prompt': connector.supports_initial_prompt,
                    'timestamps': True,
                    'language_detection': True,
                    'chunking': connector.supports_chunking,
                }
        except Exception as e:
            current_app.logger.warning(f"Could not read connector capabilities: {e}")

    # Visible models: admin-curated DB list takes precedence over the
    # TRANSCRIPTION_MODELS_AVAILABLE env var.
    models = []
    raw = SystemSetting.get_setting('transcription_models_visible_json', None)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get('value'):
                        models.append({
                            'value': item['value'],
                            'label': item.get('label') or item['value'],
                        })
                    elif isinstance(item, str) and item:
                        models.append({'value': item, 'label': item})
        except Exception:
            models = []
    if not models:
        # TRANSCRIPTION_MODELS_AVAILABLE paired with TRANSCRIPTION_MODEL_LABELS.
        from src.config.app_config import TRANSCRIPTION_MODEL_OPTIONS
        models = [dict(option) for option in TRANSCRIPTION_MODEL_OPTIONS]

    # The model a transcription with no tag, folder or request choice uses:
    # the admin default, else the connector's own configured model.
    from src.services.transcription_defaults import resolve_transcription_model
    default_model = (resolve_transcription_model(None)
                     or (getattr(connector, 'model', None) if connector else None)
                     or None)

    return jsonify({
        'connector': connector_name,
        'capabilities': capabilities,
        'models': models,
        'default_model': default_model,
    })


# =============================================================================
# Speaker Management
# =============================================================================

@api_v1_bp.route('/speakers', methods=['GET'])
@require_scope('read')
@login_required
def list_speakers():
    """List all speakers for the current user."""
    speakers = Speaker.query.filter_by(user_id=current_user.id)\
                           .order_by(Speaker.use_count.desc(), Speaker.last_used.desc())\
                           .all()

    return jsonify({
        'speakers': [{
            'id': s.id,
            'name': s.name,
            'use_count': s.use_count,
            'last_used': s.last_used.isoformat() if s.last_used else None,
            'confidence_score': s.confidence_score,
            'has_voice_profile': s.average_embedding is not None
        } for s in speakers]
    })


@api_v1_bp.route('/speakers', methods=['POST'])
@require_scope('write')
@login_required
def create_speaker():
    """Create a new speaker."""
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': 'Speaker name is required'}), 400

    name = data['name'].strip()

    # Check if already exists
    existing = Speaker.query.filter_by(user_id=current_user.id, name=name).first()
    if existing:
        return jsonify({'error': 'Speaker with this name already exists'}), 400

    speaker = Speaker(
        name=name,
        user_id=current_user.id,
        use_count=0,
        created_at=datetime.utcnow()
    )
    db.session.add(speaker)
    db.session.commit()

    return jsonify({
        'id': speaker.id,
        'name': speaker.name,
        'use_count': speaker.use_count,
        'created_at': speaker.created_at.isoformat()
    }), 201


@api_v1_bp.route('/speakers/<int:speaker_id>', methods=['PUT'])
@require_scope('write')
@login_required
def update_speaker(speaker_id):
    """Update a speaker (cascades name changes to recordings)."""
    speaker = db.session.get(Speaker, speaker_id)
    if not speaker:
        return jsonify({'error': 'Speaker not found'}), 404

    if speaker.user_id != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_name = speaker.name
    new_name = data.get('name', '').strip()

    if not new_name:
        return jsonify({'error': 'Speaker name is required'}), 400

    if new_name != old_name:
        # Update speaker name
        speaker.name = new_name

        # Update all recordings that have this speaker in their transcription
        from src.services.speaker import update_speaker_in_recordings
        try:
            update_speaker_in_recordings(current_user.id, old_name, new_name)
        except Exception as e:
            current_app.logger.error(f"Error updating speaker in recordings: {e}")

    db.session.commit()

    return jsonify({
        'success': True,
        'speaker': {
            'id': speaker.id,
            'name': speaker.name,
            'use_count': speaker.use_count
        }
    })


@api_v1_bp.route('/speakers/<int:speaker_id>', methods=['DELETE'])
@require_scope('delete')
@login_required
def delete_speaker(speaker_id):
    """Delete a speaker."""
    speaker = db.session.get(Speaker, speaker_id)
    if not speaker:
        return jsonify({'error': 'Speaker not found'}), 404

    if speaker.user_id != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403

    db.session.delete(speaker)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Speaker deleted'})


@api_v1_bp.route('/recordings/<int:recording_id>/speakers', methods=['GET'])
@require_scope('read')
@login_required
@_conditional('k')
def get_recording_speakers(recording_id):
    """Get speakers in a recording with suggestions."""
    from src.services.speaker_embedding_matcher import find_matching_speakers

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    # Parse transcription to get speakers
    speakers_in_recording = []
    speaker_counts = {}

    if recording.transcription:
        try:
            segments = json.loads(recording.transcription)
            for seg in segments:
                speaker = seg.get('speaker', 'Unknown')
                speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    # Build speaker list with identification info
    for label, count in speaker_counts.items():
        # Check if this speaker label has been identified
        identified_name = None
        speaker_id = None

        # Look for speaker in user's speakers by checking recordings
        # This is a simplified check - actual implementation would check speaker_embeddings
        speakers_in_recording.append({
            'label': label,
            'identified_name': identified_name,
            'speaker_id': speaker_id,
            'segment_count': count
        })

    # Get voice-based suggestions. speaker_embeddings maps each SPEAKER_XX
    # label to one embedding (any dimension); find_matching_speakers takes a
    # single embedding (plus user_id), compares it within the recording's
    # embedding space and returns a sorted match list with similarity
    # already expressed as a percentage.
    suggestions = {}
    if recording.speaker_embeddings:
        try:
            embeddings_data = (
                json.loads(recording.speaker_embeddings)
                if isinstance(recording.speaker_embeddings, str)
                else recording.speaker_embeddings
            )
            from src.services.voice_profiles import suggestion_threshold
            space_id = recording.speaker_embeddings_space_id
            for label, embedding in embeddings_data.items():
                if not embedding:
                    continue
                matches = find_matching_speakers(embedding, current_user.id,
                                                 threshold=suggestion_threshold(space_id), space_id=space_id)
                suggestions[label] = [{
                    'speaker_id': m['speaker_id'],
                    'name': m['name'],
                    'similarity': m['similarity']
                } for m in matches[:3]]
        except Exception as e:
            current_app.logger.error(f"Error getting speaker suggestions: {e}")

    return jsonify({
        'speakers': speakers_in_recording,
        'suggestions': suggestions
    })


@api_v1_bp.route('/recordings/<int:recording_id>/speakers/assign', methods=['PUT'])
@require_scope('write')
@login_required
def assign_speakers(recording_id):
    """
    Assign speaker names to a recording's transcription segments.

    Accepts the same speaker_map format as the web UI, plus convenience
    formats for API callers (plain strings).

    Request body:
    {
        "speaker_map": {
            "SPEAKER_00": "Jane Doe",                       // string shorthand
            "SPEAKER_01": {"name": "Bob", "isMe": false}    // full object
        },
        "regenerate_summary": false
    }
    """
    from src.services.speaker import update_speaker_usage
    from src.services.job_queue import job_queue

    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify({'error': 'Permission denied'}), 403

        data = request.get_json()
        if not data or 'speaker_map' not in data:
            return jsonify({'error': 'speaker_map is required'}), 400

        raw_speaker_map = data['speaker_map']
        regenerate_summary = data.get('regenerate_summary', False)

        if not isinstance(raw_speaker_map, dict):
            return jsonify({'error': 'speaker_map must be an object'}), 400

        # Normalize values to {name, isMe} format (same as web UI expects)
        speaker_map = {}
        for label, value in raw_speaker_map.items():
            if isinstance(value, str):
                speaker_map[label] = {'name': value.strip(), 'isMe': False}
            elif isinstance(value, dict):
                speaker_map[label] = {
                    'name': value.get('name', '').strip() if value.get('name') else '',
                    'isMe': value.get('isMe', False)
                }
            else:
                return jsonify({'error': f'Invalid value type for speaker "{label}"'}), 400

        # Same rename and follow-up steps as the web update_speakers (#412).
        from src.services.speaker_assignment import apply_speaker_names
        result = apply_speaker_names(recording, current_user, speaker_map, regenerate_summary)
        summary_queued = result['summary_queued']
        embeddings_updated = result['embeddings_updated']
        snippets_created = result['snippets_created']

        return jsonify({
            'success': True,
            'message': 'Speakers updated successfully.',
            'recording': {
                'id': recording.id,
                'title': recording.title,
                'participants': recording.participants,
                'status': recording.status
            },
            'summary_queued': summary_queued,
            'embeddings_updated': embeddings_updated,
            'snippets_created': snippets_created
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error assigning speakers for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@api_v1_bp.route('/recordings/<int:recording_id>/speakers/identify', methods=['POST'])
@require_scope('process')
@login_required
def identify_speakers(recording_id):
    """
    Trigger LLM-based auto-identification of speakers from transcript context.
    Returns suggestions only - does not modify the recording.

    Uses the shared identification service with JSON schema support,
    name sanitization, and fallback logic.
    """
    from src.services.speaker_identification import identify_speakers_from_transcript

    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user):
            return jsonify({'error': 'Permission denied'}), 403

        if not recording.transcription:
            return jsonify({'error': 'No transcription available for speaker identification'}), 400

        try:
            transcription_data = json.loads(recording.transcription)
        except (json.JSONDecodeError, TypeError):
            return jsonify({'error': 'Transcription format not supported for auto-identification'}), 400

        if not isinstance(transcription_data, list):
            return jsonify({'error': 'Transcription format not supported for auto-identification'}), 400

        speaker_map = identify_speakers_from_transcript(transcription_data, current_user.id)

        if not speaker_map:
            return jsonify({'error': 'No speakers found in transcription'}), 400

        return jsonify({'success': True, 'speaker_map': speaker_map})

    except ValueError as ve:
        return jsonify({'error': str(ve)}), 503
    except Exception as e:
        current_app.logger.error(f"Error during auto speaker identification for recording {recording_id}: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


# =============================================================================
# Processing Operations
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/transcribe', methods=['POST'])
@require_scope('process')
@login_required
def start_transcription(recording_id):
    """Queue transcription for a recording."""
    from src.services.job_queue import job_queue

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    # Same checks, clearing and settings as the web reprocess (#412).
    from src.services.reprocessing import queue_transcription_reprocess, ReprocessError
    data = request.get_json() or {}
    overrides = {
        'min_speakers': data.get('min_speakers'),
        'max_speakers': data.get('max_speakers'),
        'hotwords': data.get('hotwords'),
        'initial_prompt': data.get('initial_prompt'),
        'transcription_model': data.get('transcription_model'),
    }
    if 'language' in data:
        overrides['language'] = data.get('language')
    try:
        job_id = queue_transcription_reprocess(recording, current_user, overrides)
    except ReprocessError as e:
        return jsonify({'error': e.message}), e.status

    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'QUEUED',
        'message': 'Transcription queued'
    })


@api_v1_bp.route('/recordings/<int:recording_id>/summarize', methods=['POST'])
@require_scope('process')
@login_required
def start_summarization(recording_id):
    """Queue summarization for a recording with optional custom prompt."""
    from src.services.job_queue import job_queue

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    # Same checks, clearing and parameters as the web reprocess, including
    # prompt_mode and prompt_variables (#412).
    from src.services.reprocessing import queue_summary_reprocess, ReprocessError
    data = request.get_json() or {}
    try:
        job_id = queue_summary_reprocess(recording, current_user,
                                         custom_prompt=data.get('custom_prompt'),
                                         prompt_mode=data.get('prompt_mode'),
                                         prompt_variables=data.get('prompt_variables'))
    except ReprocessError as e:
        return jsonify({'error': e.message}), e.status

    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'QUEUED',
        'message': 'Summarization queued'
    })


# =============================================================================
# Chat with Recording
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/chat', methods=['POST'])
@require_scope('process')
@login_required
def chat_with_recording(recording_id):
    """Chat about a recording's content."""
    from src.services.llm import chat_client, call_chat_completion
    from src.tasks.processing import format_transcription_for_llm, _resolve_timestamp_template_format
    from src.models import SystemSetting

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    if not recording.transcription:
        return jsonify({'error': 'No transcription available'}), 400

    data = request.get_json()
    if not data or not data.get('message'):
        return jsonify({'error': 'message is required'}), 400

    user_message = data['message']
    conversation_history = data.get('conversation_history', [])

    # Check if chat client is available
    if chat_client is None:
        return jsonify({'error': 'Chat service not available'}), 503

    # Format transcription (optionally with timestamps per the user's setting, #304)
    _chat_ts = bool(current_user.chat_include_timestamps)
    formatted_transcription = format_transcription_for_llm(
        recording.transcription,
        include_timestamps=_chat_ts,
        template_format=_resolve_timestamp_template_format(
            current_user, current_user.chat_timestamp_template_id) if _chat_ts else None,
    )

    # Get transcript limit
    transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
    if transcript_limit != -1:
        formatted_transcription = formatted_transcription[:transcript_limit]

    # Build system prompt
    system_prompt = f"""You are a helpful assistant analyzing a recording. Answer questions based on the transcript below.

Meeting: {recording.title}
Participants: {recording.participants or 'Not specified'}

Transcript:
{formatted_transcription}

Notes: {recording.notes or 'None'}
"""
    # Same output language as web chat (#412 audit S8).
    if current_user.output_language:
        system_prompt += f"\nPlease provide all your responses in {current_user.output_language}.\n"

    # Build messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    try:
        # Same admin chat temperature as web chat (#412 audit S8).
        from src.services.llm_settings import get_temperature
        completion = call_chat_completion(messages, temperature=get_temperature('chat'), user_id=current_user.id)
        reply = completion.choices[0].message.content

        return jsonify({
            'response': reply,
            'sources': []  # Could be enhanced to extract relevant segments
        })
    except Exception as e:
        current_app.logger.error(f"Chat error: {e}")
        return jsonify({'error': 'Chat failed'}), 500


# =============================================================================
# Calendar Events
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/events', methods=['GET'])
@require_scope('read')
@login_required
@_conditional('e')
def get_recording_events(recording_id):
    """Get calendar events extracted from a recording."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    events = Event.query.filter_by(recording_id=recording_id).all()

    return jsonify({'events': [e.api_dict() for e in events]})


@api_v1_bp.route('/recordings/<int:recording_id>/events/ics', methods=['GET'])
@require_scope('read')
@login_required
def download_events_ics(recording_id):
    """Download all events as ICS file."""
    from src.api.events import generate_ics_content

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    events = Event.query.filter_by(recording_id=recording_id).all()
    if not events:
        return jsonify({'error': 'No events found'}), 404

    # Same builder as the web download (#412).
    from src.services.calendar import generate_combined_ics
    ics_text = generate_combined_ics(events)

    from flask import Response
    return Response(
        ics_text,
        mimetype='text/calendar',
        headers={'Content-Disposition': f'attachment; filename=events-{recording_id}.ics'}
    )


# =============================================================================
# Audio Download
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/audio', methods=['GET'])
@require_scope('read')
@login_required
def download_audio(recording_id):
    """Download or stream audio file."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    if recording.audio_deleted_at:
        return jsonify({'error': 'Audio has been deleted'}), 404

    if not recording.audio_path:
        return jsonify({'error': 'No audio file'}), 404

    download = request.args.get('download', 'false').lower() == 'true'

    # original_filename is user-controlled at upload time. Sanitize it
    # via secure_filename before using it as a download name so null bytes or
    # path-traversal segments can't reach a confused download dialog (this also
    # feeds the S3 presigned Content-Disposition below).
    from werkzeug.utils import secure_filename
    raw_name = recording.original_filename or f'recording-{recording_id}.mp3'
    safe_download_name = secure_filename(raw_name) or f'recording-{recording_id}'

    storage = get_storage_service()
    delivery = storage.get_audio_delivery(
        recording.audio_path,
        download=download,
        mime_type=recording.mime_type or 'audio/mpeg',
        download_name=safe_download_name,
        is_public=False,
    )

    if delivery.mode == 'redirect_url':
        return redirect(delivery.url, code=302)

    if not delivery.local_path or not os.path.exists(delivery.local_path):
        return jsonify({'error': 'Audio file not found'}), 404

    return send_file(
        delivery.local_path,
        mimetype=recording.mime_type or 'audio/mpeg',
        as_attachment=download,
        download_name=safe_download_name,
    )


# =============================================================================
# Batch Operations
# =============================================================================

@api_v1_bp.route('/recordings/batch', methods=['PATCH'])
@require_scope('write')
@login_required
def batch_update_recordings():
    """Batch update multiple recordings."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    recording_ids = data.get('recording_ids', [])
    updates = data.get('updates', {})

    if not recording_ids:
        return jsonify({'error': 'recording_ids required'}), 400

    # If folder_id is being set on the batch, validate target folder access
    # once up front. The same destination folder applies to every recording
    # in the batch, so the permission check is identical for all of them.
    target_folder_id = None
    if 'folder_id' in updates:
        new_folder_id = updates['folder_id']
        if new_folder_id is not None:
            from src.models.organization import Folder, GroupMembership
            target = db.session.get(Folder, new_folder_id)
            if not target:
                return jsonify({'error': f'Folder {new_folder_id} not found'}), 404
            if target.group_id is None:
                if target.user_id != current_user.id:
                    return jsonify({'error': 'No access to target folder'}), 403
            else:
                membership = GroupMembership.query.filter_by(
                    user_id=current_user.id, group_id=target.group_id
                ).first()
                if not membership:
                    return jsonify({'error': 'No access to target folder'}), 403
            target_folder_id = new_folder_id

    batch_archived = None
    if 'is_archived' in updates:
        batch_archived = parse_archived_flag(updates['is_archived'])
        if batch_archived is None:
            return jsonify({'error': 'is_archived must be a boolean'}), 400

    results = []
    for recording_id in recording_ids:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            results.append({'id': recording_id, 'success': False, 'error': 'Not found'})
            continue

        if not has_recording_access(recording, current_user, require_edit=True):
            results.append({'id': recording_id, 'success': False, 'error': 'Permission denied'})
            continue

        try:
            if 'is_inbox' in updates:
                recording.is_inbox = bool(updates['is_inbox'])
            if 'is_highlighted' in updates:
                recording.is_highlighted = bool(updates['is_highlighted'])
            if 'is_archived' in updates:
                set_user_archived(recording, current_user, batch_archived, commit=False)
            if 'folder_id' in updates:
                # `target_folder_id` already validated above (None to remove,
                # or a valid folder id the caller has access to).
                recording.folder_id = target_folder_id

            # Handle tag additions.
            # Authorize each tag_id: personal tags must be owned, group
            # tags require caller membership in the tag's group. Without
            # this check, the batch endpoint was an IDOR that let any
            # user attach arbitrary tag_ids (including admin-curated or
            # other users' private tags) to their own recordings.
            if 'add_tag_ids' in updates:
                from src.models.organization import Tag, GroupMembership
                for tag_id in updates['add_tag_ids']:
                    tag = db.session.get(Tag, tag_id)
                    if tag is None:
                        continue  # silently skip unknown ids
                    if tag.group_id is None:
                        if tag.user_id != current_user.id:
                            continue  # not yours
                    else:
                        is_member = GroupMembership.query.filter_by(
                            user_id=current_user.id, group_id=tag.group_id
                        ).first() is not None
                        if not is_member:
                            continue  # not a member of the tag's group
                    existing = RecordingTag.query.filter_by(
                        recording_id=recording_id,
                        tag_id=tag_id
                    ).first()
                    if not existing:
                        max_order = db.session.query(func.max(RecordingTag.order)).filter_by(
                            recording_id=recording_id
                        ).scalar() or 0
                        recording_tag = RecordingTag(
                            recording_id=recording_id,
                            tag_id=tag_id,
                            order=max_order + 1
                        )
                        db.session.add(recording_tag)

            # Handle tag removals. Removing an unauthorised tag from
            # one's own recording isn't an IDOR per se (you're only
            # editing your own RecordingTag rows) but symmetry keeps
            # the contract simple.
            if 'remove_tag_ids' in updates:
                from src.models.organization import Tag, GroupMembership
                for tag_id in updates['remove_tag_ids']:
                    tag = db.session.get(Tag, tag_id)
                    if tag is None:
                        continue
                    if tag.group_id is None:
                        if tag.user_id != current_user.id:
                            continue
                    else:
                        is_member = GroupMembership.query.filter_by(
                            user_id=current_user.id, group_id=tag.group_id
                        ).first() is not None
                        if not is_member:
                            continue
                    RecordingTag.query.filter_by(
                        recording_id=recording_id,
                        tag_id=tag_id
                    ).delete()

            results.append({'id': recording_id, 'success': True})
        except Exception as e:
            results.append({'id': recording_id, 'success': False, 'error': str(e)})

    db.session.commit()

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'success': True,
        'updated': success_count,
        'failed': len(results) - success_count,
        'results': results
    })


@api_v1_bp.route('/recordings/batch', methods=['DELETE'])
@require_scope('delete')
@login_required
def batch_delete_recordings():
    """Batch delete multiple recordings."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    recording_ids = data.get('recording_ids', [])
    if not recording_ids:
        return jsonify({'error': 'recording_ids required'}), 400

    USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
    if not USERS_CAN_DELETE and not current_user.is_admin:
        return jsonify({'error': 'Deletion not allowed'}), 403

    results = []
    for recording_id in recording_ids:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            results.append({'id': recording_id, 'success': False, 'error': 'Not found'})
            continue

        if recording.user_id != current_user.id and not current_user.is_admin:
            results.append({'id': recording_id, 'success': False, 'error': 'Permission denied'})
            continue

        try:
            from src.services.recording_deletion import delete_recording_completely
            delete_recording_completely(recording)
            results.append({'id': recording_id, 'success': True})
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error deleting recording {recording_id}: {e}")
            results.append({'id': recording_id, 'success': False, 'error': 'Delete failed'})

    if any(r['success'] for r in results):
        from src.services.recording_deletion import cleanup_orphaned_speakers_quietly
        cleanup_orphaned_speakers_quietly()

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'success': True,
        'deleted': success_count,
        'failed': len(results) - success_count,
        'results': results
    })


@api_v1_bp.route('/recordings/batch/transcribe', methods=['POST'])
@require_scope('process')
@login_required
def batch_transcribe_recordings():
    """Batch queue transcriptions for multiple recordings."""
    from src.services.job_queue import job_queue

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    recording_ids = data.get('recording_ids', [])
    if not recording_ids:
        return jsonify({'error': 'recording_ids required'}), 400

    results = []
    for recording_id in recording_ids:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            results.append({'id': recording_id, 'success': False, 'error': 'Not found'})
            continue

        if not has_recording_access(recording, current_user, require_edit=True):
            results.append({'id': recording_id, 'success': False, 'error': 'Permission denied'})
            continue

        # Same checks, clearing and settings as a single reprocess (#412).
        from src.services.reprocessing import queue_transcription_reprocess, ReprocessError
        try:
            job_id = queue_transcription_reprocess(recording, current_user)
            results.append({'id': recording_id, 'success': True, 'job_id': job_id})
        except ReprocessError as e:
            results.append({'id': recording_id, 'success': False, 'error': e.message})
        except Exception as e:
            results.append({'id': recording_id, 'success': False, 'error': str(e)})

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'success': True,
        'queued': success_count,
        'failed': len(results) - success_count,
        'results': results
    })


# =============================================================================
# Settings
# =============================================================================

@api_v1_bp.route('/settings/auto-summarization', methods=['PUT'])
@require_scope('account')
@login_required
def update_auto_summarization():
    """Toggle auto-summarization for the current user."""
    data = request.get_json()

    if data is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    if 'enabled' not in data:
        return jsonify({'error': 'enabled field is required'}), 400

    current_user.auto_summarization = bool(data['enabled'])
    db.session.commit()

    return jsonify({
        'success': True,
        'auto_summarization': current_user.auto_summarization
    })


def _normalize_asr_filename(file_name, uploaded_file):
    raw_name = (file_name or uploaded_file.filename or '').strip()
    raw_name = re.split(r'[/\\]+', raw_name)[-1]
    normalized = secure_filename(raw_name)
    if not normalized:
        normalized = secure_filename(uploaded_file.filename or '') or 'recording.bin'

    stem, extension = os.path.splitext(normalized)
    extension = extension[:20]
    max_stem = max(1, 180 - len(extension))
    return f'{stem[:max_stem]}{extension}'


def _asr_meeting_date(raw_date):
    if raw_date is None or str(raw_date).strip() == '':
        return None
    try:
        timestamp = float(raw_date)
        if not math.isfinite(timestamp) or timestamp < 0:
            return None
        # ASR documents only "epoch time". Accept both common units.
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return parsed.isoformat().replace('+00:00', 'Z')
    except (ValueError, TypeError, OSError, OverflowError):
        return None


_ASR_RATE_UNITS_PER_MINUTE = 520
_ASR_MIN_REQUEST_COST = 52


def _asr_upload_rate_cost():
    """Bound both requests and declared MiB with one pre-parse IP bucket."""
    content_length = request.content_length
    if content_length is None:
        return _ASR_RATE_UNITS_PER_MINUTE
    declared_mib = max(1, math.ceil(content_length / (1024 * 1024)))
    return max(_ASR_MIN_REQUEST_COST, declared_mib)


@api_v1_bp.route('/integrations/asr-voice-recorder/upload', methods=['POST'])
@require_scope('upload')
@rate_limit(f'{_ASR_RATE_UNITS_PER_MINUTE} per minute', cost=_asr_upload_rate_cost)
def upload_from_asr_voice_recorder():
    """Accept an ASR Voice Recorder connection test or completed upload."""
    try:
        # ASR sends its token inside the multipart body, so Flask must parse
        # the request before authentication can complete. Apply the regular
        # audio-file ceiling to this route before parsing instead of the
        # larger global video ceiling. The small allowance covers multipart
        # headers and metadata around a file at the configured limit.
        from src.models import SystemSetting
        regular_limit_mb = max(1, int(SystemSetting.get_setting('max_file_size_mb', 250)))
        # Assigning request.max_content_length requires Flask >= 3.1 /
        # Werkzeug >= 3.1 (older versions expose it as a read-only property
        # and this line raises AttributeError, 500ing every upload).
        # requirements.txt pins flask==3.1.0 / werkzeug==3.1.3.
        request.max_content_length = regular_limit_mb * 1024 * 1024 + 64 * 1024

        secret = request.form.get('secret', '')
        owner = load_user_from_token_value(secret)
        if owner is None:
            return jsonify({'error': 'Authentication failed'}), 401
        # A scoped token must carry 'upload' (mailr spec G1); full tokens pass.
        _token = request.environ.get('_speakr_secret_token')
        if _token is not None and _token.scope_set is not None and 'upload' not in _token.scope_set:
            return jsonify({'error': 'Authentication failed'}), 401

        uploaded_file = request.files.get('file')
        if uploaded_file is None:
            upload_fields = ('file_name', 'date', 'duration', 'note')
            has_upload_metadata = any(request.form.get(field) for field in upload_fields)
            if not has_upload_metadata:
                return jsonify({'status': 'ok', 'connection_test': True}), 200
            return jsonify({'error': 'No file provided'}), 400

        form = {}
        note = request.form.get('note')
        if note is not None:
            form['notes'] = note
        meeting_date = _asr_meeting_date(request.form.get('date'))
        if meeting_date:
            form['meeting_date'] = meeting_date

        return ingest_uploaded_recording(
            owner=owner,
            uploaded_file=uploaded_file,
            form=form,
            original_filename=_normalize_asr_filename(
                request.form.get('file_name'),
                uploaded_file,
            ),
            processing_source='asr_voice_recorder',
            success_status=200,
            reuse_duplicate=True,
        )
    except RequestEntityTooLarge:
        return jsonify({'error': 'File too large'}), 413


@api_v1_bp.route('/recordings/upload', methods=['POST'])
@require_scope('upload')
@login_required
def upload_recording():
    """
    Upload a recording and queue transcription (API).

    Multipart form-data fields:
      - file (required)
      - notes (optional)
      - title (optional)
      - meeting_date (optional, ISO 8601)
      - file_last_modified (optional, ms epoch)
      - language (optional)
      - min_speakers (optional)
      - max_speakers (optional)
      - hotwords (optional)
      - initial_prompt (optional)
      - transcription_model (optional, validated against admin-curated list)
      - prompt_variables (optional, JSON object of `{variable_name: value}`
        substituted into `{{name}}` placeholders in the resolved summary
        prompt at summarisation time)
      - folder_id (optional)
      - tag_ids[0], tag_ids[1], ... (optional)
      - tag_id (optional, legacy)
      - participants (optional, comma-separated, at most 500 characters)
      - external_refs (optional, JSON array of references)
      - idempotency_key (optional, 1 to 100 characters)
      - strict (optional, true: a tag or folder you cannot use is an error)
    """
    from src.api.recordings import ingest_uploaded_recording
    from src.models import Folder, GroupMembership, Tag
    from src.services import external_refs as xr
    form = request.form

    participants = form.get('participants')
    if participants is not None and len(participants) > 500:
        return jsonify({'error': 'participants must be at most 500 characters', 'code': 'invalid_parameter'}), 400
    clean_refs = []
    if form.get('external_refs'):
        try:
            clean_refs = xr.validate_list(json.loads(form['external_refs']))
        except ValueError as e:
            code = getattr(e, 'code', 'invalid_parameter')
            status = getattr(e, 'status', 400)
            return jsonify({'error': str(e) if isinstance(e, xr.RefError) else 'external_refs must be JSON',
                            'code': code}), status
    key = (form.get('idempotency_key') or '').strip() or None
    if key is not None and len(key) > 100:
        return jsonify({'error': 'idempotency_key must be 1 to 100 characters', 'code': 'invalid_parameter'}), 400
    if key:
        earlier = (Recording.query
                   .filter(Recording.user_id == current_user.id, Recording.upload_idempotency_key == key,
                           Recording.created_at >= datetime.utcnow() - timedelta(hours=24))
                   .order_by(Recording.id.desc()).first())
        if earlier is not None:
            data = earlier.to_dict(viewer_user=current_user)
            data['external_refs'] = [r.to_dict() for r in xr.refs_for(earlier.id, current_user.id)]
            data['idempotent_replay'] = True
            return jsonify(data), 200

    # Report tags and folders the upload cannot use (the pipeline drops them).
    def _usable(item):
        return item is not None and (item.user_id == current_user.id or (
            item.group_id and GroupMembership.query.filter_by(group_id=item.group_id,
                                                              user_id=current_user.id).first()))
    asked_tags = [form.get(f'tag_ids[{i}]') for i in range(100) if form.get(f'tag_ids[{i}]')] \
        or ([form.get('tag_id')] if form.get('tag_id') else [])
    ignored_tags = []
    for raw in asked_tags:
        tag = db.session.get(Tag, int(raw)) if str(raw).isdigit() else None
        if not _usable(tag):
            ignored_tags.append(int(raw) if str(raw).isdigit() else raw)
    ignored_folder = None
    if form.get('folder_id'):
        raw = form.get('folder_id')
        folder = db.session.get(Folder, int(raw)) if str(raw).isdigit() else None
        if not _usable(folder):
            ignored_folder = int(raw) if str(raw).isdigit() else raw
    if (form.get('strict') or '').lower() == 'true' and (ignored_tags or ignored_folder is not None):
        return jsonify({'error': 'A tag or folder in the upload is not one you can use',
                        'code': 'invalid_parameter',
                        'ignored': {'tag_ids': ignored_tags, 'folder_id': ignored_folder}}), 400

    def _prepare(recording):
        if participants is not None:
            recording.participants = participants.strip() or None
        if key:
            recording.upload_idempotency_key = key
        db.session.flush()
        for clean in clean_refs:
            xr.add(recording, current_user.id, clean)

    response = ingest_uploaded_recording(owner=current_user, uploaded_file=request.files.get('file'),
                                         form=form, prepare=_prepare)
    body, status = (response if isinstance(response, tuple) else (response, response.status_code))
    if status in (200, 201, 202):
        data = body.get_json() or {}
        if data.get('id'):
            data['external_refs'] = [r.to_dict() for r in xr.refs_for(data['id'], current_user.id)]
        data['ignored'] = {'tag_ids': ignored_tags, 'folder_id': ignored_folder}
        return jsonify(data), status
    return response
