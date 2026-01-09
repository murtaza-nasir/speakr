# Quick Start Guide

Get Speakr up and running in just a few minutes using the pre-built Docker image! This guide will walk you through the fastest way to deploy Speakr with either OpenAI Whisper API or a [custom ASR endpoint](features.md#speaker-diarization).

> **Note:** If you want to use the ASR endpoint option for speaker diarization features, you'll need to run an additional Docker container (`onerahmet/openai-whisper-asr-webservice`). See [Running ASR Service for Speaker Diarization](getting-started/installation.md#running-asr-service-for-speaker-diarization) for detailed setup instructions.

## Prerequisites

Before you begin, make sure you have Docker and Docker Compose installed on your system. You'll also need an API key for either OpenAI or OpenRouter (or a compatible service), at least 2GB of available RAM, and about 10GB of available disk space for storing recordings and transcriptions.

## Step 1: Create Project Directory

First, create a directory for your Speakr installation and navigate into it:

```bash
mkdir speakr
cd speakr
```

## Step 2: Download Configuration Files

Download the Docker Compose configuration and the unified environment template:

```bash
# Download docker compose example
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/docker-compose.example.yml -O docker-compose.yml

# Download the unified configuration template (supports all providers)
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/env.transcription.example -O .env
```

The unified configuration auto-detects your transcription provider based on your settings:

- **OpenAI with diarization**: Set `TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize`
- **Self-hosted ASR/WhisperX**: Set `ASR_BASE_URL=http://your-asr:9000`
- **Legacy Whisper**: Set `TRANSCRIPTION_MODEL=whisper-1`

> **Note:** For self-hosted ASR with speaker diarization, you'll need an additional Docker container. See [Running ASR Service for Speaker Diarization](getting-started/installation.md#running-asr-service-for-speaker-diarization) for complete setup instructions.

## Step 3: Configure Your API Keys

Open the `.env` file in your preferred text editor. Speakr requires **two types of API keys**:

### Required: Text Generation Model (for summaries, titles, chat)

```bash
# For text generation - OpenRouter (recommended, access to many models)
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL_API_KEY=your_openrouter_api_key_here
TEXT_MODEL_NAME=openai/gpt-4o-mini
```

OpenRouter provides access to multiple models including GPT-4, Claude, and others. Alternatively, you can use OpenAI directly:

```bash
# For text generation - OpenAI direct
TEXT_MODEL_BASE_URL=https://api.openai.com/v1
TEXT_MODEL_API_KEY=sk-your_openai_api_key
TEXT_MODEL_NAME=gpt-4o-mini
```

For OpenAI's latest GPT-5 models (`gpt-5`, `gpt-5-mini`, `gpt-5-nano`), you must use the OpenAI API directly. See the [Model Configuration Guide](admin-guide/model-configuration.md) for detailed GPT-5 setup.

### Required: Transcription Service

Choose ONE of the following options:

**Option A: OpenAI with Speaker Diarization (Recommended)**
```bash
TRANSCRIPTION_API_KEY=sk-your_openai_api_key
TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize
```
This provides high-quality transcription with automatic speaker identification - no GPU or self-hosted service required.

**Option B: Self-hosted ASR/WhisperX (Best for privacy)**
```bash
ASR_BASE_URL=http://whisper-asr:9000
ASR_DIARIZE=true
# Optional: Enable voice profiles (WhisperX only)
ASR_RETURN_SPEAKER_EMBEDDINGS=true
```
Requires running an additional ASR container. See [Running ASR Service for Speaker Diarization](getting-started/installation.md#running-asr-service-for-speaker-diarization) for setup instructions.

**Option C: Legacy Whisper (No diarization)**
```bash
TRANSCRIPTION_API_KEY=sk-your_openai_api_key
TRANSCRIPTION_MODEL=whisper-1
```

When using ASR or OpenAI diarization models, [speaker diarization](features.md#speaker-diarization) is automatically enabled, allowing Speakr to identify different speakers in your recordings. After transcription, you'll need to [identify speakers](user-guide/transcripts.md#speaker-identification) to build your speaker library.

## Step 4: Configure Admin Account

Speakr automatically creates an admin user on first startup. Configure these credentials in your `.env` file before launching:

```bash
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=changeme
```

Make sure to change these values to something secure, especially the password. This admin account will be created automatically when you first start Speakr, and you'll use these credentials to log in. The first user created through this method becomes the system administrator with full access to all features including user management and system settings.

## Step 5: Launch Speakr

With your configuration complete, start Speakr using Docker Compose:

```bash
docker compose up -d
```

The first launch will take a few minutes as Docker downloads the pre-built image (about 3GB) and initializes the database. You can monitor the startup process by viewing the logs:

```bash
docker compose logs -f app
```

Look for a message indicating that the Flask application is running and ready to accept connections. Press Ctrl+C to exit the log view (this won't stop the container).

## Step 6: Access Speakr

Once the container is running, open your web browser and navigate to:

```
http://localhost:8899
```

Log in using the admin credentials you configured in Step 4. You should now see the Speakr main screen, ready for your first recording.

## Your First Recording

After logging in, you can immediately start using Speakr. Click the "New Recording" button in the top navigation to either upload an existing audio file or start a [live recording](user-guide/recording.md). For detailed instructions, see the [recording guide](user-guide/recording.md). For uploads, Speakr supports [common audio formats](faq.md#what-audio-formats-does-speakr-support) like MP3, M4A, WAV, and more, with files up to 500MB by default. You can adjust this limit in [system settings](admin-guide/system-settings.md). For live recording, you can capture from your microphone, system audio, or both simultaneously.

## Setting Up Collaboration (Optional)

If you're using Speakr with your family or team, you can leverage powerful collaboration features including groups, group tags, and automatic sharing. These features work great for project teams, departments, or families who want to automatically share certain types of recordings.

### Creating a Group

As an admin, you can create groups to organize users who regularly collaborate:

1. Navigate to **Admin → User Groups** in the admin dashboard
2. Click "Create New Group" and enter a group name (e.g., "Engineering Team", "Family", "Sales Department")
3. Optionally assign a group lead who can manage group settings
4. Add members by searching for usernames and clicking to add them
5. Save the group

### Creating Group Tags

Group tags automatically share recordings with all group members when applied:

1. While editing or creating a group, navigate to the **Tags** section
2. Click "Create Group Tag" and enter:
   - **Tag Name**: What the tag will be called (e.g., "Team Meetings", "Family Events")
   - **Color**: Choose a color for easy visual identification
   - **Custom Prompt** (optional): AI instructions for how to summarize recordings with this tag
   - **Protected**: Enable to prevent automatic deletion by retention policies
   - **Retention Days** (optional): Auto-delete recordings after this many days (leave blank to keep forever)
3. Save the tag

When any group member applies this tag to a recording, all group members automatically get access to it.

### Sharing a Recording with a Group

There are two ways to share recordings with groups:

**Method 1: Using Group Tags** (Automatic)
1. When uploading or after creating a recording, click the tag icon
2. Select any group tag you have access to
3. All group members automatically receive access to this recording
4. Future group members will NOT see historical recordings, only new ones tagged after they join

**Method 2: Individual Sharing** (Manual)
1. Open any recording and click the share icon (users icon) in the toolbar
2. Search for and select users to share with
3. Choose permission level: view-only, edit, or reshare
4. Each user receives immediate access

For more details on collaboration features, see the [Sharing & Collaboration guide](user-guide/sharing.md).

## Optional Features

### Enable Inquire Mode

[Inquire Mode](user-guide/inquire-mode.md) allows you to search across all your recordings using natural language questions. Learn more about [semantic search capabilities](features.md#semantic-search-inquire-mode) in the features guide. To enable it, set this in your `.env` file:

```bash
ENABLE_INQUIRE_MODE=true
```

Then restart the container with `docker compose restart` for the change to take effect.

### Enable User Registration

By default, only the admin can create new users. Learn more about [user management](admin-guide/user-management.md) in the admin guide. To allow self-registration, set:

```bash
ALLOW_REGISTRATION=true
```

### Configure Your Timezone

Set your local timezone for accurate timestamp display:

```bash
TIMEZONE="America/New_York"
```

Use any valid timezone from the TZ database like "Europe/London", "Asia/Tokyo", or "UTC".

## Stopping and Starting Speakr

To stop Speakr while preserving all your data:

```bash
docker compose down
```

To start it again:

```bash
docker compose up -d
```

Your recordings, transcriptions, and settings are preserved in the `./uploads` and `./instance` directories on your host system.

## Troubleshooting

If Speakr doesn't start properly, check the logs for error messages using `docker compose logs app`. For more detailed help, see the [Troubleshooting Guide](troubleshooting.md), particularly the [installation issues](troubleshooting.md#installation-and-setup-issues) section. Common issues include incorrect API keys, which will show authentication errors in the logs, or port conflicts if another service is using port 8899. You can change the port by editing the `docker-compose.yml` file and modifying the ports section.

If transcription fails, verify your API keys are correct and you have sufficient credits with your chosen service. The logs will show detailed error messages that can help identify the issue.

---

Next: [Installation Guide](getting-started/installation.md) for production deployments and advanced configuration