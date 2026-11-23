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
    # Delete audio file via storage service (works for both local and S3)
    if recording.audio_path:
        from src.services.storage import get_storage_service
        get_storage_service().delete(recording.audio_path, missing_ok=True)
    # Delete all processing jobs for this recording
    ProcessingJob.query.filter_by(recording_id=recording_id).delete()
    # Delete the recording
    db.session.delete(recording)
```

### 3. Job Queue API (`src/api/recordings.py`)

#### Get Job Queue Status
`GET /api/recordings/job-queue-status`

The jobs of the current user are returned: every waiting or running job, and the jobs completed or failed within the last hour. Inquire chunk jobs are not listed.

```json
{
  "server_time": "2026-11-15T09:31:10Z",
  "jobs": [
    {
      "id": 123,
      "recording_id": 456,
      "recording_title": "Meeting Notes",
      "job_status": "processing",
      "job_type": "transcribe",
      "queue_type": "transcription",
      "is_new_upload": true,
      "error_message": null,
      "stage": "transcribing",
      "stage_detail": null,
      "progress": {"done": 3, "total": 8},
      "progress_kind": "chunks",
      "progress_done": 3,
      "progress_total": 8,
      "position": null,
      "queue_position": null,
      "queue": null,
      "cancel_requested": false,
      "can_cancel": true,
      "created_at": "2026-11-15T09:30:00Z",
      "started_at": "2026-11-15T09:30:05Z",
      "completed_at": null,
      "finished_at": null
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `stage` | A stable code: `queued`, `converting`, `assembling` (stitch), `joining` (merge), `transcribing`, `titling`, `summarizing`, `translating`, `completed` or `failed`. See [Progress and Stage](#6-progress-and-stage). |
| `stage_detail` | The step reported by the transcription service inside `transcribing`: `waiting`, `preparing`, `transcribing`, `aligning` or `diarizing`; `null` when nothing is reported. |
| `progress` | `{"done", "total"}` or `{"fraction"}` for a running job with a measured number; `null` otherwise. |
| `progress_kind` | The unit of `progress`: `chunks` or `fraction`; `null` without a number. |
| `progress_done`, `progress_total` | The stored values, kept for clients of earlier releases. |
| `queue_position` | For a waiting job, its place in the queue named in `queue`, counting the running jobs of that queue; `null` otherwise. `position` holds the same value. |
| `queue` | For a waiting job: `convert`, `transcription` or `summary`. `queue_type` is set for every job. |
| `cancel_requested` | `true` for a running job between the cancel request and the stop of its worker. |
| `can_cancel` | `true` when `DELETE` on the job cancels it. |
| `created_at`, `started_at`, `completed_at` | UTC, with a trailing `Z`. |
| `finished_at` | The end time of a completed or failed job; `null` for a waiting or running job. |
| `server_time` | The time of the server, for an elapsed time that does not depend on the clock of the browser. |

Timestamps in this answer have a trailing `Z`. In earlier releases the same instants were sent without it. The naive UTC form is kept in `ProcessingJob.to_dict()` and in the other answers of the application.

#### Retry Failed Job
`POST /api/recordings/jobs/<id>/retry`

The job status is reset to `queued` for another attempt. Progress, stage and a cancel mark of the failed attempt are cleared.

#### Cancel or Remove a Job
`DELETE /api/recordings/jobs/<id>`

The result depends on the state of the job:

| Job state | Result | Answer |
|---|---|---|
| `queued` | The job is removed and the recording is settled as described under [Cancel](#7-cancel). | `200`, `canceled: true` |
| `processing` | The job is marked for cancel and is stopped at the next safe point. The row stays in the queue status, with `cancel_requested: true`, until then. A repeated request gets the same answer. | `202`, `cancel_requested: true` |
| `completed` | The row is removed. The recording is not changed. | `200` |
| `failed` | The row is removed. For a failed new upload (`is_new_upload`), the recording and its audio are deleted with it. | `200` |
| `queued` or `processing`, type `stitch` or `merge` | Nothing is changed: the audio of the recording is not assembled yet. | `409`, `job_not_cancelable` |
| Ended between the request and the cancel | Nothing is canceled; the row stays in the list. | `409`, `job_already_finished` |
| Unknown id, or a second request for a removed job | | `404`, `job_not_found` |

The request is accepted from the owner of the job. An administrator can cancel a waiting or running job of another user; every other request for a job of another user is answered with `403`.

#### Clear Completed Jobs
`POST /api/recordings/jobs/clear-completed`

Removes all completed jobs for the current user.

#### Clear Failed Jobs
`POST /api/recordings/jobs/clear-failed`

All failed jobs of the current user are removed in one step. The answer is `{"success": true, "deleted": <count>}`. Only the job rows are removed. The recordings stay in the library with the status `FAILED`, where each can be reprocessed or deleted. To delete the recording of a failed new upload together with its job, use `DELETE` on that job.

#### Cancel an Upload
`POST /upload/cancel` with `{"client_upload_id": "<id>"}`

A `client_upload_id` (8 to 80 characters: letters, digits, `-` and `_`) can be sent with an upload form. In the web client, the id of the queue item is used. After the browser has aborted the upload request, the id is sent to this endpoint, and what the upload has left on the server is removed:

| Moment of the abort | What is left, and how it is removed |
|---|---|
| While the request body arrives | Only the temporary file of the multipart parser, which is removed with the request. No staging file and no recording exist yet. |
| After the body has arrived, before the recording is stored | The abort is not noticed by the server. The cancel mark is read before the file is probed and again before the first job is queued: the staging file or the new recording is discarded, no job is queued and no webhook is sent. The aborted request is answered with `409`, `upload_canceled`. |
| After the recording is stored | The recording is deleted by the cancel call (`recording_deleted: true`), as long as it was stored less than 15 minutes ago and is not `COMPLETED`. Otherwise it is kept (`recording_kept: true`). |
| Sliced upload, before finalize | The session and its slices are removed with `DELETE /upload/session/<id>`. |
| Join of several files | The stored parts are removed with `DELETE /upload/join/<group>`. |

Rows of the `client_upload` table are removed after 24 hours. Uploads without a `client_upload_id` are processed as before.

### 4. Recording Deletion Cascade

When deleting a recording, associated processing jobs must be deleted first due to the NOT NULL constraint on `recording_id`:

```python
# In delete_recording(), delete_job(), job_queue._process_job(), retention.py
ProcessingJob.query.filter_by(recording_id=recording_id).delete()
db.session.delete(recording)
```

### 5. Asynchronous Jobs of the WhisperX ASR Service

The `asr_endpoint` connector uses the jobs API of the WhisperX ASR service (version 0.5.0 and later) when the service advertises it in `/health` and `asr_async_jobs` is on. Inside a transcription job, the audio is sent with `POST /jobs`, the remote job is polled with `GET /jobs/<id>`, the result is fetched from `GET /jobs/<id>/result`, and the remote job is then removed with `DELETE /jobs/<id>`. The code is in `src/services/transcription/connectors/asr_endpoint.py` and `src/services/transcription/provider_tasks.py`.

#### Saved Task

As soon as the service accepts a job, its id is saved in `ProcessingJob.provider_task`, in a transaction of its own:

```json
{"provider": "asr_endpoint", "audio_sha256": "...", "task": {"job_id": "...", "base_url": "http://whisperx:9000"}}
```

A saved task is used only for the same provider, the same service URL and the same audio bytes.

#### Resume After a Restart

Processing jobs that were running when Speakr stopped are put back in the queue at startup. When such a job runs again, or when a failed attempt is retried, the saved remote job is polled and the audio is not submitted again. The audio is submitted again, once per attempt, only when the service no longer knows the job, when its result has expired, or when the service reports the job as `interrupted` after a restart of its own.

Each submit carries an `Idempotency-Key` of the form `speakr-<recording id>-<processing job id>-<audio hash>-<parameter digest>`. A submit whose answer was lost is therefore attached to the job that is already running.

#### Remote Cleanup

A remote job and its transcript are removed from the service at these points:

| When | Where |
|---|---|
| The processing job was canceled | the cancel request, and the connector at its next poll |
| The result was fetched | the connector |
| The remote job failed or was cancelled on the service | the connector |
| The processing job ended for good: a permanent error, or the last retry failed | `release_remote_task()` from the job queue |
| The processing job completed with a task left over | `release_remote_task()` from the job queue |
| The recording or the processing job was deleted, or the recording was reset | `cancel_remote_tasks()` |
| The synchronous request took over while a job was saved | the connector |
| Old processing jobs are cleaned up | one more attempt for a task that is still saved |

A failed attempt with retries left keeps the remote job, so that the retry can poll it. Cleanup is best effort. When the service cannot be reached, the task stays saved and the remote job is kept until its result expires on the service (`job_result_ttl_seconds` in the capabilities).

#### Requests Outside a Processing Job

Three kinds of request have no processing job to save a task on: the chunks of a chunked transcription, incognito mode and the voice check. Their remote jobs cannot be resumed, so the connector deletes the remote job on every failed attempt (a timeout, failed polls, an unreadable result or a shutdown). Their `Idempotency-Key` is built by `audio_task_key()`:

| Request | Key before the parameter digest |
|---|---|
| Chunk | `speakr-<recording id>-chunk<n>-<audio hash>`; without a recording, a scope made for the call takes the place of the recording id |
| Incognito mode | `speakr-incognito-<user id>-<audio hash>` |
| Voice check | `speakr-voice-check-<audio hash>` |

A retry of the same chunk therefore attaches to a job the service still holds, and different audio never shares a key.

### 6. Progress and Stage

A progress number is stored for a job only where a measured number exists. No estimate is stored or shown. For a job without a number, the client shows a moving bar with the time since `started_at`.

| Job | `progress` | `progress_kind` |
|---|---|---|
| Chunked transcription, on any service | Chunks finished of chunks in total | `chunks` |
| Transcription through the asynchronous jobs of the WhisperX ASR service | The fraction reported by the service, unless it is marked as estimated (`fraction_estimated`) | `fraction` |
| Translation | Chunks finished of chunks in total | `chunks` |
| Summary, title, conversion, stitch, merge, and transcription by a service without progress reports | None | `null` |

A fraction marked as estimated by the WhisperX ASR service is written to the log and is not stored. The stage reported by the service is stored in both cases, as `stage_detail`.

The values are held in `ProcessingJob.progress_done`, `progress_total` and `progress_kind`; a fraction is stored as a whole number out of 100. The step reported by the worker (`titling`, `summarizing`) is stored in `ProcessingJob.stage`; without one, the stage is derived from the job type. The title and the first summary of an upload are generated inside the transcription job, so `titling` and `summarizing` are stages of that job.

Progress is written through `src/services/job_control.py`:

- At most one write per job every 3 seconds (`PROGRESS_WRITE_INTERVAL`). A value that arrives earlier is kept and written with the next report after the interval. A change of stage is written at once.
- Each write is one `UPDATE` on a connection of its own, committed at once. The session of the job is not used, so no transaction is left open on it during a long call to a transcription service or an LLM (see the PostgreSQL `idle_in_transaction_session_timeout` note in `src/tasks/processing.py`).
- A failed write is logged and ignored.
- All values are cleared when the job is claimed by a worker, so the numbers of the previous attempt are not shown for a retry.

### 7. Cancel

A cancel is requested with `DELETE /api/recordings/jobs/<id>`. A waiting job is removed in the request. For a running job, `ProcessingJob.cancel_requested_at` is set; the mark is stored in the database because the request can be served by a process without workers. The mark is read by the job at these safe points:

| Step | Safe points |
|---|---|
| Any job | Before the job starts |
| Transcription | Before the audio is sent; between two chunks; at each poll of a WhisperX asynchronous job; when the service has answered, before anything of the answer is stored |
| Title | Before and after the LLM call |
| Summary | Before the LLM call; when the LLM has answered, before the summary is stored |
| Translation | Between two chunks |
| Conversion | After the converted audio is stored, before the transcription is queued |

A synchronous request to a transcription service and a running LLM call are not interrupted. The cancel is applied when the call has returned, and the result of the call is discarded.

When a job is stopped by a cancel:

- The remote job is deleted on the WhisperX ASR service, by the cancel request and again at the next poll.
- Converted or extracted temporary files and the chunk files are removed.
- The job row is deleted. No retry is queued.
- The summary and translation jobs of the same recording are canceled with a canceled conversion or transcription: waiting ones are removed, running ones are marked.
- No webhook and no email is sent. There is no webhook event for a cancel; `recording.transcription.failed` and `recording.summary.failed` are not sent for a canceled job.

The recording after a cancel:

| Situation | Recording |
|---|---|
| A transcript is stored. This includes a cancel during the title or the summary of a transcription, where the transcript of that run is complete, and a canceled summary job without a stored copy. | `COMPLETED`. The transcript is kept. Shares, the export and the Inquire chunks are updated; events are not extracted. |
| No transcript is stored and the job was a reprocess. | The transcript, summary, events and status from before the reprocess are restored. |
| A canceled summary reprocess. | The summary, events and status from before the reprocess are restored. |
| No transcript is stored and nothing was cleared (a first transcription or a conversion). | `FAILED` with the error message `Processing was canceled.` The audio is kept, and the transcription is started again with Reprocess. |

A partial transcript is never stored: the transcript is written in one step after the answer of the transcription service has arrived.

The transcript, summary and events are cleared when a reprocess is queued. A copy of the cleared values is kept in `ProcessingJob.restore_state` while the job waits or runs, and is removed when the job is completed or has failed.

Race conditions:

- A cancel and a claim of the same waiting job at the same moment: the removal is limited to a job in status `queued` and the mark to a job in status `processing`, so exactly one of the two statements is applied.
- A cancel after the last safe point: the job is completed, the mark is cleared, and the completion notices are sent as usual.
- A failure while a cancel is pending: the job is treated as canceled. No retry is queued and no failure notice is sent.
- A restart of Speakr before the safe point is reached: the cancel is finished at startup, and the job is not put back in the queue.

`stitch` and `merge` jobs are not canceled. To discard such a recording, delete the recording.

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
            ...queueFieldsFromJob(job),   // stage code, real progress, times, actions
            id: key,
            recordingId: job.recording_id,
            jobId: job.id,
            title: job.recording_title,
            status: mapJobStatus(job),
            // ...
        });
    }

    // 2. Add/merge upload queue items
    for (const upload of uploadQueue.value) {
        // If recordingId exists and matches a job, merge
        // Otherwise create new entry
    }

    // Sort by status priority
    return Array.from(items.values()).sort(byStatusPriority);
});
```

#### Queue Data Layer

The functions that read one job of the queue status answer are in `static/js/modules/utils/queue-data.js`: `jobStage`, `jobProgress`, `elapsedSeconds`, `clockOffsetMs`, `queueFieldsFromJob`, `cancelResult` and `settledCancels`. The following fields are set on each unified item of a job: `stageCode`, `stageDetail`, `progressInfo`, `progress` (a percent, or `null` for a moving bar), `indeterminate`, `startedAt`, `finishedAt`, `queuePosition`, `queueName`, `canCancel`, `cancelRequested`, `canRetry` and `canRemove`, together with `jobType` and `isNewUpload`. An item of the upload queue carries `serverProcessing`, `serverProcessingAt` and `canCancelUpload`. The values 25, 50 and 80 that earlier releases set as `progress` for converting, transcribing and summarizing items were never sent by the server and are no longer set.

`cancelJob(jobId, recordingId)`, `cancelProgressItem(item)`, `clearFailedJobs()` and `jobQueueClockOffset` are returned from the setup function of `app.modular.js`, and `cancelUpload(clientId)` from the upload composable. The meaning of a cancel for an upload item is defined by `uploadCancelPlan` in `static/js/modules/utils/upload-cancel.js`. The upload of a recording made in the app is not canceled, because its audio exists only in the browser until the upload has finished.

The queue status is requested once when the page is loaded, so that rows which failed or finished within the last hour are listed after a reload. It is polled every 5 seconds only while an upload, a job or a recording is in progress.

#### Unified Status States
| Status | Description | Group in the panel |
|--------|-------------|--------------------|
| `uploading` | File uploading to server | Active |
| `transcribing` | A job of the transcription queue is running (converting, joining, transcribing, titling, summarizing) | Active |
| `summarizing` | A job of the summary queue is running | Active |
| `translating` | A translation job is running | Active |
| `queued` | Waiting in a server queue | Waiting |
| `ready` | Waiting to upload | Waiting |
| `failed` | Server processing failed | Failed |
| `upload_failed` | Upload failed | Failed |
| `completed` | Processing finished | Recently finished |

#### Key Deduplication Logic
- Items keyed by `rec_${recordingId}` when recordingId is known
- Items keyed by `client_${clientId}` for uploads without recordingId yet
- A translation job is keyed by `tr_${recordingId}`, so it has a row of its own beside the other jobs of the recording
- When upload completes and gets recordingId, it merges with any existing job entry
- Backend job data takes precedence for status (more accurate)
- Upload progress refs used for live progress during upload phase

### 2. Indicator and Panel (`templates/components/queue/_queue.html`, `static/css/queue.css`)

The queue is shown as a one-line indicator and a panel. The markup of both is in the macros of `templates/components/queue/_queue.html`, which are used at these places:

| Place | Template | Shown when |
|---|---|---|
| Indicator at the foot of the recording list, or the docked panel | `components/sidebar.html` | The list is shown (`queuePlace === 'dock'`) |
| Pill in the top bar | `components/header.html` | The list is collapsed (`queuePlace === 'float'`) |
| Panel under the top bar | slot `status-area` | The list is collapsed and the panel is open |
| Indicator in the status bar | `themes/desk/status-area.html` | Desk theme, from 1024 px |
| Strip above the player strip, and at the foot of the list | `components/detail-view.html`, `components/sidebar.html` | Phone layout (`queuePlace === 'phone'`) |
| Sheet | slot `status-area`, with the sheet macro of the phone layout | Phone layout, `phoneSheet === 'queue'` |

The state is held in `app.modular.js` under "THE QUEUE ON THE PAGE":

- `progressPopupMinimized` is the one stored state of the desktop panel. It starts as `true` and is set to `false` when an upload or a recording is started. On a phone the panel is open while the sheet named `queue` is open.
- `queueGroupList` holds the four groups (Active, Waiting, Failed, Recently finished). Each item carries `view`, the result of `queueRowView`: the stage in words, the progress model, the number, the start of the step and the end of the job in the clock of the browser, the error text and the actions of the row.
- `queueSummary`, `queueParts` and `queuePillParts` hold the counts and the parts of the indicator line.
- `toggleQueuePanel`, `openQueueRecording` and `cancelQueueItem` are the actions that are not already job functions. A cancel is confirmed with `window.confirmDialog` only when `cancelNeedsConfirm(item)` is true: a running first transcription before its transcript is stored.
- The classes `queue-open` and `queue-minimized` are set on `<body>` for the stylesheets.

The grouping, the counts and the wording are pure functions in `static/js/modules/utils/queue-view.js`: `queueGroup`, `groupQueueItems`, `queueSummary`, `summaryParts`, `stageLabel`, `rowProgress`, `formatElapsed`, `finishedAgo`, `rowMark` and `cancelNeedsConfirm`. `rowProgress` returns `percent` (the uploaded share of a file, a measured fraction), `count` (parts finished of a total), `moving` (no number: a moving bar with the time since the start of the step) or `none`. No function returns an estimate.

Elapsed times and the time since a row finished are written by one timer of one second (`tickQueueClock`) directly into the elements marked `data-queue-since` and `data-queue-finished`. No reactive state is changed by a passing second, so the page is not rendered again. The timer is stopped while the panel is minimized and while the tab is in the background. The only animations are three CSS animations on `transform`: the spinner, the hairline of the indicator and the moving bar.

The surface, the borders and the type colors are the tokens of the raised layer (`--pop-*`, `--type-*`) in `static/css/styles.css`, which are mixed from the color variables every theme defines. See [UI Themes](ui-themes.md#the-raised-layer-toasts-and-the-processing-queue).

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

6. Transcription completes (transcript and title stored)
   └─▶ job.status = 'completed', the transcription worker is free
   └─▶ no auto-summary: Recording.status = 'COMPLETED'
   └─▶ auto-summary: a 'summarize' job (params after_transcription) is queued
       on the summary workers; Recording.status = 'SUMMARIZING', transcript readable

7. Summary job ends (if applicable)
   └─▶ Recording.status = 'COMPLETED'; events, auto-shares, export
   └─▶ recording.transcription.completed webhook and the completion email
   └─▶ a failed summary also leaves the recording COMPLETED, with the error as its summary
   └─▶ a canceled summary job leaves the recording COMPLETED, without a summary
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

For the queue redesign, five nullable columns were added to `processing_job` (`progress_kind`, `stage`, `stage_detail`, `cancel_requested_at`, `restore_state`) with `add_column_if_not_exists`. The `client_upload` table is created by `create_all()`. Existing rows need no change.

The `is_new_upload` column was added via auto-migration in `src/init_db.py`:

```python
if add_column_if_not_exists(engine, 'processing_job', 'is_new_upload', 'BOOLEAN DEFAULT 0'):
    app.logger.info("Added is_new_upload column to processing_job table")
```

## Error Handling

### Upload Failures
- Stored in IndexedDB for background sync retry
- Listed in the Failed group of the queue panel, with Retry

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
| `/api/recordings/jobs/<id>` | DELETE | Cancel a waiting or running job; remove a finished job (and the recording of a failed new upload) |
| `/api/recordings/jobs/clear-completed` | POST | Clear all completed jobs |
| `/api/recordings/jobs/clear-failed` | POST | Clear all failed jobs; the recordings are kept |
| `/upload/cancel` | POST | Discard what an aborted upload has left on the server |

## Future Improvements

1. **WebSocket Support**: Replace polling with WebSocket for real-time updates
2. **Job Priority**: Add priority levels for urgent jobs
3. **Batch Operations**: Support batch retry/delete operations
4. **Distributed Workers**: Support for multiple worker processes/servers
