# API Reference

Speakr provides a comprehensive REST API (v1) for automation tools, dashboard widgets, and custom integrations. This reference documents all available endpoints.

!!! tip "Interactive Documentation"
    Access the interactive Swagger UI documentation at `/api/v1/docs` on your Speakr instance. You can test endpoints directly from your browser.

## Base URL

All API v1 endpoints are prefixed with `/api/v1`:

```
https://your-speakr-instance.com/api/v1/
```

## Authentication

All endpoints require authentication. See [API Tokens](api-tokens.md) for details on creating and managing tokens.

=== "Bearer Token (Recommended)"
    ```bash
    curl -H "Authorization: Bearer YOUR_TOKEN" \
         https://speakr.example.com/api/v1/stats
    ```

=== "X-API-Token Header"
    ```bash
    curl -H "X-API-Token: YOUR_TOKEN" \
         https://speakr.example.com/api/v1/stats
    ```

=== "Query Parameter"
    ```bash
    curl "https://speakr.example.com/api/v1/stats?token=YOUR_TOKEN"
    ```

## OpenAPI Specification

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/docs` | Interactive Swagger UI |
| `GET /api/v1/openapi.json` | OpenAPI 3.0 specification |

<div style="max-width: 90%; margin: 1.5em auto;">
  <img src="../../assets/images/screenshots/api-swagger-ui.png" alt="Swagger UI Documentation" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
  <p style="text-align: center; margin-top: 0.5rem; font-style: italic; color: #666;">Interactive API documentation with Swagger UI at /api/v1/docs</p>
</div>

---

## Stats

Dashboard-compatible statistics endpoint, designed for integration with homepage widgets like [gethomepage.dev](https://gethomepage.dev/).

### Get Statistics

```http
GET /api/v1/stats
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `scope` | string | `user` | `user` for personal stats, `all` for global (admin only) |

**Response:**

```json
{
  "recordings": {
    "total": 150,
    "completed": 120,
    "processing": 5,
    "pending": 20,
    "failed": 5
  },
  "storage": {
    "used_bytes": 5368709120,
    "used_human": "5.0 GB"
  },
  "queue": {
    "jobs_queued": 3,
    "jobs_processing": 1
  },
  "tokens": {
    "used_this_month": 450000,
    "budget": 1000000,
    "percentage": 45.0
  },
  "activity": {
    "recordings_today": 3,
    "last_transcription": "2024-01-15T14:30:00Z"
  }
}
```

??? example "gethomepage.dev Widget Configuration"
    ```yaml
    - Speakr:
        widget:
          type: customapi
          url: https://speakr.example.com/api/v1/stats
          headers:
            Authorization: Bearer YOUR_TOKEN
          mappings:
            - field: recordings.completed
              label: Completed
            - field: storage.used_human
              label: Storage
            - field: tokens.percentage
              label: Token Usage
              format: percent
            - field: activity.recordings_today
              label: Today
    ```

---

## Recordings

### List Recordings

```http
GET /api/v1/recordings
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | 1 | Page number |
| `per_page` | integer | 25 | Items per page (max: 100) |
| `status` | string | `all` | Filter: `all`, `pending`, `processing`, `completed`, `failed` |
| `sort_by` | string | `created_at` | Sort field: `created_at`, `meeting_date`, `title`, `file_size` |
| `sort_order` | string | `desc` | Sort order: `asc`, `desc` |
| `date_from` | string | - | Filter from date (ISO format) |
| `date_to` | string | - | Filter to date (ISO format) |
| `tag_id` | integer | - | Filter by tag ID |
| `q` | string | - | Search query (title, participants) |
| `inbox` | boolean | - | Filter by inbox status |
| `starred` | boolean | - | Filter by starred status |

**Response:**

```json
{
  "recordings": [
    {
      "id": 123,
      "title": "Team Meeting",
      "status": "COMPLETED",
      "created_at": "2024-01-15T10:00:00Z",
      "meeting_date": "2024-01-15T09:00:00Z",
      "file_size": 15728640,
      "original_filename": "meeting.mp3",
      "participants": "Alice, Bob",
      "is_inbox": false,
      "is_highlighted": true,
      "audio_available": true,
      "has_transcription": true,
      "has_summary": true,
      "tags": [
        {"id": 1, "name": "Work", "color": "#3B82F6"}
      ]
    }
  ],
  "pagination": {
    "page": 1,
    "per_page": 25,
    "total": 150,
    "total_pages": 6,
    "has_next": true,
    "has_prev": false
  }
}
```

### Get Recording Details

```http
GET /api/v1/recordings/{id}
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `format` | string | `full` | `full` or `minimal` (excludes large text fields) |
| `include` | string | `transcription,summary,notes` | Comma-separated fields to include |

