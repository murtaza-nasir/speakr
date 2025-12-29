# Job Queue & Progress Tracking System

This document describes the backend job queue system and frontend unified progress tracking implementation. This architecture handles audio processing jobs (transcription, summarization) with fair scheduling across users and provides real-time progress feedback.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           FRONTEND                                       │
│  ┌─────────────────┐    ┌──────────────────┐    ┌───────────────────┐  │
│  │  Upload Queue   │───▶│ Unified Progress │◀───│  Job Queue API    │  │
│  │  (client-side)  │    │    Items         │    │  (polling)        │  │
│  └─────────────────┘    └──────────────────┘    └───────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           BACKEND                                        │
│  ┌─────────────────┐    ┌──────────────────┐    ┌───────────────────┐  │
│  │  Upload API     │───▶│ ProcessingJob    │◀───│  Job Queue        │  │
│  │  /upload        │    │    Model         │    │  Service          │  │
│  └─────────────────┘    └──────────────────┘    └───────────────────┘  │
│                                │                         │              │
│                                ▼                         ▼              │
│                         ┌──────────────────┐    ┌───────────────────┐  │
│                         │    SQLite DB     │    │  Worker Thread    │  │
│                         │  processing_job  │    │  (background)     │  │
│                         └──────────────────┘    └───────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Backend Components

### 1. ProcessingJob Model (`src/models/processing_job.py`)

Database model for persistent job tracking:

```python
class ProcessingJob(db.Model):
    __tablename__ = 'processing_job'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)

    # Job type: transcribe, summarize, reprocess_transcription, reprocess_summary
    job_type = db.Column(db.String(50), nullable=False)

    # Status: queued, processing, completed, failed
    status = db.Column(db.String(20), default='queued', nullable=False)

    # JSON blob for job-specific parameters
    params = db.Column(db.Text, nullable=True)

    # Error tracking
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0, nullable=False)

    # Track if this is a new upload (vs reprocessing) - for cleanup on failure
    is_new_upload = db.Column(db.Boolean, default=False, nullable=False)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
```

**Key Fields:**
- `is_new_upload`: When `True` and job fails permanently, the associated recording and audio file are deleted. When `False` (reprocessing), only the recording status is set to FAILED.
- `status`: Job lifecycle state
- `retry_count`: Jobs retry up to 3 times before permanent failure

### 2. Job Queue Service (`src/services/job_queue.py`)

Background worker that processes jobs fairly across users.

**Key Features:**

#### Fair Scheduling (Round-Robin per User)
```python
def _claim_next_job(self, job_types, queue_name):
    # Find the next user who has waiting jobs
    # Round-robin through users to ensure fairness
    users_with_jobs = db.session.query(ProcessingJob.user_id).filter(
        ProcessingJob.status == 'queued',
        ProcessingJob.job_type.in_(job_types)
    ).distinct().all()

    # Pick next user in rotation
    user_id = self._get_next_user_in_rotation(users_with_jobs)

    # Claim oldest job for that user
    job = ProcessingJob.query.filter(
        ProcessingJob.user_id == user_id,
        ProcessingJob.status == 'queued'
    ).order_by(ProcessingJob.created_at.asc()).first()
```

#### Race Condition Prevention
SQLite doesn't support `FOR UPDATE SKIP LOCKED`, so we use an atomic UPDATE with a WHERE clause that checks the status is still 'queued'. This ensures only one worker can claim a job, even with multiple processes:

```python
def _claim_next_job(self, job_types, queue_name):
    # Find candidate job
    candidate_job = ProcessingJob.query.filter(
        ProcessingJob.status == 'queued'
    ).first()

    if candidate_job:
        # Atomic claim - only succeeds if status is still 'queued'
        result = db.session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == candidate_job.id,
                ProcessingJob.status == 'queued'  # Critical check
            )
            .values(status='processing', started_at=datetime.utcnow())
        )

        if result.rowcount == 0:
            # Job was already claimed by another worker
            return None

        db.session.commit()
        return candidate_job
```

This prevents the race condition where multiple workers could claim the same job when running as separate processes (fixed in v0.7.1).

#### Session Management
Jobs are claimed in one database session context, then processed in another to avoid detached object issues:

