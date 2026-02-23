# Migration Guide

This guide helps you migrate from the legacy transcription configuration to the new connector-based architecture introduced in Speakr v0.8.

## Overview

Speakr now uses a **connector-based architecture** for transcription services. This provides:

- **Simplified configuration** - Fewer environment variables needed
- **Auto-detection** - Speakr can attempt to automatically select the right connector
- **Better feature support** - Data-driven UI that adapts to connector capabilities
- **Extensibility** - Possibility to add custom connectors for new providers

## Backwards Compatibility

**Your existing configuration will continue to work.** The new architecture maintains full backwards compatibility with legacy environment variables. However, you may see deprecation warnings in the logs for certain settings.

## What's Changed

### Deprecated Environment Variables

| Deprecated Variable | Status | Migration |
|---------------------|--------|-----------|
| `USE_ASR_ENDPOINT=true` | Still works, logs warning | Just set `ASR_BASE_URL` instead |
| `WHISPER_MODEL` | Still works, logs warning | Use `TRANSCRIPTION_MODEL` instead |

### New Environment Variables

| Variable | Description |
|----------|-------------|
| `TRANSCRIPTION_CONNECTOR` | Explicit connector selection (optional, auto-detected) |
| `TRANSCRIPTION_MODEL` | Model name for OpenAI connectors |

### Auto-Detection Priority

Speakr automatically selects a connector based on your configuration:

1. **Explicit selection** - If `TRANSCRIPTION_CONNECTOR` is set, use that connector
2. **ASR mode** - If `ASR_BASE_URL` is set, use the ASR Endpoint connector
3. **OpenAI Transcribe** - If `TRANSCRIPTION_MODEL` contains `gpt-4o`, use OpenAI Transcribe connector
4. **Default** - Use OpenAI Whisper connector with `TRANSCRIPTION_MODEL` or `whisper-1`

## Migration Examples

### From Legacy ASR Configuration

**Before (Legacy):**
```bash
USE_ASR_ENDPOINT=true
ASR_BASE_URL=http://whisperx-asr:9000
ASR_DIARIZE=true
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

**After (New - Minimal):**
```bash
ASR_BASE_URL=http://whisperx-asr:9000
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

The `USE_ASR_ENDPOINT=true` is no longer needed—setting `ASR_BASE_URL` automatically enables ASR mode. Diarization is enabled by default for ASR endpoints.

### From Legacy Whisper Configuration

**Before (Legacy):**
```bash
TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
TRANSCRIPTION_API_KEY=sk-xxx
WHISPER_MODEL=whisper-1
```

**After (New):**
```bash
TRANSCRIPTION_API_KEY=sk-xxx
TRANSCRIPTION_MODEL=whisper-1
```

The base URL defaults to OpenAI's API, and `TRANSCRIPTION_MODEL` replaces the deprecated `WHISPER_MODEL`.

### Upgrading to OpenAI Diarization

If you want speaker diarization without running a self-hosted ASR service:

**New Configuration:**
```bash
TRANSCRIPTION_API_KEY=sk-xxx
TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize
```

This uses OpenAI's built-in diarization. The connector is auto-detected from the model name.

## Chunking Behavior Changes

The new architecture makes chunking **connector-aware**:

| Connector | Chunking Behavior |
|-----------|-------------------|
| **ASR Endpoint** | Handled internally—your `CHUNK_*` settings are ignored |
| **OpenAI Transcribe** | Handled internally via `chunking_strategy=auto`—your settings are ignored |
| **OpenAI Whisper** | Uses your `CHUNK_LIMIT` and `CHUNK_OVERLAP_SECONDS` settings |

If you were manually configuring chunking for ASR endpoints, you can remove those settings as they no longer have any effect.

## UI Feature Changes

Some UI features are now **data-driven** rather than configuration-driven:

| Feature | Old Behavior | New Behavior |
|---------|--------------|--------------|
| Speaker identification button | Shown when `USE_ASR_ENDPOINT=true` | Shown when transcription has diarization data |
| Min/Max speakers in reprocess | Always shown for ASR | Only shown when connector supports it |
| Bubble view toggle | Based on config | Based on whether transcription has dialogue |