**Response:**

```json
{
  "id": 123,
  "title": "Team Meeting",
  "status": "COMPLETED",
  "participants": "Alice, Bob",
  "created_at": "2024-01-15T10:00:00Z",
  "meeting_date": "2024-01-15T09:00:00Z",
  "completed_at": "2024-01-15T10:05:00Z",
  "file_size": 15728640,
  "original_filename": "meeting.mp3",
  "mime_type": "audio/mpeg",
  "is_inbox": false,
  "is_highlighted": true,
  "audio_available": true,
  "processing_time_seconds": 45.2,
  "transcription": "Alice: Hello everyone...",
  "summary": "## Meeting Summary\n- Key point 1...",
  "notes": "Personal notes...",
  "tags": [{"id": 1, "name": "Work", "color": "#3B82F6"}]
}
```

!!! note "Transcript Formatting"
    The `transcription` field is automatically formatted using your default transcript template. Configure templates in Account Settings → Transcript Templates.

### Get Transcript

```http
GET /api/v1/recordings/{id}/transcript
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `format` | string | `json` | Output format: `json`, `text`, `srt`, `vtt` |

=== "JSON Format"
    ```json
    {
      "format": "json",
      "segments": [
        {
          "speaker": "Alice",
          "sentence": "Hello everyone",
          "start_time": 0.0,
          "end_time": 2.5
        }
      ]
    }
    ```

=== "Text Format"
    Uses your default transcript template:
    ```json
    {
      "format": "text",
      "content": "Alice: Hello everyone\n\nBob: Hi Alice..."
    }
    ```

=== "SRT Format"
    ```json
    {
      "format": "srt",
      "content": "1\n00:00:00,000 --> 00:00:02,500\nHello everyone\n\n2\n..."
    }
    ```

=== "VTT Format"
    ```json
    {
      "format": "vtt",
      "content": "WEBVTT\n\n00:00:00.000 --> 00:00:02.500\n<v Alice>Hello everyone\n\n..."
    }
    ```

### Get Summary

```http
GET /api/v1/recordings/{id}/summary
```

**Response:**

```json
{
  "summary": "## Meeting Summary\n\n### Key Points\n- Point 1...",
  "has_summary": true
}
```

### Get Notes

```http
GET /api/v1/recordings/{id}/notes
```

**Response:**

```json
{
  "notes": "My personal notes about this meeting...",
  "has_notes": true
}
```

### Get Processing Status

```http
GET /api/v1/recordings/{id}/status
```

**Response:**

```json
{
  "id": 123,
  "status": "PROCESSING",
  "queue_position": 2,
  "error_message": null,
  "completed_at": null
}
```

**Status Values:**

| Status | Description |
|--------|-------------|
| `PENDING` | Waiting in queue |
| `PROCESSING` | Transcription in progress |
| `SUMMARIZING` | Summary generation in progress |
| `COMPLETED` | Processing finished successfully |
| `FAILED` | Processing failed (check `error_message`) |

### Update Recording

```http
PATCH /api/v1/recordings/{id}
```

**Request Body:**

```json
{
  "title": "Updated Title",
  "participants": "Alice, Bob, Charlie",
  "notes": "Updated notes...",
  "summary": "Updated summary...",
  "meeting_date": "2024-01-15T09:00:00Z",
  "is_inbox": false,
  "is_highlighted": true
}
```

All fields are optional.

### Replace Notes

```http
PUT /api/v1/recordings/{id}/notes
```

**Request Body:**

```json
{
  "notes": "New notes content..."
}
```

### Replace Summary

```http
PUT /api/v1/recordings/{id}/summary
```

**Request Body:**

```json
{
  "summary": "## New Summary\n- Point 1..."
}
```

### Delete Recording

```http
DELETE /api/v1/recordings/{id}
```

**Response:**

```json
{
  "success": true,
  "message": "Recording deleted"
}
```

---

## Tags

### List Tags

```http
GET /api/v1/tags
```

Returns both personal tags and group tags you have access to.

**Response:**

```json
{
  "tags": [
    {
      "id": 1,
      "name": "Work Meetings",
      "color": "#3B82F6",
      "is_group_tag": false,
      "group_id": null,
      "custom_prompt": "Focus on action items...",
      "default_language": "en",
      "default_min_speakers": 2,
      "default_max_speakers": 10,
      "protect_from_deletion": false,
      "can_edit": true
    }
  ]
}
```

### Create Tag

```http
POST /api/v1/tags
```

**Request Body:**

```json
{
  "name": "Interviews",
  "color": "#10B981",
  "custom_prompt": "Extract candidate qualifications...",
  "default_language": "en",
  "default_min_speakers": 2,
  "default_max_speakers": 3,
  "group_id": null
}
```

### Update Tag

```http
PUT /api/v1/tags/{id}
```

**Request Body:**

```json
{
  "name": "Updated Name",
  "color": "#EF4444",
  "custom_prompt": "New prompt..."
}
```

### Delete Tag

```http
DELETE /api/v1/tags/{id}
```

### Add Tags to Recording

```http
POST /api/v1/recordings/{id}/tags
```

**Request Body:**

```json
{
  "tag_ids": [1, 2, 3]
}
```

### Remove Tag from Recording

```http
DELETE /api/v1/recordings/{id}/tags/{tag_id}
```

---

## Speakers

### List Speakers

```http
GET /api/v1/speakers
```

**Response:**

```json
{
  "speakers": [
    {
      "id": 1,
      "name": "John Doe",
      "use_count": 45,
      "last_used": "2024-01-15T14:30:00Z",
      "confidence_score": 0.87,
      "has_voice_profile": true
    }
  ]
}
```

### Create Speaker

```http
POST /api/v1/speakers
```

**Request Body:**

```json
{
  "name": "Jane Smith"
}
```

### Update Speaker

```http
PUT /api/v1/speakers/{id}
```

Updates the speaker name and cascades changes to all recordings.

**Request Body:**

```json
{
  "name": "Jane Doe"
}
```

### Delete Speaker

```http
DELETE /api/v1/speakers/{id}
```

### Get Recording Speakers

```http
GET /api/v1/recordings/{id}/speakers
```

Returns speakers in the recording with voice-based identification suggestions.

**Response:**

```json
{
  "speakers": [
    {
      "label": "SPEAKER_00",
      "identified_name": "John Doe",
      "speaker_id": 1,
      "segment_count": 23
    }
  ],
  "suggestions": {
    "SPEAKER_01": [
      {"speaker_id": 2, "name": "Jane Smith", "similarity": 89.5}
    ]
  }
}
```

---

## Processing Operations

### Queue Transcription

```http
POST /api/v1/recordings/{id}/transcribe
```

**Request Body:**

```json
{
  "language": "en",
  "min_speakers": 1,
  "max_speakers": 5
}
```

All parameters are optional.

**Response:**

```json
{
  "success": true,
  "job_id": "abc123",
  "status": "QUEUED",
  "message": "Transcription queued"
}
```

### Queue Summarization

```http
POST /api/v1/recordings/{id}/summarize
```

**Request Body:**

```json
{
  "custom_prompt": "Focus on technical decisions and action items only"
}
```

The custom prompt overrides the recording's tag prompts and user defaults.

---

## Chat

### Chat with Recording

```http
POST /api/v1/recordings/{id}/chat
```

Ask questions about a recording's content using AI.

**Request Body:**

```json
{
  "message": "What were the main action items discussed?",
  "conversation_history": [
    {"role": "user", "content": "Who attended?"},
    {"role": "assistant", "content": "John and Jane attended..."}
  ]
}
```

**Response:**

```json
{
  "response": "The main action items were:\n1. Complete the report by Friday\n2. Schedule follow-up meeting...",
  "sources": []
}
```

---

## Events

### Get Calendar Events

```http
GET /api/v1/recordings/{id}/events
```

Returns calendar events extracted from the recording.

**Response:**

```json
{
  "events": [
    {
      "id": 1,
      "title": "Follow-up Meeting",
      "start_datetime": "2024-01-22T10:00:00Z",
      "end_datetime": "2024-01-22T11:00:00Z",
      "description": "Discuss project progress",
      "location": "Conference Room A"
    }
  ]
}
```

### Download Events as ICS

```http
GET /api/v1/recordings/{id}/events/ics
```

Returns an ICS file containing all events from the recording.

---

## Audio

### Download Audio

```http
GET /api/v1/recordings/{id}/audio
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `download` | boolean | `false` | `true` to force download, `false` to stream |

