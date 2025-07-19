# Speakr Deployment Guide

This guide covers different deployment methods for Speakr, from simple setups to advanced configurations with ASR endpoints and various AI providers.

## Prerequisites

- Docker and Docker Compose installed
- Basic understanding of environment variables
- API keys for your chosen AI providers

## API Endpoint Requirements

Before setting up Speakr, it's important to understand what API endpoints your services must provide. Speakr integrates with (locally hosted or cloud based) external APIs for transcription and text generation using specific endpoint formats.

### Transcription Services

**Standard Whisper API Method:**
- **Endpoint:** `/audio/transcriptions` 
- **Method:** POST
- **Format:** OpenAI Whisper API compatible
- **Used for:** Audio transcription
- **When:** When `USE_ASR_ENDPOINT=false` (default)

**_Common providers using this method:_**
- **OpenAI Whisper API** (`https://api.openai.com/v1`) - The original OpenAI service
- **OpenRouter** (`https://openrouter.ai/api/v1`) - Multi-provider API gateway
- **Local APIs** (`http://localhost:1234/v1`) - Self-hosted solutions like LM Studio, Ollama, or custom deployments
- **Other API providers** - Any service implementing the OpenAI Whisper API format

**ASR Webservice Method:**
- **Endpoint:** `/asr`
- **Method:** POST  
- **Format:** Custom ASR webservice format
- **Used for:** Audio transcription with speaker diarization support
- **When:** When `USE_ASR_ENDPOINT=true`

**_Recommended provider:_**
- **onerahmet/openai-whisper-asr-webservice** - Docker image that provides the `/asr` endpoint with WhisperX engine support for speaker diarization

### Text Generation Services

**Chat Completions API:**
- **Endpoint:** `/chat/completions`
- **Method:** POST
- **Format:** OpenAI Chat Completions API compatible
- **Used for:** 
  - AI-generated titles and summaries
  - Interactive chat with transcriptions
  - Automatic speaker identification
  - Summary reprocessing
- **When:** Always (for all text generation features, e.g., title generation, summarization, chat, etc.)

**Common providers using this method:**
- **OpenAI** (`https://api.openai.com/v1`) - GPT models
- **OpenRouter** (`https://openrouter.ai/api/v1`) - Access to multiple LLM providers
- **Local APIs** (`http://localhost:1234/v1`) - Self-hosted solutions like LM Studio, Ollama, vLLM, or custom deployments
- **Other API providers** - Anthropic, Google, Azure OpenAI, or any service implementing the OpenAI Chat Completions format

**Example API Base URLs:**
- OpenAI: `https://api.openai.com/v1`
- OpenRouter: `https://openrouter.ai/api/v1`
- Local APIs: `http://localhost:1234/v1`

## Quick Start (Recommended)

The fastest way to get started is using the pre-built Docker image with a simple configuration.

### Method 1: Using Pre-built Docker Image (Recommended)

You don't need to clone the repository for this method.

1. **Create a project directory:**
```bash
mkdir speakr-deployment
cd speakr-deployment
```

2. **Create docker-compose.yml:**
```yaml
services:
  app:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
```

3. **Create your .env file** (see configuration sections below)

4. **Start the application:**
```bash
docker compose up -d
```

### Method 2: Building from Source

If you want to modify the code or build locally:

1. **Clone the repository:**
```bash
git clone https://github.com/murtaza-nasir/speakr.git
cd speakr
```

2. **Use the build-enabled compose file:**
```yaml
services:
  app:
    build: .
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
```

3. **Build and start:**
```bash
docker compose up -d --build
```

## Configuration Options

### 1. Standard Whisper API Method (Simple Setup)

This method uses the `/audio/transcriptions` endpoint from OpenAI's Whisper API or compatible services. It's the simplest to set up but doesn't support speaker diarization. See the [API Endpoint Requirements](#api-endpoint-requirements) section above for compatible providers.

**Create .env file:**
```env
# --- Text Generation Model (uses /chat/completions endpoint) ---
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=your_openrouter_api_key_here
TEXT_MODEL_NAME=openai/gpt-4o-mini

# --- Transcription Service (uses /audio/transcriptions endpoint) ---
TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
TRANSCRIPTION_API_KEY=your_openai_api_key_here
WHISPER_MODEL=whisper-1

# --- Application Settings ---
ALLOW_REGISTRATION=false
SUMMARY_MAX_TOKENS=8000
CHAT_MAX_TOKENS=5000

# --- Admin User (created on first run) ---
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your_secure_password_here

# --- Docker Settings (rarely need to be changed) ---
SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
UPLOAD_FOLDER=/data/uploads
```

