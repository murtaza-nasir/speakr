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
import json
from datetime import datetime, date, timedelta
from typing import Optional

from flask import Blueprint, jsonify, request, current_app, send_file
from flask_login import login_required, current_user
from sqlalchemy import func, extract, or_, and_

from src.database import db
from src.models import Recording, User, Tag, RecordingTag, Speaker, Event
from src.models.processing_job import ProcessingJob
from src.models.token_usage import TokenUsage
from src.services.token_tracking import TokenTracker
from src.file_exporter import format_transcription_with_template

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
        "title": "Speakr API v1",
        "description": "REST API for Speakr - Audio transcription and note-taking application.\n\n## Authentication\nAll endpoints require token authentication via one of:\n- `Authorization: Bearer <token>`\n- `X-API-Token: <token>`\n- `API-Token: <token>`\n- `?token=<token>` query parameter\n\nGenerate tokens in Settings > API Tokens.",
        "version": "1.0.0"
    },
    "servers": [{"url": "/api/v1", "description": "API v1"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
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
                    "participants": {"type": "string"},
                    "is_inbox": {"type": "boolean"},
                    "is_highlighted": {"type": "boolean"},
                    "tags": {"type": "array", "items": {"$ref": "#/components/schemas/Tag"}}
                }
            },
            "Tag": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "custom_prompt": {"type": "string"},
                    "default_language": {"type": "string"},
                    "default_min_speakers": {"type": "integer"},
                    "default_max_speakers": {"type": "integer"}
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
                "properties": {"error": {"type": "string"}}
            }
        }
    },
    "security": [{"bearerAuth": []}, {"apiKeyHeader": []}, {"apiKeyQuery": []}],
    "paths": {
        "/stats": {
            "get": {
                "tags": ["Stats"],
                "summary": "Get system statistics",
                "description": "Returns stats compatible with gethomepage.dev widgets",
                "parameters": [{"name": "scope", "in": "query", "schema": {"type": "string", "enum": ["user", "all"], "default": "user"}, "description": "user=personal stats, all=global (admin only)"}],
                "responses": {"200": {"description": "Stats object"}}
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
                    {"name": "sort_by", "in": "query", "schema": {"type": "string", "enum": ["created_at", "meeting_date", "title", "file_size"]}},
                    {"name": "sort_order", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}},
                    {"name": "tag_id", "in": "query", "schema": {"type": "integer"}},
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
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"title": {"type": "string"}, "participants": {"type": "string"}, "notes": {"type": "string"}, "summary": {"type": "string"}, "meeting_date": {"type": "string"}, "is_inbox": {"type": "boolean"}, "is_highlighted": {"type": "boolean"}}}}}},
                "responses": {"200": {"description": "Updated recording"}}
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
        "/recordings/{id}/status": {
            "get": {"tags": ["Recordings"], "summary": "Get processing status", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Status with queue position"}}}
        },
        "/recordings/{id}/transcribe": {
            "post": {"tags": ["Processing"], "summary": "Queue transcription", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"language": {"type": "string"}, "min_speakers": {"type": "integer"}, "max_speakers": {"type": "integer"}}}}}}, "responses": {"200": {"description": "Job queued"}}}
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
        "/recordings/batch": {
            "patch": {"tags": ["Batch"], "summary": "Batch update recordings", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["recording_ids", "updates"], "properties": {"recording_ids": {"type": "array", "items": {"type": "integer"}}, "updates": {"type": "object"}}}}}}, "responses": {"200": {"description": "Batch results"}}},
            "delete": {"tags": ["Batch"], "summary": "Batch delete recordings", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["recording_ids"], "properties": {"recording_ids": {"type": "array", "items": {"type": "integer"}}}}}}}, "responses": {"200": {"description": "Batch results"}}}
        },
        "/recordings/batch/transcribe": {
            "post": {"tags": ["Batch"], "summary": "Batch queue transcriptions", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["recording_ids"], "properties": {"recording_ids": {"type": "array", "items": {"type": "integer"}}}}}}}, "responses": {"200": {"description": "Batch results"}}}
        },
        "/tags": {
            "get": {"tags": ["Tags"], "summary": "List tags", "responses": {"200": {"description": "List of tags"}}},
            "post": {"tags": ["Tags"], "summary": "Create tag", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "custom_prompt": {"type": "string"}, "default_language": {"type": "string"}, "default_min_speakers": {"type": "integer"}, "default_max_speakers": {"type": "integer"}}}}}}, "responses": {"201": {"description": "Tag created"}}}
        },
        "/tags/{id}": {
            "put": {"tags": ["Tags"], "summary": "Update tag", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "custom_prompt": {"type": "string"}}}}}}, "responses": {"200": {"description": "Tag updated"}}},
            "delete": {"tags": ["Tags"], "summary": "Delete tag", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Tag deleted"}}}
        },
        "/speakers": {
            "get": {"tags": ["Speakers"], "summary": "List speakers", "responses": {"200": {"description": "List of speakers"}}},
            "post": {"tags": ["Speakers"], "summary": "Create speaker", "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}}}, "responses": {"201": {"description": "Speaker created"}}}
        },
        "/speakers/{id}": {
            "put": {"tags": ["Speakers"], "summary": "Update speaker", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}}}, "responses": {"200": {"description": "Speaker updated"}}},
            "delete": {"tags": ["Speakers"], "summary": "Delete speaker", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Speaker deleted"}}}
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
        {"name": "Speakers", "description": "Speaker management"},
        {"name": "Batch", "description": "Batch operations"}
    ]
}