---

## Batch Operations

### Batch Update Recordings

```http
PATCH /api/v1/recordings/batch
```

**Request Body:**

```json
{
  "recording_ids": [1, 2, 3],
  "updates": {
    "is_inbox": false,
    "is_highlighted": true,
    "add_tag_ids": [5],
    "remove_tag_ids": [2]
  }
}
```

**Response:**

```json
{
  "success": true,
  "updated": 3,
  "failed": 0,
  "results": [
    {"id": 1, "success": true},
    {"id": 2, "success": true},
    {"id": 3, "success": true}
  ]
}
```

### Batch Delete Recordings

```http
DELETE /api/v1/recordings/batch
```

**Request Body:**

```json
{
  "recording_ids": [1, 2, 3]
}
```

### Batch Queue Transcriptions

```http
POST /api/v1/recordings/batch/transcribe
```

**Request Body:**

```json
{
  "recording_ids": [1, 2, 3]
}
```

---

## Error Responses

All endpoints return consistent error responses:

```json
{
  "error": "Error message description"
}
```

**Common HTTP Status Codes:**

| Code | Description |
|------|-------------|
| `200` | Success |
| `201` | Created |
| `400` | Bad Request - Invalid parameters |
| `401` | Unauthorized - Invalid or missing token |
| `403` | Forbidden - No permission for this resource |
| `404` | Not Found - Resource doesn't exist |
| `500` | Internal Server Error |