### 2. ASR Webservice Method (Advanced Setup)

This method uses the `/asr` endpoint and enables speaker identification and diarization but requires running a separate ASR service. See the [API Endpoint Requirements](#api-endpoint-requirements) section above for details on the `/asr` endpoint format.

#### Option 2.1: Single Docker Compose File

**docker-compose.yml:**
```yaml
services:
  # ASR Webservice
  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr-webservice
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=distil-large-v3
      - ASR_COMPUTE_TYPE=int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=your_huggingface_token_here  # Required for diarization models
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
              device_ids: ["0"]  # Adjust GPU ID as needed
    restart: unless-stopped
    networks:
      - speakr-network

  # Speakr Application
  speakr:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
    depends_on:
      - whisper-asr
    networks:
      - speakr-network

networks:
  speakr-network:
    driver: bridge
```

**Create .env file for ASR setup:**
```env
# --- Text Generation Model (for summaries, titles, etc.) ---
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=your_openrouter_api_key_here
TEXT_MODEL_NAME=openai/gpt-4o-mini

# --- Transcription Service (ASR Endpoint) ---
USE_ASR_ENDPOINT=true
ASR_BASE_URL=http://whisper-asr:9000
ASR_DIARIZE=true
ASR_MIN_SPEAKERS=1
ASR_MAX_SPEAKERS=5

# --- Application Settings ---
ALLOW_REGISTRATION=false
SUMMARY_MAX_TOKENS=8000
CHAT_MAX_TOKENS=5000

# --- Admin User (created on first run) ---
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your_secure_password_here

# --- Docker Settings ---
SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
UPLOAD_FOLDER=/data/uploads
```

#### Option 2.2: Separate Docker Compose Files

If you want to run ASR and Speakr on different machines or manage them separately:

**whisper-asr-compose.yml** (for ASR service):
```yaml
services:
  whisper-asr-webservice:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr-webservice
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=distil-large-v3
      - ASR_COMPUTE_TYPE=int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=your_huggingface_token_here
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
              device_ids: ["0"]
    restart: unless-stopped
```

**speakr-compose.yml** (for Speakr app):
```yaml
services:
  app:
    image: learnedmachine/speakr:latest
    container_name: speakr
    restart: unless-stopped
    ports:
      - "8899:8899"
    env_file:
      - .env
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
```

**Update .env for separate setup:**
```env
# Change ASR_BASE_URL to point to the actual IP/hostname of ASR service
ASR_BASE_URL=http://192.168.1.100:9000  # Replace with actual IP of the above ASR container
# ... rest of configuration same as above
```

**Start services separately:**
```bash
# On ASR machine
docker compose -f whisper-asr-compose.yml up -d

# On Speakr machine  
docker compose -f speakr-compose.yml up -d
```

## AI Provider Configurations

**Note:** Thinking models (like OpenAI's o1 series) are not currently supported. Please use standard chat completion models.

### OpenRouter (Recommended for variety)

```env
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=sk-or-v1-your_key_here
TEXT_MODEL_NAME=openai/gpt-4o-mini
# Recommended options: qwen/qwen3-32b, qwen/qwen3-30b-a3b-04-28, deepseek/deepseek-chat-v3-0324, etc.
```

**Setup:**
1. Visit [OpenRouter](https://openrouter.ai/)
2. Create account and get API key
3. Add credits to your account
4. Choose from 100+ models

### OpenAI

```env
TEXT_MODEL_BASE_URL=https://api.openai.com/v1
TEXT_MODEL_API_KEY=sk-proj-your_key_here
TEXT_MODEL_NAME=gpt-4o-mini
# Other options: gpt-4o, gpt-3.5-turbo
```

**Setup:**
1. Visit [OpenAI Platform](https://platform.openai.com/)
2. Create API key
3. Add billing information

### Google Gemini

```env
TEXT_MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta
TEXT_MODEL_API_KEY=your_gemini_api_key_here
TEXT_MODEL_NAME=gemini-2.5-flash
# Other options: gemini-2.5-pro
```

**Setup:**
1. Visit [Google AI Studio](https://aistudio.google.com/)
2. Create API key
3. Enable Gemini API

### Local OpenAI-Compatible API

For local models using Ollama, LM Studio, or similar:

```env
TEXT_MODEL_BASE_URL=http://localhost:11434/v1  # Ollama default
# or http://localhost:1234/v1 for LM Studio
TEXT_MODEL_API_KEY=not-needed  # Usually not required for local
TEXT_MODEL_NAME=localmodel
# Model name depends on what you have installed locally
```

**Setup for Ollama:**
1. Install [Ollama](https://ollama.ai/)
2. Pull a model: `ollama pull llama3.1:8b`
3. Ensure Ollama is running: `ollama serve`

**Setup for LM Studio:**
1. Install [LM Studio](https://lmstudio.ai/)
2. Download a model through the UI
3. Start local server in LM Studio

## Transcription Service Options

### OpenAI Whisper API

```env
TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
TRANSCRIPTION_API_KEY=sk-proj-your_key_here
WHISPER_MODEL=whisper-1
```

### Local Whisper API Compatible

```env
TRANSCRIPTION_BASE_URL=http://localhost:8000/v1
TRANSCRIPTION_API_KEY=not-needed
WHISPER_MODEL=whisper-1
```

### ASR Endpoint (for Speaker Diarization)

```env
USE_ASR_ENDPOINT=true
ASR_BASE_URL=http://localhost:9000  # or remote IP
ASR_DIARIZE=true
ASR_MIN_SPEAKERS=1
ASR_MAX_SPEAKERS=5
```

## Advanced ASR Configuration

### Important Prerequisites for Diarization

**Hugging Face Token Setup:**
1. Create account at [Hugging Face](https://huggingface.co/)
2. Generate an access token in your [settings](https://huggingface.co/settings/tokens)
3. **Accept terms for required models:**
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   
   **Note:** These are gated models requiring approval. Visit each model page and request access by sharing your information.

4. Use your token in the `HF_TOKEN` environment variable

**Required for Diarization:**
- `ASR_ENGINE=whisperx` (only WhisperX supports diarization)
- Valid Hugging Face token with access to pyannote models
- GPU recommended (CPU inference impractical for larger files)

### GPU vs CPU

**GPU (Strongly Recommended):**
```yaml
whisper-asr:
  image: onerahmet/openai-whisper-asr-webservice:latest-gpu
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            capabilities: [gpu]
            device_ids: ["0"]
```

**CPU (Limited Use - Not Recommended for Large Files):**
```yaml
whisper-asr:
  image: onerahmet/openai-whisper-asr-webservice:latest-cpu
  # No GPU configuration needed
  # WARNING: CPU inference is impractical for larger audio files
```

**Performance Note:** CPU inference will be extremely slow, especially for files larger than a few minutes. GPU is strongly recommended for any practical use.

### Model Selection

| Model | Size | Speed | Accuracy | Memory | Notes |
|-------|------|-------|----------|---------|-------|
| `tiny` | 39 MB | Fastest | Lowest | ~1 GB | English: `tiny.en` |
| `base` | 74 MB | Fast | Good | ~1 GB | English: `base.en` |
| `small` | 244 MB | Medium | Better | ~2 GB | English: `small.en` |
| `medium` | 769 MB | Slow | Good | ~5 GB | English: `medium.en` |
| `large-v1` | 1550 MB | Slower | Best | ~10 GB | |
| `large-v2` | 1550 MB | Slower | Best | ~10 GB | |
| `large-v3` | 1550 MB | Slower | Best | ~10 GB | |
| `large-v3-turbo` | 809 MB | Fast | Excellent | ~6 GB | Also: `turbo` |
| `distil-large-v2` | 756 MB | Medium | Excellent | ~6 GB | Distilled model |
| `distil-large-v3` | 756 MB | Medium | Excellent | ~6 GB | **Recommended** |
| `distil-medium.en` | 394 MB | Fast | Good | ~3 GB | English only |
| `distil-small.en` | 166 MB | Faster | Good | ~2 GB | English only |

**Recommendations:**
- **General use:** `distil-large-v3` - best balance of speed and accuracy
- **English only:** `.en` models perform better for English-only applications
- **Speed priority:** `distil-small.en` or `base.en`
- **Accuracy priority:** `large-v3` or `large-v3-turbo`

**Source:** [OpenAI Whisper Models](https://github.com/openai/whisper)

### Compute Types

- `int8`: Fastest, lowest memory, slight accuracy loss (default for CPU)
- `float16`: Good balance, GPU only (default for GPU)
- `float32`: Highest accuracy, most memory

### Model Caching

To avoid downloading models on every container restart:

```yaml
whisper-asr:
  image: onerahmet/openai-whisper-asr-webservice:latest-gpu
  volumes:
    - ./whisper-cache:/root/.cache  # Persist model cache
  environment:
    - ASR_MODEL_PATH=/root/.cache/whisper  # Optional: custom model path
```

**Note:** Using persistent cache prevents automatic model updates.

### Additional ASR Configuration

**Environment Variables:**
```yaml
environment:
  - ASR_MODEL=distil-large-v3
  - ASR_ENGINE=whisperx              # Required for diarization
  - ASR_DEVICE=cuda                  # or 'cpu'
  - ASR_QUANTIZATION=float16         # or 'float32', 'int8'
  - MODEL_IDLE_TIMEOUT=300           # Unload model after 300s of inactivity (0=never)
  - SAMPLE_RATE=16000                # Default sample rate
  - HF_TOKEN=your_token_here         # Required for diarization models
```
**Documentation:** [Whisper ASR Webservice](https://ahmetoner.com/whisper-asr-webservice/)

## Troubleshooting

### Common Issues

**1. ASR Service Not Accessible**
```bash
# Test ASR connectivity (note: no /health endpoint available)
curl http://localhost:9000/docs  # Check Swagger API docs
```

**2. GPU Not Detected**
```bash
# Check NVIDIA Docker runtime
docker run --rm --gpus all nvidia/cuda:11.0-base nvidia-smi
```

**3. Permission Issues with Volumes**
```bash
# Fix volume permissions
sudo chown -R 1000:1000 ./uploads ./instance
```

**4. Diarization Model Access Issues**
```bash
# Check ASR container logs for model download errors
docker logs whisper-asr-webservice

# Look for errors like:
# "Repository not found" or "Access denied"
# This indicates you need to accept terms for pyannote models
```

**5. Out of Memory Errors**
- Use smaller model (e.g., `distil-large-v3` instead of `large-v3`)
- Use `int8` compute type
- Reduce `ASR_MAX_SPEAKERS`
- Enable model unloading: `MODEL_IDLE_TIMEOUT=300`

### Logs and Debugging

```bash
# View Speakr logs
docker logs speakr

# View ASR logs (important for diarization troubleshooting)
docker logs whisper-asr-webservice

# Follow logs in real-time
docker logs -f whisper-asr-webservice

# Check container status
docker compose ps
```

**What to look for in ASR logs:**
- Model download progress
- Pyannote model access errors
- GPU detection status
- Memory usage warnings
- API request processing

## Security Considerations

### Production Deployment

1. **Change default passwords:**
```env
ADMIN_PASSWORD=your_very_secure_password_here
```

2. **Use environment-specific URLs:**
```env
# Don't use localhost in production
ASR_BASE_URL=http://your-asr-server.internal:9000
```

3. **Secure API keys:**
- Use Docker secrets or external secret management
- Never commit .env files to version control

4. **Network security:**
```yaml
# Use internal networks
networks:
  speakr-internal:
    driver: bridge
    internal: true  # No external access
```

5. **Reverse proxy setup:**
```yaml
# Add nginx or traefik for HTTPS
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.speakr.rule=Host(`speakr.yourdomain.com`)"
  - "traefik.http.routers.speakr.tls.certresolver=letsencrypt"
```

## Performance Optimization

### For High Volume Usage

1. **Use faster models:**
```env
ASR_MODEL=distil-large-v3  # Instead of large-v3
ASR_COMPUTE_TYPE=int8      # Instead of float16
```

2. **Optimize Docker resources:**
```yaml
deploy:
  resources:
    limits:
      memory: 8G
      cpus: '4'
```

3. **Use SSD storage for volumes:**
```yaml
volumes:
  - type: bind
    source: /fast-ssd/speakr/uploads
    target: /data/uploads
```

## Backup and Maintenance

### Backup Important Data

```bash
# Backup database and uploads
tar -czf speakr-backup-$(date +%Y%m%d).tar.gz ./instance ./uploads

# Restore from backup
tar -xzf speakr-backup-20241219.tar.gz
```

### Updates

```bash
# Update to latest version
docker compose pull
docker compose up -d
```

### Database Maintenance

```bash
# Access database for maintenance
docker exec -it speakr sqlite3 /data/instance/transcriptions.db
```

## Browser Recording Feature

Speakr includes a powerful in-browser recording feature on the "New Recording" screen, allowing you to capture audio directly without external software.

### Recording Modes

- **Microphone:** Records audio from your microphone. This is ideal for dictating notes or recording your side of a conversation.
- **System Audio:** Captures all audio playing from your computer, such as online meetings (Zoom, Teams), videos, or other applications.
- **Both:** Reliably records both your microphone and system audio simultaneously into a single audio track. This is the recommended mode for capturing online meetings where you are an active participant.

### Important Requirements for System Audio

The ability to record **system audio** (and therefore "both") relies on the browser's Screen Capture API. For security reasons, this API has strict requirements:

- **HTTPS is Mandatory:** You **must** access Speakr through a secure `https://` connection. This feature will not work over `http://`, with the exception of `http://localhost`.
- **User Permission:** When you start a system audio recording, your browser will prompt you to share your screen. You must select a screen or tab to share and **ensure you check the "Share system audio" (or similar) checkbox to grant permission.** Recording audio from a single application window is often not supported.

### Local Development Setup (HTTP)

For local development without setting up SSL, most browsers will block the system audio recording feature. To enable it for testing on `http://localhost`:

#### Google Chrome (Recommended)

1. **Open Chrome**
2. Go to: `chrome://flags`
3. Search for: `insecure origin`
4. Find: **"Insecure origins treated as secure"**
5. In the text box, enter your site URL (e.g., `http://localhost:8899` or `http://your-server-ip:8899`)
6. Set the dropdown to **Enabled**
7. Click **Relaunch** to restart Chrome

#### Mozilla Firefox: Allow Microphone on HTTP Sites

1. **Open Firefox**
2. Go to: `about:config`
3. Click **"Accept the Risk and Continue"**
4. Search for: `media.devices.insecure.enabled`
5. Double-click to change from `false` to `true`
6. Restart Firefox

**Note on Firefox System Audio:** While the above setting enables microphone recording on HTTP, Firefox's security model is very strict regarding system audio. As of recent tests, capturing system audio (and therefore "Both" mode) does not work reliably in Firefox even with SSL. **For system audio recording, Chrome is the recommended browser.**

> **⚠️ Security Warning:** Modifying browser flags reduces security. Only use these settings for local development and testing. For production deployments, **always use HTTPS with a valid SSL certificate.**

### Production Deployment with SSL

For production use, it is essential to deploy Speakr behind a reverse proxy that handles SSL/TLS termination. This ensures the application is served over HTTPS, enabling all recording features securely.

**Example with Nginx:**
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    
    ssl_certificate /path/to/your/certificate.crt;
    ssl_certificate_key /path/to/your/private.key;
    
    location / {
        proxy_pass http://localhost:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Example with Traefik:**
```yaml
# In your docker-compose.yml for the Speakr service
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.speakr.rule=Host(`your-domain.com`)"
  - "traefik.http.routers.speakr.tls.certresolver=letsencrypt"
  - "traefik.http.services.speakr.loadbalancer.server.port=8899"
```

### Recording Workflow

1.  **Access Recording:** Go to the "New Recording" screen.
2.  **Choose Mode:** Select "Microphone", "System Audio", or "Both".
3.  **Grant Permissions:** 
    - For **Microphone**, allow access when prompted.
    - For **System Audio** or **Both**, select a screen or tab to share and **ensure you check the "Share system audio" box**.
4.  **Record:** The visualizer(s) will confirm that audio is being captured.
5.  **Stop & Upload:** Stop the recording when finished and click "Upload" to process it.

This guide should cover all the deployment scenarios you need. Choose the method that best fits your infrastructure and requirements!