@api_v1_bp.route('/openapi.json', methods=['GET'])
def get_openapi_spec():
    """Return OpenAPI specification."""
    return jsonify(OPENAPI_SPEC)


@api_v1_bp.route('/docs', methods=['GET'])
def get_docs():
    """Serve Swagger UI documentation."""
    from flask import Response
    html = '''<!DOCTYPE html>
<html>
<head>
    <title>Speakr API v1 Documentation</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({
            url: "/api/v1/openapi.json",
            dom_id: '#swagger-ui',
            presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
            layout: "BaseLayout",
            persistAuthorization: true
        });
    </script>
</body>
</html>'''
    return Response(html, mimetype='text/html')


# =============================================================================
# Stats Endpoint (Homepage Widget Compatible)
# =============================================================================

@api_v1_bp.route('/stats', methods=['GET'])
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

    # Storage calculation
    storage_query = db.session.query(func.sum(Recording.file_size)).filter(recording_filter)
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
# Recordings List with Enhanced Filtering
# =============================================================================

@api_v1_bp.route('/recordings', methods=['GET'])
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
    search_query = request.args.get('q', '').strip()
    inbox_filter = request.args.get('inbox')
    starred_filter = request.args.get('starred')

    # Base query - user's recordings
    query = Recording.query.filter(Recording.user_id == current_user.id)

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
    if date_from:
        try:
            from_date = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
            query = query.filter(Recording.created_at >= from_date)
        except ValueError:
            pass

    if date_to:
        try:
            to_date = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
            query = query.filter(Recording.created_at <= to_date)
        except ValueError:
            pass

    # Tag filter
    if tag_id:
        query = query.join(RecordingTag).filter(RecordingTag.tag_id == tag_id)

    # Search filter
    if search_query:
        search_pattern = f'%{search_query}%'
        query = query.filter(
            or_(
                Recording.title.ilike(search_pattern),
                Recording.participants.ilike(search_pattern)
            )
        )

    # Inbox filter
    if inbox_filter is not None:
        is_inbox = inbox_filter.lower() == 'true'
        query = query.filter(Recording.is_inbox == is_inbox)

    # Starred filter
    if starred_filter is not None:
        is_starred = starred_filter.lower() == 'true'
        query = query.filter(Recording.is_highlighted == is_starred)

    # Sorting
    sort_columns = {
        'created_at': Recording.created_at,
        'meeting_date': Recording.meeting_date,
        'title': Recording.title,
        'file_size': Recording.file_size,
        'status': Recording.status
    }
    sort_column = sort_columns.get(sort_by, Recording.created_at)

    if sort_order == 'asc':
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())

    # Pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Build response
    recordings = []
    for r in pagination.items:
        recordings.append({
            'id': r.id,
            'title': r.title,
            'status': r.status,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'meeting_date': r.meeting_date.isoformat() if r.meeting_date else None,
            'file_size': r.file_size,
            'original_filename': r.original_filename,
            'participants': r.participants,
            'is_inbox': r.is_inbox,
            'is_highlighted': r.is_highlighted,
            'audio_available': r.audio_deleted_at is None,
            'has_transcription': bool(r.transcription),
            'has_summary': bool(r.summary),
            'error_message': r.error_message if r.status == 'FAILED' else None,
            'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in r.tags]
        })

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