This means features automatically appear when available, regardless of which connector produced the transcription.

## Verifying Your Migration

After updating your configuration:

1. **Check the logs** - Look for deprecation warnings:
   ```bash
   docker compose logs app | grep -i deprecat
   ```

2. **Test transcription** - Upload a test file and verify it transcribes correctly

3. **Check system info** - Visit `/api/system/info` to see the active connector:
   ```json
   {
     "transcription": {
       "connector": "asr_endpoint",
       "supports_diarization": true,
       "supports_speaker_embeddings": true
     }
   }
   ```

## Recommended Configuration

### For Self-Hosted (Best Quality)

Using WhisperX ASR Service for superior transcription and diarization:

```bash
# Transcription
ASR_BASE_URL=http://whisperx-asr:9000
ASR_RETURN_SPEAKER_EMBEDDINGS=true

# Text generation
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=sk-or-v1-xxx
TEXT_MODEL_NAME=openai/gpt-4o-mini
```

### For Cloud-Based (No Self-Hosting)

Using OpenAI's transcription with diarization:

```bash
# Transcription
TRANSCRIPTION_API_KEY=sk-xxx
TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize

# Text generation
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=sk-or-v1-xxx
TEXT_MODEL_NAME=openai/gpt-4o-mini
```

## Troubleshooting

### "Connector not found" Error

Ensure you have the correct environment variables set. Check the auto-detection priority above.

### Features Missing After Migration

If UI features like speaker identification are missing:
- Verify the transcription actually contains diarization data
- Check that your connector supports the feature (e.g., voice profiles require ASR endpoint)

### Deprecation Warnings in Logs

These are informational only—your configuration still works. Update your `.env` file at your convenience to use the new variable names.

## Getting Help

If you encounter issues during migration:

