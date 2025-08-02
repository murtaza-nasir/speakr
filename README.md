<div align="center">
    <img src="static/img/icon-32x32.png" alt="Speakr Logo" width="32"/>
</div>

<h1 align="center">Speakr</h1>
<p align="center">Self-hosted, intelligent note-taking for meetings and recordings</p>

<p align="center">
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img alt="AGPL v3" src="https://img.shields.io/badge/License-AGPL_v3-blue.svg"></a>
  <a href="https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml"><img alt="Docker Build" src="https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml/badge.svg"></a>
</p>

> Speakr is an intelligent, self-hosted web application that transforms your audio recordings into organized, searchable, and insightful notes. By running on your own server, it ensures your sensitive conversations and data remain completely private.

> Designed for a wide range of uses, Speakr is trusted by professionals for meeting minutes, by therapists for session notes, by students for lecture capture, and even for transcribing D&D sessions. It automatically transcribes audio with speaker identification, generates concise summaries, and provides an AI chat interface to interact with your content.

<div align="center">
    <img src="static/img/main.png" alt="Speakr Main Interface" width="750"/>
</div>

---

## What's New?

### Latest Release (Version 0.4.2)
* **Large File Chunking Support:** Automatically splits large audio files to work with transcription services that have file size limits (e.g., OpenAI's 25MB limit).
* **Optimized File Processing:** Improved efficiency by minimizing file conversions and using compressed formats.
* **Enhanced Security:** Strengthened CSRF protection and fixed session timeout issues.
* **Improved Recording Reliability:** Addressed several bugs related to in-browser recording.

<details>
<summary><strong>Previous Version History</strong></summary>

### Version 0.4.1 
* **Secure Sharing System:** Share transcriptions via public links with customizable permissions.
* **Enhanced Recording & Note-Taking:** Redesigned recording interface with a real-time notepad.
* **Advanced Speaker Diarization:** AI-powered speaker detection and saved speaker profiles.
* **"Black hole" Directory:** Feature for automatic, hands-free audio file processing.
* **Transcript Editing:** Manually edit and correct transcriptions.
* **Clickable Timestamps:** Navigate audio by clicking timestamps in the transcript.
* **Streaming Chat Responses:** More interactive and responsive AI chat.

</details>

---
## Screenshots


<div style="display: flex; justify-content: center; gap: 10px; flex-wrap: wrap;">
  <img src="static/img/main2.png" alt="Main" style="max-width: 48%; height: auto;"/>
  <img src="static/img/multilingual-support.png" alt="Multilingual" style="max-width: 48%; height: auto;"/>
</div>

---
<details><summary><strong>Transcription & chat</strong></summary>
<div style="display: flex; justify-content: center;">
  <figure>
    <img src="static/img/transcription-chat-bubble-view.png" alt="Transcription and Chat" width="400"/>
    <figcaption style="text-align: center;">Integrated Chat</figcaption>
  </figure>
</div>

<details><summary><strong>Light & dark</strong></summary>
<div style="display: flex; justify-content: space-around;">
  <figure>
    <img src="static/img/light-mode.png" alt="Light Mode" width="400"/>
    <figcaption style="text-align: center;">Light</figcaption>
  </figure>
  <figure>
    <img src="static/img/dark-mode.png" alt="Dark Mode" width="400"/>
    <figcaption style="text-align: center;">Dark</figcaption>
  </figure>
</div>
</details>
</details>

<details><summary><strong>Transcription views</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/simple-transcription-view.png" alt="Simple View" width="400"/>
    <figcaption style="text-align: center;">Simple</figcaption>
  </figure>
  <figure>
    <img src="static/img/transcription-bubble-view.png" alt="Bubble View" width="400"/>
    <figcaption style="text-align: center;">Bubble</figcaption>
  </figure>
</div>
</details>

<details><summary><strong>Speaker identification</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/intuitive-speaker-identification.png" alt="AI-assisted" width="400"/>
    <figcaption style="text-align: center;">AI-assisted</figcaption>
  </figure>
  <figure>
    <img src="static/img/manual-auto-speaker-identification.png" alt="Manual & Auto" width="400"/>
    <figcaption style="text-align: center;">Manual & Auto</figcaption>
  </figure>
  <figure>
    <img src="static/img/speaker-suggestions.png" alt="Suggestions" width="400"/>
    <figcaption style="text-align: center;">Saved Suggestions</figcaption>
  </figure>
</div>
</details>

<details><summary><strong>Recordings & notes</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/rec1.png" alt="Options" width="400"/>
    <figcaption style="text-align: center;">Recording Options</figcaption>
  </figure>
  <figure>
    <img src="static/img/rec2.png" alt="Mic" width="400"/>
    <figcaption style="text-align: center;">Mic/System Audio</figcaption>
  </figure>
  <figure>
    <img src="static/img/rec3.png" alt="Both" width="400"/>
    <figcaption style="text-align: center;">Mic + System Audio</figcaption>
  </figure>
</div>
</details>

</details>

---

## Core Features

* **Self-Hosted and Private:** Keep full control over your data by hosting Speakr on your own server.
* **Advanced Transcription & Diarization:** Get accurate transcripts with optional AI-powered speaker identification (diarization) to know who said what.
* **AI-Powered Insights:** Automatically generate titles and summaries for your recordings. Use the integrated chat to ask questions and pull insights directly from the transcript.
* **Install as a PWA App:** Install on your phone for quick and easy recordings and note capture. 
* **Versatile Recording & Upload:** Upload existing audio files or record directly in the browser or PWA app. Capture audio from your microphone, your system's audio (e.g., for an online meeting), or both simultaneously.
* **Automated Processing:** Designate a "black hole" directory for drag-and-drop batch processing of audio files.
* **Secure Sharing:** Create shareable links for your transcripts with granular controls, allowing you to include or exclude summaries and notes.
* **Customizable AI:** Configure the specific AI models, API endpoints (compatible with OpenAI, OpenRouter, local models), and custom prompts for summarization and chat.
* **Multi-User Support:** Includes a complete user management system with an admin dashboard.

<div style="display: flex; justify-content: center; gap: 10px; flex-wrap: wrap;">
    <img src="static/img/rec1.png" alt="Transcription and Chat" style="max-width: 25%; height: auto;"/>
    <img src="static/img/rec3.png" alt="Speaker Identification" style="max-width: 25%; height: auto;"/>
    <img src="static/img/intuitive-speaker-identification.png" alt="Speaker Identification" style="max-width: 48%; height: auto;"/>
</div>

---

## Getting Started

The recommended setup method uses Docker, which is simple and fast.

<details>
<summary><strong>Easy Setup: Docker Compose (Recommended)</strong></summary>

You only need Docker installed for this method; you do not need to clone the repository.

1.  **Create `docker-compose.yml`**
    Create a file named `docker-compose.yml` and add the following content:
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

2.  **Create Configuration (`.env`) File**
    Create a file named `.env` in the same directory. Your configuration will depend on whether you need speaker identification (diarization).

    * **Option A: Standard Whisper API (No Speaker Diarization)**
        This is the simplest method and works with any OpenAI Whisper-compatible API (like OpenAI, OpenRouter, or local LLMs).

        ```dotenv
        # --- Text Generation Model (uses /chat/completions endpoint) ---
        TEXT_MODEL_BASE_URL=[https://openrouter.ai/api/v1](https://openrouter.ai/api/v1)
        TEXT_MODEL_API_KEY=your_openrouter_api_key
        TEXT_MODEL_NAME=openai/gpt-4o-mini

        # --- Transcription Service (uses /audio/transcriptions endpoint) ---
        TRANSCRIPTION_BASE_URL=[https://api.openai.com/v1](https://api.openai.com/v1)
        TRANSCRIPTION_API_KEY=your_openai_api_key
        WHISPER_MODEL=whisper-1
        
        # --- Large File Chunking (for endpoints with file size limits) ---
        ENABLE_CHUNKING=true
        CHUNK_SIZE_MB=20

        # --- Application Settings ---
        ALLOW_REGISTRATION=false
        ADMIN_USERNAME=admin
        ADMIN_EMAIL=admin@example.com
        ADMIN_PASSWORD=changeme
        
        # --- Docker Settings ---
        SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
        UPLOAD_FOLDER=/data/uploads
        ```

    * **Option B: ASR Webservice (With Speaker Diarization)**
        This method enables speaker identification but requires running a separate ASR webservice container. See the **Advanced Configuration** section below for details on setting up the ASR service.

        ```dotenv
        # --- Text Generation Model (uses /chat/completions endpoint) ---
        TEXT_MODEL_BASE_URL=[https://openrouter.ai/api/v1](https://openrouter.ai/api/v1)
        TEXT_MODEL_API_KEY=your_openrouter_api_key
        TEXT_MODEL_NAME=openai/gpt-4o-mini

        # --- Transcription Service (uses /asr endpoint) ---
        USE_ASR_ENDPOINT=true
        ASR_BASE_URL=http://your_asr_host:9000  # URL of your running ASR webservice
        ASR_DIARIZE=true
        ASR_MIN_SPEAKERS=1
        ASR_MAX_SPEAKERS=5

        # --- Application Settings ---
        ALLOW_REGISTRATION=false
        ADMIN_USERNAME=admin
        ADMIN_EMAIL=admin@example.com
        ADMIN_PASSWORD=changeme
        
        # --- Docker Settings ---
        SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
        UPLOAD_FOLDER=/data/uploads
        ```

3.  **Start the Application**
    After editing your `.env` file with your API keys and settings, run the following command:
    ```bash
    docker compose up -d
    ```
    Access the application at `http://localhost:8899`. The admin user will be created on the first run.

</details>

<details>
<summary><strong>Advanced Setup: Build from Source</strong></summary>

Follow these steps if you want to modify the code or build the Docker image yourself.

1.  **Clone the Repository:**
    ```bash
    git clone [https://github.com/murtaza-nasir/speakr.git](https://github.com/murtaza-nasir/speakr.git)
    cd speakr
    ```
2.  **Create Configuration Files:**
    Copy the example files. Use `env.whisper.example` for the standard API method or `env.asr.example` for the ASR webservice method.
    ```bash
    cp docker-compose.example.yml docker-compose.yml
    cp env.whisper.example .env # Or cp env.asr.example .env
    ```
    Edit the `.env` file with your custom settings and API keys.

3.  **Build and Start:**
    ```bash
    docker compose up -d --build
    ```
</details>

---

## Usage Guide

1.  **Login:** Access the application (e.g., `http://localhost:8899`) and log in. The admin account is created from the `.env` variables on the first launch.
2.  **Set Preferences (Recommended):** Navigate to your **Account** page to set your default language, customize the AI summarization prompt, and add professional context to improve chat results.
3.  **Add a Recording:**
    * **Upload:** Drag and drop an audio file onto the dashboard or use the **New Recording** page.
    * **Record:** Use the in-browser recorder. You can record your mic, system audio, or both. **Note:** To capture system audio (e.g., from a meeting), you must share a **browser tab** or your **entire screen** and ensure the **"Share audio"** checkbox is enabled.
    * **Automated:** If enabled, simply drop files into the monitored "black hole" directory.

    <details>
    <summary><strong>Recording Interface Showcase</strong></summary>
    <div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
      <figure>
        <img src="static/img/rec1.png" alt="Recording options" width="400"/>
        <figcaption style="text-align: center;">Recording Options</figcaption>
      </figure>
      <figure>
        <img src="static/img/rec2.png" alt="Record from microphone" width="400"/>
        <figcaption style="text-align: center;">Record from Phone/Computer Microphone</figcaption>
      </figure>
      <figure>
        <img src="static/img/rec3.png" alt="Record microphone and system audio" width="400"/>
        <figcaption style="text-align: center;">Record Mic & System Audio (e.g., Zoom meeting)</figcaption>
      </figure>
    </div>
    </details>

4.  **Interact with Your Transcript:**
    * From the gallery, click a recording to view its details.
    * Read the transcript, listen to the audio, and review the AI-generated summary.
    * Edit metadata like titles and participants.
    * Use the **Chat** panel to ask questions about the content.
5.  **Identify Speakers (Diarization):**
    * If you used the ASR method with diarization enabled, click the **Identify Speakers** button.
    * In the modal, assign names to the detected speakers (e.g., `SPEAKER 00`, `SPEAKER 01`). You can use the **Auto Identify** feature to let the AI suggest names based on the conversation.

---

<details>
<summary><strong>Advanced Configuration & Technical Details</strong></summary>

## API Endpoint Requirements

Speakr integrates with external APIs for transcription and text generation. Here are the exact endpoints each service must implement:

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
- **When:** Always (for all text generation features)

**_Common providers using this method:_**
- **OpenAI** (`https://api.openai.com/v1`) - GPT models
- **OpenRouter** (`https://openrouter.ai/api/v1`) - Access to multiple LLM providers
- **Local APIs** (`http://localhost:1234/v1`) - Self-hosted solutions like LM Studio, Ollama, vLLM, or custom deployments
- **Other API providers** - Anthropic, Google, Azure OpenAI, or any service implementing the OpenAI Chat Completions format

**_Example API Base URLs:_**
- OpenAI: `https://api.openai.com/v1`
- OpenRouter: `https://openrouter.ai/api/v1`
- Local APIs: `http://localhost:1234/v1`

## Setup Instructions

**For detailed deployment instructions, see the [Deployment Guide](DEPLOYMENT_GUIDE.md)**

The recommended method is to use the pre-built Docker image, which is fast and simple. This is explained above. 

### Advanced Setup (Build from Source)

If you want to modify the code or build the Docker image yourself, clone the repository first.

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/murtaza-nasir/speakr.git
    cd speakr
    ```
2.  **Create `docker-compose.yml` and `.env` files:**
    Copy the example files from the repository.
    ```bash
    cp docker-compose.example.yml docker-compose.yml
    
    # For standard API
    cp env.whisper.example .env

    # OR for ASR Webservice
    cp env.asr.example .env
    ```
    Edit the `.env` file with your settings.
3.  **Build and Start:**
    ```bash
    docker compose up -d --build
    ```

### Recommended ASR Webservice Setup

**Speaker Diarization only works with the ASR method and requires the `whisperx` engine.** Here is an example `docker-compose.yml` for running the ASR service itself. You would run this *in addition* to the Speakr app container.

```yaml
services:
  whisper-asr-webservice:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr-webservice
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=distil-large-v3 # or large-v3, medium
      - ASR_COMPUTE_TYPE=float16     # or int8, float32
      - ASR_ENGINE=whisperx        # REQUIRED for diarization
      - HF_TOKEN=your_hugging_face_token # Optional
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
              device_ids: ["0"]
    restart: unless-stopped
```

**A Note on Diarization Accuracy:** For best results, it is often better to set the number of speakers slightly higher (e.g., by 1-2) than the actual number present. You can easily merge speakers later in the speaker identification modal.

Speakr has been tested with the recommended `onerahmet/openai-whisper-asr-webservice` image. Other ASR webservices might work but have not been tested.

**Important for ASR Setup:** Speaker diarization requires a Hugging Face token and accepting terms for gated models (pyannote). If you encounter issues, check the ASR container logs with `docker logs whisper-asr-webservice` for troubleshooting.

**For complete ASR setup instructions, model configurations, troubleshooting, and logs analysis, see the [Deployment Guide](DEPLOYMENT_GUIDE.md#advanced-asr-configuration)**

## Automated File Processing

Speakr includes a powerful "black hole" directory monitoring feature that automatically processes audio files without manual uploads. This is perfect for batch processing scenarios where you want to drop files into a directory and have them automatically transcribed.

### How It Works

1. **File Monitoring:** Speakr monitors a designated directory for new audio files
2. **Automatic Detection:** When new audio files are detected, they are automatically queued for processing
3. **File Stability Check:** Files are checked for stability (not being written to) before processing
4. **Automatic Processing:** Files are moved to the uploads directory and processed using your configured transcription settings
5. **Database Integration:** Processed recordings appear in your gallery with the title "Auto-processed - [filename]"

### Setup Instructions

#### 1. Enable Auto-Processing

Add these environment variables to your `.env` file:

```dotenv
# Enable automated file processing
ENABLE_AUTO_PROCESSING=true

# Directory to monitor (inside container)
AUTO_PROCESS_WATCH_DIR=/data/auto-process

# How often to check for new files (seconds)
AUTO_PROCESS_CHECK_INTERVAL=30

# Processing mode (see modes below)
AUTO_PROCESS_MODE=admin_only
```

#### 2. Mount the Auto-Process Directory

Update your `docker-compose.yml` to mount the auto-process directory:

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
      - ./auto-process:/data/auto-process  # Add this line
```

#### 3. Create the Directory

Create the auto-process directory on your host system:

```bash
mkdir auto-process
```

#### 4. Restart the Container

```bash
docker compose down
docker compose up -d
```

### Processing Modes

Speakr supports three different auto-processing modes:

#### Mode 1: Admin Only (Default)
```dotenv
AUTO_PROCESS_MODE=admin_only
```
- All files in the main auto-process directory are assigned to the admin user
- Simple setup, good for single-user scenarios

#### Mode 2: User Directories
```dotenv
AUTO_PROCESS_MODE=user_directories
```
- Create subdirectories for each user: `auto-process/user123/` or `auto-process/123/`
- Files are automatically assigned to the corresponding user
- Example structure:
  ```
  auto-process/
  ├── user1/          # Files for user ID 1
  ├── user5/          # Files for user ID 5
  └── 123/            # Files for user ID 123
  ```

#### Mode 3: Single User
```dotenv
AUTO_PROCESS_MODE=single_user
AUTO_PROCESS_DEFAULT_USERNAME=john_doe
```
- All files are assigned to a specific username
- Useful when you want all auto-processed files to go to a specific non-admin user

### Supported File Formats

The auto-processor supports the same audio formats as manual uploads:
- **Common formats:** MP3, WAV, M4A, FLAC, AAC, OGG, WebM
- **Mobile formats:** AMR

**File Processing:**
- **Supported formats** (MP3, WAV, FLAC, WebM, M4A, AAC, OGG) are processed directly without conversion
- **Unsupported formats** are automatically converted to 32kbps MP3 using ffmpeg for optimal size/quality balance

### Usage Examples

#### Basic Usage (Admin Mode)
```bash
# Copy files to the auto-process directory
cp /path/to/meeting1.mp3 auto-process/
cp /path/to/meeting2.wav auto-process/

# Files will be automatically processed within 30 seconds (default interval)
```

#### User Directory Mode
```bash
# Create user directories
mkdir -p auto-process/user1
mkdir -p auto-process/user5

# Drop files for specific users
cp meeting_with_john.mp3 auto-process/user1/
cp team_standup.wav auto-process/user5/
```

#### Batch Processing
```bash
# Process multiple files at once
cp /recordings/*.mp3 auto-process/
cp /meetings/2024-01/*.wav auto-process/
```

### Monitoring and Logs

Check the container logs to monitor auto-processing activity:

```bash
docker logs speakr -f
```

You'll see log entries like:
```
File monitor started in 'admin_only' mode, watching: /data/auto-process
Found new audio file for user 1: /data/auto-process/meeting.mp3
Copied /data/auto-process/meeting.mp3 to /data/uploads/auto_20250119021530_meeting.mp3
Created recording record with ID: 123 for user: admin
Started background processing for recording ID: 123
```

</details>

-----

## License

This project is **dual-licensed**:

1.  **GNU Affero General Public License v3.0 (AGPLv3)**
    [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

    Speakr is offered under the AGPLv3 as its open-source license. You are free to use, modify, and distribute this software under the terms of the AGPLv3. A key condition of the AGPLv3 is that if you run a modified version on a network server and provide access to it for others, you must also make the source code of your modified version available to those users under the AGPLv3.

    * You **must** create a file named `LICENSE` (or `COPYING`) in the root of your repository and paste the full text of the [GNU AGPLv3 license](https://www.gnu.org/licenses/agpl-3.0.txt) into it.
    * Read the full license text carefully to understand your rights and obligations.

2.  **Commercial License**

    For users or organizations who cannot or do not wish to comply with the terms of the AGPLv3 (for example, if you want to integrate Speakr into a proprietary commercial product or service without being obligated to share your modifications under AGPLv3), a separate commercial license is available.

    Please contact **speakr maintainers** for details on obtaining a commercial license.

**You must choose one of these licenses** under which to use, modify, or distribute this software. If you are using or distributing the software without a commercial license agreement, you must adhere to the terms of the AGPLv3.

## Roadmap

Speakr is in active development. Planned features include a faster way to switch transcription languages on the fly.

## Contributing

Feedback, bug reports, and feature suggestions are highly encouraged\! Please open an issue on the GitHub repository to share your thoughts.

**Note on Code Contributions:** Should the project begin formally accepting external code contributions, a Contributor License Agreement (CLA) will be required. 