@api_v1_bp.route('/recordings/<int:recording_id>', methods=['GET'])
@login_required
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
        'audio_available': recording.audio_deleted_at is None,
        'processing_time_seconds': recording.processing_time_seconds,
        'error_message': recording.error_message if recording.status == 'FAILED' else None,
        'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in recording.tags]
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
            response['notes'] = recording.notes

    return jsonify(response)


# =============================================================================
# Recording Transcript/Summary/Notes Individual Endpoints
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/transcript', methods=['GET'])
@login_required
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
        try:
            segments = json.loads(recording.transcription)
            return jsonify({
                'format': 'json',
                'segments': segments
            })
        except json.JSONDecodeError:
            return jsonify({
                'format': 'json',
                'segments': [],
                'raw': recording.transcription
            })

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
@login_required
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
@login_required
def get_notes(recording_id):
    """Get notes markdown."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    return jsonify({
        'notes': recording.notes,
        'has_notes': bool(recording.notes)
    })


# =============================================================================
# Recording Update Operations
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>', methods=['PATCH'])
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
        "is_highlighted": true
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

    # Update fields if provided
    if 'title' in data:
        recording.title = data['title']
    if 'participants' in data:
        recording.participants = data['participants']
    if 'notes' in data:
        recording.notes = data['notes']
    if 'summary' in data:
        recording.summary = data['summary']
    if 'meeting_date' in data:
        try:
            if data['meeting_date']:
                recording.meeting_date = datetime.fromisoformat(data['meeting_date'].replace('Z', '+00:00'))
            else:
                recording.meeting_date = None
        except ValueError:
            return jsonify({'error': 'Invalid meeting_date format'}), 400
    if 'is_inbox' in data:
        recording.is_inbox = bool(data['is_inbox'])
    if 'is_highlighted' in data:
        recording.is_highlighted = bool(data['is_highlighted'])

    db.session.commit()

    return jsonify({
        'success': True,
        'recording': {
            'id': recording.id,
            'title': recording.title,
            'participants': recording.participants,
            'notes': recording.notes,
            'summary': recording.summary,
            'meeting_date': recording.meeting_date.isoformat() if recording.meeting_date else None,
            'is_inbox': recording.is_inbox,
            'is_highlighted': recording.is_highlighted
        }
    })


@api_v1_bp.route('/recordings/<int:recording_id>/notes', methods=['PUT'])
@login_required
def replace_notes(recording_id):
    """Replace notes entirely."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    if not data or 'notes' not in data:
        return jsonify({'error': 'notes field required'}), 400

    recording.notes = data['notes']
    db.session.commit()

    return jsonify({'success': True, 'notes': recording.notes})


@api_v1_bp.route('/recordings/<int:recording_id>/summary', methods=['PUT'])
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

    # Delete associated files
    if recording.audio_path:
        try:
            audio_path = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), recording.audio_path)
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass  # Continue with DB deletion even if file deletion fails

    # Delete from database
    db.session.delete(recording)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Recording deleted'})


# =============================================================================
# Recording Status
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/status', methods=['GET'])
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
# Tag Management
# =============================================================================

@api_v1_bp.route('/tags', methods=['GET'])
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
            'default_language': tag.default_language,
            'default_min_speakers': tag.default_min_speakers,
            'default_max_speakers': tag.default_max_speakers,
            'protect_from_deletion': tag.protect_from_deletion,
            'can_edit': (user_role == 'admin')
        })

    return jsonify({'tags': result})


@api_v1_bp.route('/tags', methods=['POST'])
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
        'default_language': tag.default_language,
        'default_min_speakers': tag.default_min_speakers,
        'default_max_speakers': tag.default_max_speakers,
        'protect_from_deletion': tag.protect_from_deletion
    }), 201


@api_v1_bp.route('/tags/<int:tag_id>', methods=['PUT'])
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
        'default_language': tag.default_language,
        'default_min_speakers': tag.default_min_speakers,
        'default_max_speakers': tag.default_max_speakers,
        'protect_from_deletion': tag.protect_from_deletion
    }})


@api_v1_bp.route('/tags/<int:tag_id>', methods=['DELETE'])
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


