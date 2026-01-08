"""
Application configuration and initialization.
"""

import os
import sys
import httpx
from openai import OpenAI

from src.audio_chunking import AudioChunkingService
from src.config.version import get_version

# Configuration from environment
TEXT_MODEL_API_KEY = os.environ.get("TEXT_MODEL_API_KEY")
TEXT_MODEL_BASE_URL = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
if TEXT_MODEL_BASE_URL:
    TEXT_MODEL_BASE_URL = TEXT_MODEL_BASE_URL.split('#')[0].strip()
TEXT_MODEL_NAME = os.environ.get("TEXT_MODEL_NAME", "openai/gpt-3.5-turbo")

transcription_api_key = os.environ.get("TRANSCRIPTION_API_KEY", "")
transcription_base_url = os.environ.get("TRANSCRIPTION_BASE_URL", "")
if transcription_base_url:
    transcription_base_url = transcription_base_url.split('#')[0].strip()

# New transcription connector configuration
# TRANSCRIPTION_CONNECTOR: explicit connector name (openai_whisper, openai_transcribe, asr_endpoint)
# TRANSCRIPTION_MODEL: model to use (e.g., gpt-4o-transcribe-diarize for diarization)
TRANSCRIPTION_CONNECTOR = os.environ.get('TRANSCRIPTION_CONNECTOR', '').lower().strip()
TRANSCRIPTION_MODEL = os.environ.get('TRANSCRIPTION_MODEL', '')
if TRANSCRIPTION_MODEL:
    TRANSCRIPTION_MODEL = TRANSCRIPTION_MODEL.split('#')[0].strip()

# Feature flag for new transcription architecture (default: enabled)
USE_NEW_TRANSCRIPTION_ARCHITECTURE = os.environ.get(
    'USE_NEW_TRANSCRIPTION_ARCHITECTURE', 'true'
).lower() == 'true'

USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
ASR_BASE_URL = os.environ.get('ASR_BASE_URL')
if ASR_BASE_URL:
    ASR_BASE_URL = ASR_BASE_URL.split('#')[0].strip()

if USE_ASR_ENDPOINT:
    ASR_DIARIZE = os.environ.get('ASR_DIARIZE', 'true').lower() == 'true'
    ASR_MIN_SPEAKERS = os.environ.get('ASR_MIN_SPEAKERS')
    ASR_MAX_SPEAKERS = os.environ.get('ASR_MAX_SPEAKERS')
    # Speaker embeddings are only supported by WhisperX ASR service, not the basic whisper-asr-webservice
    ASR_RETURN_SPEAKER_EMBEDDINGS = os.environ.get('ASR_RETURN_SPEAKER_EMBEDDINGS', 'false').lower() == 'true'
else:
    ASR_DIARIZE = False
    ASR_MIN_SPEAKERS = None
    ASR_MAX_SPEAKERS = None
    ASR_RETURN_SPEAKER_EMBEDDINGS = False

ENABLE_CHUNKING = os.environ.get('ENABLE_CHUNKING', 'true').lower() == 'true'
CHUNK_SIZE_MB = int(os.environ.get('CHUNK_SIZE_MB', '20'))
CHUNK_OVERLAP_SECONDS = int(os.environ.get('CHUNK_OVERLAP_SECONDS', '3'))

# Audio compression settings - compress lossless uploads (WAV, AIFF) to save storage
AUDIO_COMPRESS_UPLOADS = os.environ.get('AUDIO_COMPRESS_UPLOADS', 'true').lower() == 'true'
AUDIO_CODEC = os.environ.get('AUDIO_CODEC', 'mp3').lower()  # mp3, flac, opus
AUDIO_BITRATE = os.environ.get('AUDIO_BITRATE', '128k')  # For lossy codecs

# Unsupported codecs - comma-separated list of codecs to exclude from the default supported list
# Useful when your transcription service doesn't support certain codecs (e.g., vllm doesn't support opus)
# Example: AUDIO_UNSUPPORTED_CODECS=opus,vorbis
_unsupported_codecs_str = os.environ.get('AUDIO_UNSUPPORTED_CODECS', '')
AUDIO_UNSUPPORTED_CODECS = {c.strip().lower() for c in _unsupported_codecs_str.split(',') if c.strip()}

# Create chunking service at module level so it can be imported by processing.py
chunking_service = AudioChunkingService(CHUNK_SIZE_MB, CHUNK_OVERLAP_SECONDS) if ENABLE_CHUNKING else None


def initialize_config(app):
    """Initialize application configuration."""
    app_headers = {
        "HTTP-Referer": "https://github.com/murtaza-nasir/speakr",
        "X-Title": "Speakr - AI Audio Transcription",
        "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)"
    }

    http_client_no_proxy = httpx.Client(verify=True, headers=app_headers)

    client = None
    try:
        api_key = TEXT_MODEL_API_KEY or "not-needed"
        client = OpenAI(api_key=api_key, base_url=TEXT_MODEL_BASE_URL, http_client=http_client_no_proxy)
        app.logger.info(f"LLM client initialized: {TEXT_MODEL_BASE_URL} / {TEXT_MODEL_NAME}")
    except Exception as e:
        app.logger.error(f"Failed to initialize LLM client: {e}")

    # Use module-level chunking_service (already created above)
    version = get_version()

    app.logger.info(f"=== Speakr {version} Starting Up ===")

    # Initialize transcription connector
    if USE_NEW_TRANSCRIPTION_ARCHITECTURE:
        try:
            from src.services.transcription import get_registry
            registry = get_registry()
            connector = registry.initialize_from_env()
            connector_name = registry.get_active_connector_name()
            capabilities = [c.name for c in connector.get_capabilities()]
            app.logger.info(f"Transcription connector initialized: {connector_name}")
            app.logger.info(f"Connector capabilities: {capabilities}")

            # Log diarization support prominently
            if connector.supports_diarization:
                app.logger.info("Speaker diarization: ENABLED")
            else:
                app.logger.info("Speaker diarization: NOT AVAILABLE (connector does not support it)")

        except Exception as e:
            app.logger.error(f"Failed to initialize transcription connector: {e}")
            app.logger.error("Falling back to legacy transcription configuration validation")
            # Fall through to legacy validation
            _validate_legacy_transcription_config(app)
    else:
        # Legacy configuration validation
        _validate_legacy_transcription_config(app)

    return client, chunking_service, version


def _validate_legacy_transcription_config(app):
    """Validate legacy transcription configuration (backwards compatibility)."""
    if USE_ASR_ENDPOINT:
        if not ASR_BASE_URL:
            app.logger.error("ERROR: ASR enabled but ASR_BASE_URL not configured!")
            sys.exit(1)
        app.logger.info(f"Using ASR endpoint: {ASR_BASE_URL}")
    else:
        if not transcription_base_url or not transcription_api_key:
            app.logger.error("ERROR: No transcription service configured!")
            sys.exit(1)
        app.logger.info(f"Using Whisper API: {transcription_base_url}")
