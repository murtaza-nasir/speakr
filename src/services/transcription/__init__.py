"""
Transcription service package.

Provides a connector-based architecture for speech-to-text transcription
with support for multiple providers:

- OpenAI Whisper (whisper-1)
- OpenAI GPT-4o Transcribe (gpt-4o-transcribe, gpt-4o-mini-transcribe, gpt-4o-transcribe-diarize)
- Custom ASR endpoints (whisper-asr-webservice, WhisperX, etc.)

Usage:
    from src.services.transcription import (
        transcribe,
        get_connector,
        supports_diarization,
        TranscriptionRequest,
        TranscriptionResponse,
    )

    # Simple transcription using active connector
    with open('audio.mp3', 'rb') as f:
        request = TranscriptionRequest(
            audio_file=f,
            filename='audio.mp3',
            diarize=True
        )
        response = transcribe(request)
        print(response.text)
        if response.segments:
            for seg in response.segments:
                print(f"[{seg.speaker}]: {seg.text}")
"""

from .base import (
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    BaseTranscriptionConnector,
    ConnectorSpecifications,
    DEFAULT_SPECIFICATIONS,
)

from .exceptions import (
    TranscriptionError,
    ConfigurationError,
    ProviderError,
    AudioFormatError,
    ChunkingError,
)

from .registry import (
    ConnectorRegistry,
    get_registry,
    connector_registry,
    transcribe,
    get_connector,
    supports_diarization,
)

from .connectors import (
    OpenAIWhisperConnector,
    OpenAITranscribeConnector,
    ASREndpointConnector,
)

__all__ = [
    # Base types
    'TranscriptionCapability',
    'TranscriptionRequest',
    'TranscriptionResponse',
    'TranscriptionSegment',
    'BaseTranscriptionConnector',
    'ConnectorSpecifications',
    'DEFAULT_SPECIFICATIONS',

    # Exceptions
    'TranscriptionError',
    'ConfigurationError',
    'ProviderError',
    'AudioFormatError',
    'ChunkingError',

    # Registry
    'ConnectorRegistry',
    'get_registry',
    'connector_registry',

    # Convenience functions
    'transcribe',
    'get_connector',
    'supports_diarization',

    # Connectors
    'OpenAIWhisperConnector',
    'OpenAITranscribeConnector',
    'ASREndpointConnector',
]