---

## Rate Limits

API endpoints are rate-limited to prevent abuse:

| Endpoint Type | Limit |
|---------------|-------|
| Stats | 60 requests/minute |
| GET endpoints | 100 requests/minute |
| PATCH/DELETE | 30 requests/minute |
| Processing operations | 10 requests/minute |
| Batch operations | 10 requests/minute |

---

## Integration Examples

### Python SDK Pattern

```python
import requests

class SpeakrAPI:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {token}'

    def get_stats(self):
        return self.session.get(f'{self.base_url}/api/v1/stats').json()

    def list_recordings(self, status='all', page=1):
        return self.session.get(
            f'{self.base_url}/api/v1/recordings',
            params={'status': status, 'page': page}
        ).json()

    def get_transcript(self, recording_id, format='text'):
        return self.session.get(
            f'{self.base_url}/api/v1/recordings/{recording_id}/transcript',
            params={'format': format}
        ).json()

# Usage
api = SpeakrAPI('https://speakr.example.com', 'YOUR_TOKEN')
stats = api.get_stats()
print(f"Total recordings: {stats['recordings']['total']}")
```

### n8n Workflow

1. Use **HTTP Request** node
2. Set **Method** to `GET`
3. Set **URL** to `https://your-instance/api/v1/recordings`
4. In **Authentication**, select **Header Auth**
5. Add header: `Authorization` = `Bearer YOUR_TOKEN`

### Zapier Integration

Use the **Webhooks by Zapier** app with:

- **Trigger**: Custom webhook
- **Action**: GET request to `/api/v1/recordings`
- **Headers**: `Authorization: Bearer YOUR_TOKEN`

---

Next: Learn about [API Tokens](api-tokens.md) for authentication setup.
