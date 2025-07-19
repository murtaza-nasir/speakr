# Speakr

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0) [![Docker Build and Publish](https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml)

This project is dual-licensed. See the [License](#license) section for details.

Speakr is your intelligent note-taking companion - a personal, self-hosted web application that transforms audio recordings into organized, searchable notes. Whether you're capturing meetings on your phone, recording lectures, or documenting interviews, Speakr automatically transcribes your audio with speaker identification, generates concise summaries, and lets you interact with your content through an AI chat interface. Keep all your notes and insights securely on your own server, accessible from any device.

**Perfect for mobile note-taking:** Start the app on your phone, hit record, and get back diarized transcriptions that identify who said what - turning any conversation into structured, searchable notes.

<div style="display: flex; justify-content: center; gap: 10px; flex-wrap: wrap;">
    <img src="static/img/main.png" alt="Hero Shot" style="max-width: 48%; height: auto;"/>
    <img src="static/img/multilingual-support.png" alt="Multilingual Support" style="max-width: 48%; height: auto;"/>
</div>

## Features Showcase

<details>
<summary><strong>Transcription and Chat</strong></summary>
<div style="display: flex; justify-content: center;">
  <figure>
    <img src="static/img/transcription-chat-bubble-view.png" alt="Transcription and Chat" width="400"/>
    <figcaption style="text-align: center;">Integrated Chat</figcaption>
  </figure>
</div>
</details>

<details>
<summary><strong>Light and Dark Mode</strong></summary>
<div style="display: flex; justify-content: space-around;">
  <figure>
    <img src="static/img/light-mode.png" alt="Light Mode" width="400"/>
    <figcaption style="text-align: center;">Light Mode</figcaption>
  </figure>
  <figure>
    <img src="static/img/dark-mode.png" alt="Dark Mode" width="400"/>
    <figcaption style="text-align: center;">Dark Mode</figcaption>
  </figure>
</div>
</details>

<details>
<summary><strong>Intuitive Transcription Views</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/simple-transcription-view.png" alt="Simple Transcription View" width="400"/>
    <figcaption style="text-align: center;">Simple View</figcaption>
  </figure>
  <figure>
    <img src="static/img/transcription-bubble-view.png" alt="Bubble View" width="400"/>
    <figcaption style="text-align: center;">Bubble View</figcaption>
  </figure>
</div>
</details>

<details>
<summary><strong>Speaker Identification</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/intuitive-speaker-identification.png" alt="Intuitive Speaker Identification" width="400"/>
    <figcaption style="text-align: center;">AI-Assisted Identification</figcaption>
  </figure>
  <figure>
    <img src="static/img/manual-auto-speaker-identification.png" alt="Manual and Auto Speaker ID" width="400"/>
    <figcaption style="text-align: center;">Manual & Auto Options</figcaption>
  </figure>
  <figure>
    <img src="static/img/speaker-suggestions.png" alt="Speaker Suggestions" width="400"/>
    <figcaption style="text-align: center;">Saved Speaker Suggestions</figcaption>
  </figure>
</div>
</details>

<details>
<summary><strong>Record Meetings & Take Notes</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/rec1.png" alt="Transcription with ASR" width="400"/>
    <figcaption style="text-align: center;">Recording options</figcaption>
  </figure>
  <figure>
    <img src="static/img/rec2.png" alt="Transcription without ASR" width="400"/>
    <figcaption style="text-align: center;">Record Phone/Computer Microphone</figcaption>
  </figure>
    <figure>
    <img src="static/img/rec3.png" alt="Transcription without ASR" width="400"/>
    <figcaption style="text-align: center;">Record both Microphone and Computer Sounds (e.g., zoom meeting)</figcaption>
  </figure>
</div>
</details>

## What's New?

*   **Secure Sharing System:** Share your transcriptions publicly with customizable permissions - control whether to include summaries and notes, manage shared links, and revoke access anytime.
*   **Enhanced Recording & Note-Taking:** Completely redesigned recording interface with real-time notepad during recording. Perfect for mobile note-taking with improved system audio capture and dual visualizers.
*   **Advanced Speaker Diarization:** Automatically identify and label different speakers in your recordings with AI-powered speaker detection and saved speaker profiles for future sessions.
*   **Mobile-Optimized Experience:** Seamless recording and note-taking on phones and tablets - start recording on your phone and get back diarized transcriptions with speaker identification.
*   **Automated File Processing:** "Black hole" directory feature for automatic audio file processing - drag and drop recordings from your computer's audio devices for instant transcription.
*   **AMR File Support:** Added support for AMR audio files commonly used by mobile devices and voice recorders.
*   **Transcript Editing:** Edit and update transcriptions in both simple and ASR modes to perfect your notes.
*   **Enhanced Markdown Editor:** Improved summary and notes editor with markdown support for better formatting and organization.
*   **Configurable Logging:** Dynamic logging control for better debugging and troubleshooting.
*   **Clickable Timestamps:** Jump to specific points in recordings by clicking timestamps in transcriptions for easy verification and speaker identification.
*   **Streaming Chat Responses:** Interactive AI chat with real-time streaming responses for better user experience.

## Features

*   **Audio Upload:** Upload audio files (MP3, WAV, M4A, AMR, and many other formats) via drag-and-drop or file selection.
*   **Automated File Processing:** "Black hole" directory monitoring for automatic batch processing of audio files without manual uploads.
*   **Advanced Browser Recording:** Record audio directly in your browser with multiple options:
    *   **Microphone:** Record your voice.
    -   **System Audio:** Capture audio from other applications (e.g., online meetings). **Note: To record system audio, you must select to share a "tab" or "screen" in your browser's screen sharing prompt and ensure the "share audio" checkbox is enabled.**
    -   **Both:** Reliably record both your microphone and system audio simultaneously.
    *   Features dual real-time audio visualizers.
    *   *(Note: System audio capture requires a secure context (HTTPS) or specific browser configuration. See the [Deployment Guide](DEPLOYMENT_GUIDE.md#browser-recording-feature) for details.)*
*   **Transcription:** Choose between a standard OpenAI-compatible API or a more advanced ASR Webservice.
*   **Speaker Diarization:** (ASR/WhisperX only) Automatically detect and separate different speakers in the transcript.
*   **Intuitive Speaker Labeling:** A new, more intuitive interface for identifying and labeling speakers.
*   **AI Summarization & Titling:** Generates concise titles and summaries using configurable LLMs.
*   **Interactive Chat:** Ask questions and interact with the transcription content using an AI model.
*   **Metadata Editing:** Edit titles, participants, meeting dates, summaries, and notes.
*   **User Management:** Secure user registration and login system with an admin dashboard for managing users.
*   **Customization:** Users can set their own language preferences, custom summarization prompts, and professional context to improve AI results.

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

The recommended method is to use the pre-built Docker image, which is fast and simple.

### Easy Setup (Pre-built Docker Image)

You do not need to clone this repository for this method. You only need Docker installed.

1.  **Create the `docker-compose.yml` file:**
    Create a new file named `docker-compose.yml` and paste the following content into it:

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

2.  **Create a Configuration (`.env`) File:**
    Your choice here depends on which transcription method you want to use. See the [API Endpoint Requirements](#api-endpoint-requirements) section above for details on what endpoints each service must implement. Create a new file named `.env` and paste one of the following templates into it.

    *   **Option A: Standard Whisper API Method**
        Uses the `/audio/transcriptions` endpoint. This is the simplest method and works with OpenAI, OpenRouter, local APIs, and other providers implementing the OpenAI Whisper API format.

        ```dotenv
        # --- Text Generation Model (uses /chat/completions endpoint) ---
        TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
        TEXT_MODEL_API_KEY=your_openrouter_api_key
        TEXT_MODEL_NAME=openai/gpt-4o-mini

        # --- Transcription Service (uses /audio/transcriptions endpoint) ---
        TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
        TRANSCRIPTION_API_KEY=your_openai_api_key
        WHISPER_MODEL=whisper-1

        # --- Application Settings ---
        ALLOW_REGISTRATION=false
        SUMMARY_MAX_TOKENS=8000
        CHAT_MAX_TOKENS=5000

        # --- Admin User (created on first run) ---
        ADMIN_USERNAME=admin
        ADMIN_EMAIL=admin@example.com
        ADMIN_PASSWORD=changeme

        # --- Docker Settings (rarely need to be changed) ---
        SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
        UPLOAD_FOLDER=/data/uploads
        ```
        Now, **edit the `.env` file** with your API keys and settings.

    *   **Option B: ASR Webservice Method (for Speaker Diarization)**
        Uses the `/asr` endpoint. This method requires a separate ASR webservice container but enables speaker identification. This has been tested with the `onerahmet/openai-whisper-asr-webservice` image. See the [Deployment Guide](DEPLOYMENT_GUIDE.md#2-asr-webservice-method-advanced-setup) for instructions on how to run the ASR service.

        ```dotenv
        # --- Text Generation Model (uses /chat/completions endpoint) ---
        TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
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
        SUMMARY_MAX_TOKENS=8000
        CHAT_MAX_TOKENS=5000

        # --- Admin User (created on first run) ---
        ADMIN_USERNAME=admin
        ADMIN_EMAIL=admin@example.com
        ADMIN_PASSWORD=changeme

        # --- Docker Settings (rarely need to be changed) ---
        SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
        UPLOAD_FOLDER=/data/uploads
        ```
        Now, **edit the `.env` file** with your ASR service URL and other settings.

3.  **Start the Application:**
    In your terminal, in the same directory as your `docker-compose.yml` and `.env` files, run:
    ```bash
    docker compose up -d
    ```

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

## Usage Guide

1.  **Register/Login:** Access the application at `http://localhost:8899`. The admin user is created from the `ADMIN_*` variables in your `.env` file on first launch.
2.  **Set Preferences (Recommended):** Go to your **Account** page. Here you can:
    *   Set your preferred transcription and output languages.
    *   Define a custom summarization prompt to tailor summaries to your needs.
    *   Add your name, job title, and company to provide more context for AI chat interactions.
    *   View and manage your saved speakers.
3.  **Upload or Record:** 
    *   **Upload:** Go to **New Recording** or drag-and-drop an audio file onto the page
    *   **Record:** Use the browser recording feature in the "New Recording" screen to record directly from your microphone (requires HTTPS or browser configuration - see [Browser Recording Setup](DEPLOYMENT_GUIDE.md#browser-recording-feature)).
        *   **System Audio Recording:** To capture system audio (e.g., from a video conference), you must select to share a specific **browser tab** or your **entire screen** in the browser's sharing dialog. Make sure to also check the box to **"Share tab audio"** or **"Share system audio"**. Recording audio from a single application window is often not supported.
    *   **Automated Processing:** Drop files into the monitored directory for automatic batch processing (see [Automated File Processing](#automated-file-processing) below)
    *   The upload and processing status will appear in a popup.
4.  **View and Interact:**
    *   The main **Gallery** lists your recordings. Click one to view its details.
    *   **Listen** to the audio with the built-in player.
    *   **Read** the transcription and the AI-generated summary.
    *   **Edit** the title, participants, and other metadata.
    *   **Chat with Transcript:** Use the chat panel to ask questions about the recording.
5.  **Speaker Diarization Workflow:**
    *   To enable speaker diarization, you must use the ASR endpoint method and set `ASR_DIARIZE=true` in your `.env` file.
    *   When a recording is processed with this option, speakers will be automatically detected and assigned generic labels (e.g., `SPEAKER 00`, `SPEAKER 01`).
    *   After processing, click the **Identify Speakers** button on the transcription page.
    *   In the speaker identification modal, you can manually assign names to each speaker.
    *   Alternatively, use the **Auto Identify** button to let an AI model attempt to identify and name the speakers based on the conversation context.
    *   Saved speakers will be suggested for auto-completion in future sessions.
    *   You can also use the **Reprocess** button to re-transcribe the audio with different diarization settings if needed.

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
- **Common formats:** MP3, WAV, M4A, FLAC, AAC, OGG
- **Mobile formats:** AMR, 3GP, 3GPP
- **Video formats:** MP4, MOV, WEBM, WMA

Files in unsupported formats are automatically converted to WAV using ffmpeg.

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

### Best Practices

1. **File Naming:** Use descriptive filenames as they become part of the auto-generated title
2. **Batch Processing:** You can drop multiple files at once; they'll be processed sequentially
3. **Network Shares:** Mount network drives or NAS shares to the auto-process directory for remote file drops
4. **Monitoring:** Set up log monitoring to track processing status and catch any errors
5. **Cleanup:** Processed files are automatically moved and deleted from the auto-process directory

### Troubleshooting

**Files not being processed:**
- Check that `ENABLE_AUTO_PROCESSING=true` in your `.env` file
- Verify the auto-process directory is properly mounted
- Check container logs for error messages

**Permission issues:**
- Ensure the auto-process directory has proper read/write permissions
- The container runs as the user specified in the Docker configuration

**Processing errors:**
- Check that your transcription API is properly configured
- Verify ffmpeg is available in the container for file conversion
- Monitor container logs for specific error messages

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

Speakr is actively being developed. Future planned features include:

*   **Quick Language Switching:** A faster way to change transcription or output languages on the fly.
*   **Large File Chunking:** Automatic splitting of large audio files to support transcription endpoints with file size limits (like OpenAI's 25MB limit).

## Contributing

While direct code contributions are not the primary focus at this stage, feedback, bug reports, and feature suggestions are highly valuable! Please feel free to open an Issue on the GitHub repository.

**Note on Future Contributions and CLAs:**
Should this project begin accepting code contributions from external developers in the future, signing a **Contributor License Agreement (CLA)** will be **required** before any pull requests can be merged. This policy ensures that the project maintainer receives the necessary rights to distribute all contributions under both the AGPLv3 and the commercial license options offered. Details on the CLA process will be provided if and when the project formally opens up to external code contributions.