@api_v1_bp.route('/recordings/<int:recording_id>/tags', methods=['POST'])
@login_required
def add_tags_to_recording(recording_id):
    """Add tag(s) to a recording."""
    from src.models.organization import GroupMembership

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    data = request.get_json()
    tag_ids = data.get('tag_ids', [])
    if not tag_ids:
        # Support single tag_id for backward compatibility
        tag_id = data.get('tag_id')
        if tag_id:
            tag_ids = [tag_id]
        else:
            return jsonify({'error': 'tag_ids or tag_id required'}), 400

    added_tags = []
    errors = []

    for tag_id in tag_ids:
        tag = db.session.get(Tag, tag_id)
        if not tag:
            errors.append(f'Tag {tag_id} not found')
            continue

        # Check permission for this tag
        if tag.group_id:
            membership = GroupMembership.query.filter_by(
                group_id=tag.group_id,
                user_id=current_user.id
            ).first()
            if not membership:
                errors.append(f'No access to tag {tag_id}')
                continue
        else:
            if tag.user_id != current_user.id:
                errors.append(f'No access to tag {tag_id}')
                continue

        # Check if already exists
        existing = RecordingTag.query.filter_by(
            recording_id=recording_id,
            tag_id=tag_id
        ).first()
        if existing:
            continue  # Skip, already added

        # Get next order position
        max_order = db.session.query(func.max(RecordingTag.order)).filter_by(
            recording_id=recording_id
        ).scalar() or 0

        recording_tag = RecordingTag(
            recording_id=recording_id,
            tag_id=tag_id,
            order=max_order + 1
        )
        db.session.add(recording_tag)
        added_tags.append({'id': tag.id, 'name': tag.name})

    db.session.commit()

    return jsonify({
        'success': True,
        'added_tags': added_tags,
        'errors': errors if errors else None
    })


@api_v1_bp.route('/recordings/<int:recording_id>/tags/<int:tag_id>', methods=['DELETE'])
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
# Speaker Management
# =============================================================================

@api_v1_bp.route('/speakers', methods=['GET'])
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
@login_required
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

    # Get voice-based suggestions
    suggestions = {}
    if recording.speaker_embeddings:
        try:
            matches = find_matching_speakers(current_user.id, recording.speaker_embeddings)
            for label, speaker_matches in matches.items():
                suggestions[label] = [{
                    'speaker_id': m['speaker_id'],
                    'name': m['name'],
                    'similarity': round(m['similarity'] * 100, 1)
                } for m in speaker_matches[:3]]
        except Exception as e:
            current_app.logger.error(f"Error getting speaker suggestions: {e}")

    return jsonify({
        'speakers': speakers_in_recording,
        'suggestions': suggestions
    })


# =============================================================================
# Processing Operations
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/transcribe', methods=['POST'])
@login_required
def start_transcription(recording_id):
    """Queue transcription for a recording."""
    from src.services.job_queue import job_queue

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    # Check if audio is available
    if recording.audio_deleted_at:
        return jsonify({'error': 'Audio has been deleted'}), 400

    data = request.get_json() or {}

    params = {
        'language': data.get('language'),
        'min_speakers': data.get('min_speakers'),
        'max_speakers': data.get('max_speakers')
    }

    # Queue the job
    job_id = job_queue.enqueue(
        user_id=current_user.id,
        recording_id=recording_id,
        job_type='reprocess_transcription',
        params={k: v for k, v in params.items() if v is not None}
    )

    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'QUEUED',
        'message': 'Transcription queued'
    })


@api_v1_bp.route('/recordings/<int:recording_id>/summarize', methods=['POST'])
@login_required
def start_summarization(recording_id):
    """Queue summarization for a recording with optional custom prompt."""
    from src.services.job_queue import job_queue

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user, require_edit=True):
        return jsonify({'error': 'Permission denied'}), 403

    # Check if transcription exists
    if not recording.transcription:
        return jsonify({'error': 'No transcription available - transcribe first'}), 400

    data = request.get_json() or {}

    params = {
        'custom_prompt': data.get('custom_prompt'),
        'user_id': current_user.id
    }

    # Queue the job
    job_id = job_queue.enqueue(
        user_id=current_user.id,
        recording_id=recording_id,
        job_type='reprocess_summary',
        params={k: v for k, v in params.items() if v is not None}
    )

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
@login_required
def chat_with_recording(recording_id):
    """Chat about a recording's content."""
    from src.services.llm import chat_client, call_chat_completion
    from src.tasks.processing import format_transcription_for_llm
    from src.models.system_settings import SystemSetting

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

    # Format transcription
    formatted_transcription = format_transcription_for_llm(recording.transcription)

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

    # Build messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = call_chat_completion(messages, user_id=current_user.id)

        return jsonify({
            'response': response,
            'sources': []  # Could be enhanced to extract relevant segments
        })
    except Exception as e:
        current_app.logger.error(f"Chat error: {e}")
        return jsonify({'error': 'Chat failed'}), 500


