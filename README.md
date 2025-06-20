# Speakr

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0) [![Docker Build and Publish](https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml)

This project is dual-licensed. See the [License](#license) section for details.

Speakr is a personal, self-hosted web application designed for transcribing audio recordings (like meetings), generating concise summaries and titles, and interacting with the content through a chat interface. Keep all your meeting notes and insights securely on your own server.

<div style="display: flex; justify-content: center; gap: 10px; flex-wrap: wrap;">
    <img src="static/img/hero-shot.png" alt="Hero Shot" style="max-width: 48%; height: auto;"/>
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
<summary><strong>ASR and API Options</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/transcription-with-asr.png" alt="Transcription with ASR" width="400"/>
    <figcaption style="text-align: center;">With ASR Webservice</figcaption>
  </figure>
  <figure>
    <img src="static/img/transcription-without-asr.png" alt="Transcription without ASR" width="400"/>
    <figcaption style="text-align: center;">With OpenAI-Compatible API</figcaption>
  </figure>
</div>
</details>

<details>
<summary><strong>Advanced Features</strong></summary>
<div style="display: flex; justify-content: space-around; flex-wrap: wrap;">
  <figure>
    <img src="static/img/reprocessing-transcript.png" alt="Reprocessing Transcript" width="400"/>
    <figcaption style="text-align: center;">Reprocess with New Settings</figcaption>
  </figure>
</div>
</details>

## What's New?

*   **Simplified Docker Setup:** Configuration is now managed via a single `.env` file, making setup faster and easier.
*   **Advanced ASR Integration:** Added support for ASR endpoints using the [`onerahmet/openai-whisper-asr-webservice`](https://github.com/ahmetoner/whisper-asr-webservice) package. This integration is necessary for the speaker diarization feature.
*   **Speaker Diarization:** Identify and label different speakers in your recordings. **Note: This feature requires the ASR Webservice method with the `whisperx` engine.**
*   **Speaker Auto-Detection:** When using speaker diarization, the system can automatically attemtpt to detect participant names based on the transcript, so you don't have to specify it manually.
*   **Intuitive Speaker Labeling:** A new, more intuitive interface for identifying and labeling speakers.
*   **Transcription Reprocessing:** A new "Reprocess" button allows you to re-run transcription with different settings (e.g., to add diarization).
*   **Speaker Identification:** A new modal helps you name speakers, with highlighting for clarity. You can also automatically identify speakers using an LLM.
*   **Saved Speaker Profiles:** Save identified speakers for auto-completion in future transcriptions. View and manage them on your Account page.
*   **Enhanced Summaries:** Summarization now includes user context (name, title) and allows for custom prompts.

## Features

*   **Audio Upload:** Upload audio files (MP3, WAV, M4A, etc. depending on your transcription endpoint) via drag-and-drop or file selection.
*   **Browser Recording:** Record audio directly in your browser from the "New Recording" screen (requires HTTPS or browser configuration for HTTP - see [Deployment Guide](DEPLOYMENT_GUIDE.md#browser-recording-feature)).
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
        Uses the `/asr` endpoint. This method requires a separate ASR webservice container but enables speaker identification. This has been tested with the `onerahmet/openai-whisper-asr-webservice` image.

        ```dotenv
        # --- Text Generation Model (uses /chat/completions endpoint) ---
        TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
        TEXT_MODEL_API_KEY=your_openrouter_api_key
        TEXT_MODEL_NAME=openai/gpt-4o-mini

        # --- Transcription Service (uses /asr endpoint) ---
        USE_ASR_ENDPOINT=true
        ASR_BASE_URL=http://your_asr_host:9000
        ASR_ENCODE=true
        ASR_TASK=transcribe
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
    *   **Record:** Use the browser recording feature in the "New Recording" screen to record directly from your microphone (requires HTTPS or browser configuration - see [Browser Recording Setup](DEPLOYMENT_GUIDE.md#browser-recording-feature))
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