```python
def _process_job(self, job):
    # Save job attributes before context ends
    job_id = job.id
    job_type = job.job_type
    recording_id = job.recording_id
    is_new_upload = job.is_new_upload

    with self._app_context():
        # Re-fetch job in new session
        job = db.session.get(ProcessingJob, job_id)
        # ... process job
```

#### Failed Upload Cleanup
When a new upload fails permanently:

```python
if is_new_upload and recording:
    # Delete audio file
    if recording.audio_path and os.path.exists(recording.audio_path):
        os.remove(recording.audio_path)
    # Delete all processing jobs for this recording
    ProcessingJob.query.filter_by(recording_id=recording_id).delete()
    # Delete the recording
    db.session.delete(recording)
```

### 3. Job Queue API (`src/api/recordings.py`)

#### Get Job Queue Status
`GET /api/recordings/job-queue-status`

Returns all jobs for the current user (active + completed/failed from last hour):

```json
{
  "jobs": [
    {
      "id": 123,
      "recording_id": 456,
      "recording_title": "Meeting Notes",
      "job_status": "processing",
      "job_type": "transcribe",
      "queue_type": "transcription",
      "position": null,
      "is_new_upload": true,
      "error_message": null,
      "created_at": "2025-11-27T01:00:00",
      "started_at": "2025-11-27T01:00:05",
      "completed_at": null
    }
  ]
}
```

**Queue Types:**
- `transcription`: transcribe, reprocess_transcription
- `summary`: summarize, reprocess_summary

#### Retry Failed Job
`POST /api/recordings/jobs/<id>/retry`

Resets job status to `queued` for another attempt.

#### Delete Job
`DELETE /api/recordings/jobs/<id>`

Deletes a job. If it's a failed new upload, also deletes the recording and audio file.

#### Clear Completed Jobs
`POST /api/recordings/jobs/clear-completed`

Removes all completed jobs for the current user.

### 4. Recording Deletion Cascade

When deleting a recording, associated processing jobs must be deleted first due to the NOT NULL constraint on `recording_id`:

```python
# In delete_recording(), delete_job(), job_queue._process_job(), retention.py
ProcessingJob.query.filter_by(recording_id=recording_id).delete()
db.session.delete(recording)
```

## Frontend Components

### 1. Unified Progress Tracking System (`static/js/app.modular.js`)

The frontend merges multiple data sources into a single unified list to prevent duplicate entries.

#### Data Sources
1. **Backend Jobs** (`allJobs`) - Polled from `/api/recordings/job-queue-status`
2. **Upload Queue** (`uploadQueue`) - Client-side tracking of uploads
3. **Global Progress Refs** - `currentlyProcessingFile`, `processingProgress`, `processingMessage`

#### Unified Progress Items
```javascript
const unifiedProgressItems = computed(() => {
    const items = new Map(); // Key by recordingId or clientId

    // 1. Add backend jobs (most accurate status)
    for (const job of allJobs.value) {
        const key = `rec_${job.recording_id}`;
        items.set(key, {
            id: key,
            recordingId: job.recording_id,
            jobId: job.id,
            title: job.recording_title,
            status: mapJobStatus(job),
            progress: getProgress(job),
            progressMessage: getMessage(job),
            // ...
        });
    }

    // 2. Add/merge upload queue items
    for (const upload of uploadQueue.value) {
        // If recordingId exists and matches a job, merge
        // Otherwise create new entry
        // Use global progress refs for currently uploading file
    }

    // Sort by status priority
    return Array.from(items.values()).sort(byStatusPriority);
});
```

#### Unified Status States
| Status | Description | Icon | Color |
|--------|-------------|------|-------|
| `uploading` | File uploading to server | cloud-upload-alt | blue |
| `transcribing` | Server transcribing audio | microphone-alt | purple |
| `summarizing` | Server generating summary | file-alt | green |
| `queued` | Waiting in server queue | clock | yellow |
| `ready` | Waiting to upload | clock | gray |
| `completed` | Processing finished | check-circle | green |
| `failed` | Server processing failed | exclamation-circle | red |
| `upload_failed` | Upload failed | exclamation-circle | red |

#### Filtered Views
```javascript
const activeProgressItems = computed(() =>
    unifiedProgressItems.value.filter(item =>
        ['uploading', 'transcribing', 'summarizing', 'queued', 'ready'].includes(item.status)
    )
);

const completedProgressItems = computed(() =>
    unifiedProgressItems.value.filter(item => item.status === 'completed')
);

const failedProgressItems = computed(() =>
    unifiedProgressItems.value.filter(item =>
        ['failed', 'upload_failed'].includes(item.status)
    )
);
```

