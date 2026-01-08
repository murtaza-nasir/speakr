# Connector Architecture Testing Checklist

This checklist covers manual testing scenarios for the transcription connector architecture. Use this alongside the automated tests (`tests/test_connector_architecture.py`) for comprehensive coverage.

## Prerequisites

- Speakr running in Docker
- Access to at least one of:
  - OpenAI API key (for OpenAI connectors)
  - Self-hosted ASR service (for ASR endpoint connector)
- Test audio files:
  - Short file (<1 minute, single speaker)
  - Longer file (>5 minutes, multiple speakers)
  - Opus format file (for codec conversion testing)

---

## 1. Connector Auto-Detection

### Test 1.1: OpenAI Transcribe Auto-Detection
**Config:**
```bash
TRANSCRIPTION_API_KEY=sk-xxx
TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize
```

**Expected:**
- [ ] Logs show "Auto-detected OpenAI Transcribe"
- [ ] `/api/system/info` shows `connector: "openai_transcribe"`
- [ ] Transcription works with speaker labels (A, B, C...)

### Test 1.2: ASR Endpoint Auto-Detection
**Config:**
```bash
ASR_BASE_URL=http://whisperx-asr:9000
```

**Expected:**
- [ ] Logs show "Auto-detected ASR endpoint"
- [ ] `/api/system/info` shows `connector: "asr_endpoint"`
- [ ] No deprecation warning (USE_ASR_ENDPOINT not set)

### Test 1.3: Legacy USE_ASR_ENDPOINT Backwards Compatibility
**Config:**
```bash
USE_ASR_ENDPOINT=true
ASR_BASE_URL=http://whisperx-asr:9000
```

**Expected:**
- [ ] Logs show deprecation warning for USE_ASR_ENDPOINT
- [ ] Connector still works correctly
- [ ] `/api/system/info` shows `connector: "asr_endpoint"`

### Test 1.4: Explicit Connector Selection
**Config:**
```bash
TRANSCRIPTION_CONNECTOR=openai_whisper
TRANSCRIPTION_API_KEY=sk-xxx
ASR_BASE_URL=http://whisperx-asr:9000  # Should be ignored
```

**Expected:**
- [ ] Uses openai_whisper despite ASR_BASE_URL being set
- [ ] `/api/system/info` shows `connector: "openai_whisper"`

---

## 2. Speaker Diarization

### Test 2.1: OpenAI Transcribe Diarization
**Config:** Use `TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize`

**Steps:**
1. Upload audio with 2+ speakers
2. Wait for transcription to complete

**Expected:**
- [ ] Transcription shows speaker labels (A, B, C...)
- [ ] Speaker identification button appears in UI
- [ ] Bubble view toggle available
- [ ] Can rename speakers

### Test 2.2: ASR Endpoint Diarization
**Config:** Use ASR endpoint with `ASR_DIARIZE=true`

**Steps:**
1. Upload audio with 2+ speakers
2. Wait for transcription to complete

**Expected:**
- [ ] Transcription shows speaker labels (SPEAKER_00, SPEAKER_01...)
- [ ] Speaker identification button appears in UI
- [ ] Min/max speakers options visible in upload form
- [ ] Min/max speakers options visible in reprocess modal

### Test 2.3: OpenAI Whisper (No Diarization)
**Config:** Use `TRANSCRIPTION_MODEL=whisper-1`

**Steps:**
1. Upload audio with 2+ speakers
2. Wait for transcription to complete

**Expected:**
- [ ] Transcription is plain text (no speaker labels)
- [ ] Speaker identification button NOT visible
- [ ] Bubble view toggle NOT available

---

## 3. UI Feature Visibility

### Test 3.1: Speaker Count Controls (ASR Only)
**ASR Config:**
- [ ] Min speakers field visible in upload form
- [ ] Max speakers field visible in upload form
- [ ] Min/max speakers visible in reprocess modal

**OpenAI Config:**
- [ ] Min speakers field NOT visible
- [ ] Max speakers field NOT visible

### Test 3.2: Speaker Identification Based on Data
Upload the same file with different connectors and verify:

- [ ] Speaker ID button shows when transcription HAS diarization
- [ ] Speaker ID button hidden when transcription has NO diarization
- [ ] Behavior is the same regardless of current connector config

---

## 4. Chunking Behavior

### Test 4.1: ASR Endpoint (Internal Chunking)
**Config:**
```bash
ASR_BASE_URL=http://whisperx-asr:9000
ENABLE_CHUNKING=true
CHUNK_LIMIT=10MB
```

**Steps:**
1. Upload file >10MB

**Expected:**
- [ ] File uploads without app-level chunking
- [ ] Logs show "Connector handles chunking internally"
- [ ] No chunk progress in UI

