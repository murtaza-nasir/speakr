# Migration Guide: Connector Architecture

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