#### Key Deduplication Logic
- Items keyed by `rec_${recordingId}` when recordingId is known
- Items keyed by `client_${clientId}` for uploads without recordingId yet
- When upload completes and gets recordingId, it merges with any existing job entry
- Backend job data takes precedence for status (more accurate)
- Upload progress refs used for live progress during upload phase

### 2. Progress Popup Template (`templates/components/progress-popup.html`)

Displays unified progress items in a single list:

```html
<!-- Active Items -->
<div v-for="item in activeProgressItems" :key="item.id">
    <i :class="getStatusDisplay(item.status).icon"></i>
    <span>${item.title}</span>
    <div v-if="item.status === 'uploading'" class="progress-bar">
        <div :style="{width: item.progress + '%'}"></div>
    </div>
    <p>${item.progressMessage}</p>
</div>

<!-- Failed Items (with retry/delete buttons) -->
<div v-for="item in failedProgressItems" :key="item.id">
    <span>${item.title}</span>
    <span v-if="item.errorMessage">${item.errorMessage}</span>
    <button @click="retryProgressItem(item)">Retry</button>
    <button @click="removeProgressItem(item)">Delete</button>
</div>

<!-- Completed Items -->
<div v-for="item in completedProgressItems" :key="item.id">
    <span>${item.title}</span>
    <span>Done</span>
</div>
```

## Job Lifecycle Flow

### New Upload Flow
```
1. User drops file
   └─▶ uploadQueue.push({status: 'queued', clientId: 'client-xxx'})

2. User clicks Upload
   └─▶ item.status = 'ready'

3. Upload starts
   └─▶ item.status = 'uploading'
   └─▶ processingProgress updates (10%, 20%, ...)

4. Upload completes (HTTP 202)
   └─▶ item.recordingId = response.id
   └─▶ item.status = 'pending'
   └─▶ Recording created in DB with status='PENDING'
   └─▶ ProcessingJob created with status='queued', is_new_upload=true

5. Job Queue Worker claims job
   └─▶ job.status = 'processing'
   └─▶ Recording.status = 'PROCESSING'

6. Transcription completes
   └─▶ job.status = 'completed' (if no auto-summary)
   └─▶ OR Recording.status = 'SUMMARIZING' (if auto-summary)

7. Summary completes (if applicable)
   └─▶ Summary job.status = 'completed'
   └─▶ Recording.status = 'COMPLETED'
```

### Reprocessing Flow
```
1. User clicks Reprocess
   └─▶ ProcessingJob created with is_new_upload=false

2. Job Queue Worker processes
   └─▶ On failure: Recording.status = 'FAILED' (recording NOT deleted)

3. User can retry or manually fix
```

## Database Migrations

The `is_new_upload` column was added via auto-migration in `src/init_db.py`:

```python
if add_column_if_not_exists(engine, 'processing_job', 'is_new_upload', 'BOOLEAN DEFAULT 0'):
    app.logger.info("Added is_new_upload column to processing_job table")
```

## Error Handling

### Upload Failures
- Stored in IndexedDB for background sync retry
- Shown in progress popup with retry button

### Processing Failures
- `is_new_upload=true`: Recording and audio file deleted
- `is_new_upload=false`: Recording marked as FAILED, can be retried

### Race Conditions
- Atomic UPDATE with WHERE clause prevents multiple workers claiming same job (v0.7.1+)
- SQLite WAL mode enabled for better concurrency

## API Endpoints Summary

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/recordings/job-queue-status` | GET | Get all jobs for current user |
| `/api/recordings/jobs/<id>/retry` | POST | Retry a failed job |
| `/api/recordings/jobs/<id>` | DELETE | Delete a job (and recording if failed new upload) |
| `/api/recordings/jobs/clear-completed` | POST | Clear all completed jobs |

## Future Improvements

1. **WebSocket Support**: Replace polling with WebSocket for real-time updates
2. **Job Priority**: Add priority levels for urgent jobs
3. **Batch Operations**: Support batch retry/delete operations
4. **Progress Estimation**: More accurate progress based on file size/duration
5. **Distributed Workers**: Support for multiple worker processes/servers
6. **Job Cancellation**: Allow canceling in-progress jobs