### Test 4.2: OpenAI Whisper (App Chunking)
**Config:**
```bash
TRANSCRIPTION_MODEL=whisper-1
ENABLE_CHUNKING=true
CHUNK_LIMIT=10MB
```

**Steps:**
1. Upload file >10MB

**Expected:**
- [ ] File is chunked by the app
- [ ] Logs show chunk processing
- [ ] Transcription merges correctly

### Test 4.3: Chunking Disabled
**Config:**
```bash
TRANSCRIPTION_MODEL=whisper-1
ENABLE_CHUNKING=false
```

**Steps:**
1. Upload file >25MB

**Expected:**
- [ ] Upload fails with file size error (OpenAI 25MB limit)
- [ ] Error message is clear

---

## 5. Codec Handling

### Test 5.1: Opus File with OpenAI Connector
**Config:** Any OpenAI connector

**Steps:**
1. Upload an opus format audio file
2. Check logs

**Expected:**
- [ ] File is converted before transcription
- [ ] Logs show codec conversion
- [ ] Transcription succeeds

### Test 5.2: AUDIO_UNSUPPORTED_CODECS Override
**Config:**
```bash
AUDIO_UNSUPPORTED_CODECS=aac
```

**Steps:**
1. Upload an AAC format audio file
2. Check logs

**Expected:**
- [ ] File is converted before transcription
- [ ] Logs show "Excluding codecs from supported list"

### Test 5.3: Supported Format (No Conversion)
**Steps:**
1. Upload an MP3 file

**Expected:**
- [ ] No conversion occurs
- [ ] Transcription proceeds directly

---

## 6. Error Handling

### Test 6.1: Invalid API Key
**Config:**
```bash
TRANSCRIPTION_API_KEY=invalid-key
```

**Expected:**
- [ ] Clear error message about authentication
- [ ] Job fails gracefully
- [ ] UI shows error state

### Test 6.2: Unreachable ASR Endpoint
**Config:**
```bash
ASR_BASE_URL=http://nonexistent:9000
```

**Expected:**
- [ ] Connection error logged
- [ ] Job fails with timeout/connection error
- [ ] UI shows error state

### Test 6.3: Invalid Connector Name
**Config:**
```bash
TRANSCRIPTION_CONNECTOR=nonexistent
```

**Expected:**
- [ ] App fails to start (or logs critical error)
- [ ] Error message lists available connectors

---

## 7. Voice Profiles (ASR Only)

### Test 7.1: Speaker Embeddings
**Config:**
```bash
ASR_BASE_URL=http://whisperx-asr:9000
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

**Steps:**
1. Upload audio with known speaker
2. Create voice profile from transcription
3. Upload another audio with same speaker

**Expected:**
- [ ] Speaker embeddings returned in response
- [ ] Voice profile creation works
- [ ] Automatic speaker matching works

### Test 7.2: Embeddings Disabled
**Config:**
```bash
ASR_RETURN_SPEAKER_EMBEDDINGS=false
```

**Expected:**
- [ ] Voice profile features not available
- [ ] No embeddings in response

---

## 8. System Info API

### Test 8.1: Connector Capabilities Exposed
**Endpoint:** `GET /api/system/info`

**Expected Response Structure:**
```json
{
  "transcription": {
    "connector": "asr_endpoint",
    "supports_diarization": true,
    "supports_speaker_embeddings": true,
    "supports_speaker_count": true
  }
}
```

- [ ] connector field matches active connector
- [ ] supports_diarization accurate
- [ ] supports_speaker_embeddings accurate
- [ ] supports_speaker_count accurate

---

## 9. Reprocessing

### Test 9.1: Reprocess with Different Language
**Steps:**
1. Transcribe file (auto-detect language)
2. Reprocess with specific language

**Expected:**
- [ ] Reprocess uses selected language
- [ ] Transcription updates correctly

### Test 9.2: Reprocess with Speaker Count (ASR)
**Steps:**
1. Transcribe file with auto speaker detection
2. Reprocess with min_speakers=2, max_speakers=3

**Expected:**
- [ ] Min/max speakers respected
- [ ] Diarization quality may improve

---

## 10. Performance & Edge Cases

### Test 10.1: Very Long Audio
Upload audio >1 hour

**Expected:**
- [ ] Processing completes (may take time)
- [ ] No timeout errors
- [ ] Transcription is complete

### Test 10.2: Empty/Silent Audio
Upload silent audio file

**Expected:**
- [ ] Transcription returns empty or minimal text
- [ ] No errors

### Test 10.3: Corrupt Audio File
Upload corrupt/invalid audio file

**Expected:**
- [ ] Clear error message
- [ ] Job fails gracefully

---

## Notes

- Run automated tests first: `docker exec speakr-dev python /app/tests/test_connector_architecture.py`
- After significant changes, test with all three connectors
- Voice profile tests only work with WhisperX ASR Service
