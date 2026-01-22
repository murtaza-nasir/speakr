---
layout: default
title: WhisperX ASR Setup
parent: Admin Guide
nav_order: 8
---

# WhisperX ASR Service Setup

WhisperX is an advanced ASR (Automatic Speech Recognition) service that provides superior speaker diarization and word-level timestamps compared to standard Whisper implementations. This guide covers setting up WhisperX as an alternative ASR backend for Speakr.

## Overview

**WhisperX Benefits:**

- ✅ Better speaker diarization accuracy (Pyannote.audio 4.0)
- ✅ More precise word-level timestamps
- ✅ Improved multi-speaker handling
- ✅ **Voice profile support** with 256-dimensional speaker embeddings
- ✅ Automatic speaker recognition across recordings
- ✅ Active development and updates
- ✅ Production-ready Docker deployment

**vs. Standard Whisper ASR:**

- Standard: Simple, lightweight, good for single speakers, no voice profiles
- WhisperX: Advanced diarization, voice profiles, better for meetings/conversations

## Prerequisites

### Hardware Requirements

**Minimum:**

- NVIDIA GPU with 8GB+ VRAM (RTX 3060, RTX 2080, etc.)
- 16GB RAM
- 50GB free disk space

**Recommended:**

- NVIDIA GPU with 16GB+ VRAM (RTX 3080, RTX 4080, A100)
- 32GB RAM
- 100GB SSD storage

### Software Requirements

- Docker and Docker Compose
- NVIDIA Container Toolkit
- Hugging Face account with model access

## Quick Start

### 1. Get the WhisperX Service

The WhisperX ASR service is maintained in a separate repository:

```bash
# Clone the WhisperX ASR service
git clone https://github.com/murtaza-nasir/whisperx-asr-service.git
cd whisperx-asr-service
```

### 2. Configure Hugging Face Access

**Complete ALL steps below to enable speaker diarization:**