# =============================================================================
# Calendar Events
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/events', methods=['GET'])
@login_required
def get_recording_events(recording_id):
    """Get calendar events extracted from a recording."""
    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    if not has_recording_access(recording, current_user):
        return jsonify({'error': 'Permission denied'}), 403

    events = Event.query.filter_by(recording_id=recording_id).all()

    return jsonify({
        'events': [{
            'id': e.id,
            'title': e.title,
            'start_datetime': e.start_datetime.isoformat() if e.start_datetime else None,
            'end_datetime': e.end_datetime.isoformat() if e.end_datetime else None,
            'description': e.description,
            'location': e.location
        } for e in events]
    })


@api_v1_bp.route('/recordings/<int:recording_id>/events/ics', methods=['GET'])
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

    # Generate combined ICS
    ics_lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Speakr//Events//EN']

    for event in events:
        ics_lines.append('BEGIN:VEVENT')
        ics_lines.append(f'UID:{event.id}@speakr')
        ics_lines.append(f'SUMMARY:{event.title}')
        if event.start_datetime:
            ics_lines.append(f'DTSTART:{event.start_datetime.strftime("%Y%m%dT%H%M%S")}')
        if event.end_datetime:
            ics_lines.append(f'DTEND:{event.end_datetime.strftime("%Y%m%dT%H%M%S")}')
        if event.description:
            ics_lines.append(f'DESCRIPTION:{event.description}')
        if event.location:
            ics_lines.append(f'LOCATION:{event.location}')
        ics_lines.append('END:VEVENT')

    ics_lines.append('END:VCALENDAR')

    from flask import Response
    return Response(
        '\r\n'.join(ics_lines),
        mimetype='text/calendar',
        headers={'Content-Disposition': f'attachment; filename=events-{recording_id}.ics'}
    )


# =============================================================================
# Audio Download
# =============================================================================

@api_v1_bp.route('/recordings/<int:recording_id>/audio', methods=['GET'])
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

    audio_path = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), recording.audio_path)
    if not os.path.exists(audio_path):
        return jsonify({'error': 'Audio file not found'}), 404

    download = request.args.get('download', 'false').lower() == 'true'

    return send_file(
        audio_path,
        mimetype=recording.mime_type or 'audio/mpeg',
        as_attachment=download,
        download_name=recording.original_filename or f'recording-{recording_id}.mp3'
    )


# =============================================================================
# Batch Operations
# =============================================================================

@api_v1_bp.route('/recordings/batch', methods=['PATCH'])
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

            # Handle tag additions
            if 'add_tag_ids' in updates:
                for tag_id in updates['add_tag_ids']:
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

            # Handle tag removals
            if 'remove_tag_ids' in updates:
                for tag_id in updates['remove_tag_ids']:
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
            # Delete audio file
            if recording.audio_path:
                audio_path = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), recording.audio_path)
                if os.path.exists(audio_path):
                    os.remove(audio_path)

            db.session.delete(recording)
            results.append({'id': recording_id, 'success': True})
        except Exception as e:
            results.append({'id': recording_id, 'success': False, 'error': str(e)})

    db.session.commit()

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'success': True,
        'deleted': success_count,
        'failed': len(results) - success_count,
        'results': results
    })


@api_v1_bp.route('/recordings/batch/transcribe', methods=['POST'])
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

        if recording.audio_deleted_at:
            results.append({'id': recording_id, 'success': False, 'error': 'Audio deleted'})
            continue

        try:
            job_id = job_queue.enqueue(
                user_id=current_user.id,
                recording_id=recording_id,
                job_type='reprocess_transcription',
                params={}
            )
            results.append({'id': recording_id, 'success': True, 'job_id': job_id})
        except Exception as e:
            results.append({'id': recording_id, 'success': False, 'error': str(e)})

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'success': True,
        'queued': success_count,
        'failed': len(results) - success_count,
        'results': results
    })