1. Check the [troubleshooting guide](../troubleshooting.md)
2. Review the [installation guide](../getting-started/installation.md) for complete configuration examples
3. Open an issue on [GitHub](https://github.com/murtaza-nasir/speakr/issues)

---

# Migrating Audio Files to S3

Speakr supports storing recording audio files in S3-compatible object storage (AWS S3, MinIO, etc.) alongside or instead of the local filesystem. This section covers how to transition an existing instance from local storage to S3.

## Prerequisites

- Speakr updated to a version that includes the storage abstraction layer
- S3 bucket created and accessible from your Speakr server
- S3 credentials configured in `.env` (see [installation guide](../getting-started/installation.md#file-storage-backend-local-s3-compatible))
- `boto3>=1.34.0` installed (included in the default Docker image)

## Migration Phases

The migration is designed to be **gradual and zero-downtime**. Each phase is independent and can be performed separately.

### Phase 1: Deploy Storage Abstraction (No Behavior Change)

Update Speakr to the version with the storage layer while keeping `FILE_STORAGE_BACKEND=local` (the default). The application continues to work exactly as before — all reads, writes, and deletions now go through the unified storage service but still operate on local files.

```bash
FILE_STORAGE_BACKEND=local
```

### Phase 2: Normalize Legacy Paths

Existing recordings may have inconsistent `audio_path` values (absolute paths, relative paths). The normalization script converts them all to the `local://` locator format without moving any files:

```bash
# Preview changes without writing
docker compose exec app python scripts/migrate_local_paths_to_local_locator.py --dry-run

# Run the actual normalization
docker compose exec app python scripts/migrate_local_paths_to_local_locator.py
```

**Available options:**

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview changes without writing to DB |
| `--limit N` | Process only the first N records |
| `--recording-id ID` | Process a single recording |
| `--only-user ID` | Process recordings for a specific user |
| `--allow-missing-file` | Normalize even if the local file is missing |
| `--report-jsonl <path>` | Write a JSONL report of all actions |

The script is **idempotent** — running it multiple times is safe.

### Phase 3: Switch New Uploads to S3

Configure S3 credentials and switch the storage backend:

```bash
FILE_STORAGE_BACKEND=s3
S3_BUCKET_NAME=speakr-audio
S3_ENDPOINT_URL=http://minio:9000     # For MinIO
S3_ACCESS_KEY_ID=minioadmin
S3_SECRET_ACCESS_KEY=minioadmin
S3_USE_PATH_STYLE=true                # Required for MinIO
```

After restarting, **new uploads** go to S3, while existing `local://` recordings continue to be served from the local filesystem. The application supports both backends simultaneously.

### Phase 4: Migrate Historical Files to S3

Move existing local files to S3 using the migration script:

```bash
# Preview what would be migrated
docker compose exec app python scripts/migrate_local_recordings_to_s3.py --dry-run

# Run the migration (verifies upload size by default)
docker compose exec app python scripts/migrate_local_recordings_to_s3.py --limit 100
```

**Available options:**

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview without uploading or modifying DB |
| `--limit N` | Migrate only N records per run |
| `--recording-id ID` | Migrate a specific recording |
| `--only-user ID` | Migrate recordings for a specific user |
| `--verify-size` | Verify uploaded size matches local (enabled by default) |

The script is **idempotent**:

- Recordings already on `s3://` are skipped
- If the S3 object already exists with matching size, only the DB is updated
- Recordings in `PROCESSING` or `QUEUED` status are skipped to avoid race conditions

Run in batches and monitor progress. Repeat until all local files are migrated.

### Optional: Backfill Cached Audio Durations (`audio_duration_seconds`)

If your instance already had recordings before the `audio_duration_seconds` cache field was introduced, older rows may still have `NULL` values. You can backfill them with the dedicated script:

```bash
# Preview only (no DB writes)
docker compose exec app python scripts/backfill_audio_duration_seconds.py --dry-run

# Run backfill for all eligible recordings
docker compose exec app python scripts/backfill_audio_duration_seconds.py
```

The script:

- updates **only** recordings where `audio_duration_seconds IS NULL`
- skips recordings with `audio_deleted_at` set
- skips active jobs (`PROCESSING`, `QUEUED`)
- calculates duration using the storage abstraction + `ffprobe`, so it works for both `local://` and `s3://` recordings

**Useful options:**

| Flag | Description |
|------|-------------|
| `--dry-run` | Preview without updating DB |
| `--limit N` | Process only the first N recordings |
| `--recording-id ID` | Backfill one recording |
| `--only-user ID` | Backfill recordings for one user |
| `--report-jsonl <path>` | Write a JSONL report |
| `--ffprobe-timeout SECONDS` | Override ffprobe timeout (default: 30) |

Example with report:

```bash
docker compose exec app python scripts/backfill_audio_duration_seconds.py --report-jsonl /tmp/audio-duration-backfill.jsonl
```

### Phase 5: Cleanup Local Files

After confirming all recordings are served from S3, you can reclaim local disk space. The migration script deletes local source files after successful upload by default. If you disabled this, you can clean up manually or with a dedicated pass.

### Phase 6: Enforce S3-Only Storage (Optional)

Once all recordings are migrated, you may optionally remove legacy path support by disabling the local fallback. This is not required — Speakr will continue to route correctly based on each recording's locator indefinitely.

## Verifying the Migration

After migration, verify that:

1. **Audio playback** works for migrated recordings (they should redirect to presigned S3 URLs)
2. **New uploads** are stored in S3 (check `recording.audio_path` starts with `s3://`)
3. **Reprocessing** works — the worker materializes audio from S3 to a temporary local file for transcription
4. **Deletion** and **retention** properly remove S3 objects
5. **Shared links** generate working presigned URLs with appropriate TTL
6. **Cached durations** are backfilled for older rows (check `recording.audio_duration_seconds` is no longer `NULL` for historical recordings)

## Rollback

If you need to revert to local storage:

1. Set `FILE_STORAGE_BACKEND=local` in `.env`
2. Restart the container
3. New uploads will go to local storage again
4. Existing `s3://` recordings continue to work as long as S3 credentials remain configured

The system is designed so that both backends can coexist indefinitely.