#### Step 1: Create Account
- Visit: [https://huggingface.co/join](https://huggingface.co/join)
- Sign up with your email

#### Step 2: Accept Model Agreements (CRITICAL - ALL THREE REQUIRED)

You must accept agreements for **all three models** used by the diarization pipeline:

1. **Main diarization model:**
   - [https://huggingface.co/pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)

2. **Segmentation model:**
   - [https://huggingface.co/pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)

3. **Speaker diarization 3.1:**
   - [https://huggingface.co/pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)

For each model:

- Click the **"Agree and access repository"** button
- Fill out form (Company/university: your organization, Use case: "Meeting note taker")
- Submit (approval is instant)

#### Step 3: Generate Access Token
- Visit: [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
- Click **"New token"**
- Name: `whisperx-diarization`
- Permission: **Read**
- Click **"Generate token"**
- Copy the token (starts with `hf_...`)

**⚠️ Important:** You MUST accept the model agreement in Step 2. Without this, you'll get "403 Access Denied" errors even with a valid token.

### 3. Set Up Environment

```bash
# Copy example configuration
cp .env.example .env

# Edit and add your Hugging Face token
nano .env
```

Update `.env`:
```bash
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEVICE=cuda
COMPUTE_TYPE=float16
BATCH_SIZE=16
```

### 4. Deploy the Service

```bash
# Build Docker image
docker compose build

# Start service
docker compose up -d

# Check logs
docker compose logs -f
```

### 5. Test the Service

```bash
# Health check
curl http://localhost:9000/health

# Should return:
{
  "status": "healthy",
  "device": "cuda",
  "loaded_models": []
}
```

## Integration with Speakr

### Same Machine Deployment

If WhisperX is running on the same machine as Speakr:

Update Speakr's `.env` file:

```bash
# Enable ASR endpoint
USE_ASR_ENDPOINT=true

# Point to WhisperX service
ASR_BASE_URL=http://whisperx-asr-api:9000

# Enable voice profile features (speaker embeddings)
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

> **Important:** The `ASR_RETURN_SPEAKER_EMBEDDINGS=true` setting is required to enable voice profile features. This setting is only supported by WhisperX and should not be enabled when using the basic OpenAI Whisper ASR Webservice.

Restart Speakr:

```bash
docker compose restart
```

### Separate GPU Machine Deployment

If WhisperX is on a dedicated GPU server:

**On GPU Machine:**

1. Expose service to network in `docker-compose.yml`:
   ```yaml
   ports:
     - "0.0.0.0:9000:9000"
   ```

2. Configure firewall:
   ```bash
   sudo ufw allow 9000/tcp
   ```

**On Speakr Machine:**

Update Speakr's `.env`:

```bash
USE_ASR_ENDPOINT=true
ASR_BASE_URL=http://GPU_MACHINE_IP:9000
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```

Replace `GPU_MACHINE_IP` with actual IP address.

## Configuration

### Performance Tuning

Edit WhisperX service `.env`:

**High-End GPU (RTX 3080+, A100):**
```bash
BATCH_SIZE=32
COMPUTE_TYPE=float16
```

**Mid-Range GPU (RTX 3060, RTX 2080):**
```bash
BATCH_SIZE=16
COMPUTE_TYPE=float16
```

**Low-End GPU (GTX 1660, RTX 2060):**
```bash
BATCH_SIZE=8
COMPUTE_TYPE=int8
```

### Model Selection

Models are selected per-recording in Speakr. Available options:

| Model | Quality | Speed | VRAM Required |
|-------|---------|-------|---------------|
| tiny | Low | Fastest | 1GB |
| base | Low | Very Fast | 1GB |
| small | Medium | Fast | 2GB |
| medium | Good | Moderate | 5GB |
| large-v2 | Excellent | Slow | 10GB |
| large-v3 | Best | Slow | 10GB |

**Recommendation:** Use `large-v3` for best quality, `small` for speed.

## Speaker Diarization

WhisperX provides superior speaker diarization compared to standard implementations.

### Settings in Speakr

When uploading or processing recordings:

- **Min Speakers:** Minimum expected number of speakers
- **Max Speakers:** Maximum expected number of speakers
- Leave blank for automatic detection

**Tips:**
- Set `min_speakers=2` and `max_speakers=6` for typical meetings
- For interviews: `min_speakers=2`, `max_speakers=2`
- For panels: `min_speakers=3`, `max_speakers=8`

### After Transcription

Use Speakr's speaker identification feature to assign real names to detected speakers. WhisperX's improved diarization makes this more accurate.

## Monitoring

### Check Service Health

```bash
# Container status
cd /path/to/whisperx-asr-service
docker compose ps

# View logs
docker compose logs -f

# Check GPU usage
nvidia-smi -l 1
```

### Performance Metrics

Monitor in Speakr's admin interface:

- Transcription times
- Error rates
- Model usage statistics

## Troubleshooting

### WhisperX Service Won't Start

**Check logs:**
```bash
docker compose logs whisperx-asr
```

**Common issues:**

- GPU not accessible: Verify `nvidia-smi` works
- Invalid HF_TOKEN: Check token and model agreements
- Port conflict: Change port in `docker-compose.yml`

### Speakr Can't Connect

**Test connectivity:**
```bash
curl http://ASR_BASE_URL/health
```

**Solutions:**

- Verify firewall rules
- Check network connectivity
- Ensure service is running
- Try IP address instead of hostname

### Slow Processing

**Solutions:**

- Increase `BATCH_SIZE` (if GPU has memory)
- Use smaller model (`small` instead of `large-v3`)
- Disable diarization for faster processing
- Check GPU usage with `nvidia-smi`

### Out of Memory

**Error:** `CUDA out of memory`

**Solutions:**

1. Reduce `BATCH_SIZE`: Set to `8` or `4`
2. Use smaller model
3. Use `COMPUTE_TYPE=int8`
4. Close other GPU applications

### Speaker Diarization Fails

**Check:**

1. HF_TOKEN is set correctly in `.env`
2. Accepted pyannote model agreements
3. Service has internet access (for first-time model download)

**Solutions:**

- Regenerate HF token
- Accept model agreements again
- Check logs for specific errors

## Upgrading

### Update WhisperX Service

```bash
cd /path/to/whisperx-asr-service

# Pull latest changes (if using Git)
git pull

# Rebuild image
docker compose build --no-cache

# Restart service
docker compose up -d
```

### Update Models

Models are cached automatically. To use newer models:

```bash
# Remove cache volume
docker compose down -v

# Restart (models will re-download)
docker compose up -d
```

## Performance Benchmarks

Tested on RTX 3080 (10GB VRAM):

| Model | 10min Audio | Processing Time | Real-time Factor |
|-------|-------------|-----------------|------------------|
| small | 10 min | 45 sec | 13x |
| medium | 10 min | 90 sec | 6.7x |
| large-v3 | 10 min | 180 sec | 3.3x |

*With diarization: add ~30% processing time*

## Comparison: WhisperX vs Standard Whisper

| Feature | Standard Whisper | WhisperX |
|---------|-----------------|----------|
| Transcription Quality | Excellent | Excellent |
| Word Timestamps | Good | Excellent |
| Speaker Diarization | Good | Excellent |
| Voice Profiles | ❌ Not supported | ✅ 256-dim embeddings |
| Speaker Recognition | ❌ Manual only | ✅ Automatic matching |
| Setup Complexity | Low | Medium |
| Resource Usage | Lower | Higher |
| Active Development | Moderate | High |
| Production Ready | Yes | Yes |

> **Note:** To enable voice profile features with WhisperX, you must set `ASR_RETURN_SPEAKER_EMBEDDINGS=true` in Speakr's `.env` file. This setting is disabled by default for compatibility with the basic ASR webservice.

## Getting Help

- **Service Documentation:** See WhisperX service README
- **WhisperX Issues:** [GitHub](https://github.com/m-bain/whisperX/issues)
- **Speakr Integration:** Check Speakr logs and admin dashboard

---

For detailed setup instructions, see the [WhisperX Service Setup Guide](https://github.com/murtaza-nasir/whisperx-asr-service/blob/main/SETUP_GUIDE.md).
