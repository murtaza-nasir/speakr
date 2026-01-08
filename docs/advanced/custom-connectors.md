# Creating Custom Transcription Connectors

This guide explains how to create custom transcription connectors for Speakr. Connectors allow you to integrate new transcription providers like Deepgram, AssemblyAI, Google Cloud Speech-to-Text, or your own custom services.

## Architecture Overview

Speakr uses a connector-based architecture where each transcription provider is implemented as a connector class that extends `BaseTranscriptionConnector`. The connector registry manages all available connectors and handles auto-detection from environment variables.

```
src/services/transcription/
├── base.py              # Base classes and data types
├── registry.py          # Connector registry and factory
├── exceptions.py        # Custom exceptions
└── connectors/
    ├── __init__.py
    ├── openai_whisper.py    # OpenAI Whisper connector
    ├── openai_transcribe.py # OpenAI GPT-4o transcribe connector
    └── asr_endpoint.py      # Self-hosted ASR connector
```

## Base Classes

### TranscriptionCapability

Capabilities that connectors can declare support for:

```python
from enum import Enum, auto

class TranscriptionCapability(Enum):
    DIARIZATION = auto()           # Speaker diarization
    CHUNKING = auto()              # Automatic file chunking for large files
    TIMESTAMPS = auto()            # Word/segment timestamps
    LANGUAGE_DETECTION = auto()    # Auto language detection
    KNOWN_SPEAKERS = auto()        # Support for known speaker references
    SPEAKER_EMBEDDINGS = auto()    # Return speaker embeddings
    SPEAKER_COUNT_CONTROL = auto() # Support for min/max speaker count
    STREAMING = auto()             # Real-time streaming transcription
```

### ConnectorSpecifications

Provider-specific constraints and requirements:

```python
from dataclasses import dataclass
from typing import Optional, FrozenSet

@dataclass
class ConnectorSpecifications:
    # Size constraints
    max_file_size_bytes: Optional[int] = None  # None = unlimited

    # Duration constraints
    max_duration_seconds: Optional[int] = None
    min_duration_for_chunking: Optional[int] = None

    # Chunking behavior
    handles_chunking_internally: bool = False  # Provider handles large files
    requires_chunking_param: bool = False      # Must send chunking_strategy param
    recommended_chunk_seconds: int = 600       # 10 minutes default

    # Audio format support
    supported_codecs: Optional[FrozenSet[str]] = None    # Override defaults
    unsupported_codecs: Optional[FrozenSet[str]] = None  # Exclude from defaults
```

### Request/Response Types

```python
@dataclass
class TranscriptionRequest:
    audio_file: BinaryIO
    filename: str
    mime_type: Optional[str] = None
    language: Optional[str] = None

    # Diarization options
    diarize: bool = False
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None

    # Advanced options
    prompt: Optional[str] = None
    temperature: Optional[float] = None
    extra_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TranscriptionSegment:
    text: str
    speaker: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    confidence: Optional[float] = None
    words: Optional[List[Dict[str, Any]]] = None


@dataclass
class TranscriptionResponse:
    text: str
    segments: Optional[List[TranscriptionSegment]] = None
    language: Optional[str] = None
    duration: Optional[float] = None
    speakers: Optional[List[str]] = None
    speaker_embeddings: Optional[Dict[str, List[float]]] = None
    provider: str = ""
    model: str = ""
    raw_response: Optional[Dict[str, Any]] = None
```

## Creating a Custom Connector

### Step 1: Create the Connector File

Create a new file in `src/services/transcription/connectors/`:

```python
# src/services/transcription/connectors/deepgram.py

"""
Deepgram transcription connector.

Integrates with Deepgram's speech-to-text API.
"""

import logging
import httpx
from typing import Dict, Any, Set

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError, ProviderError

logger = logging.getLogger(__name__)


class DeepgramConnector(BaseTranscriptionConnector):
    """Connector for Deepgram speech-to-text API."""

    # Declare what capabilities this connector supports
    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,
    }

    PROVIDER_NAME = "deepgram"

    # Define provider constraints
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=2 * 1024 * 1024 * 1024,  # 2GB
        handles_chunking_internally=True,  # Deepgram handles large files
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Deepgram connector.

        Args:
            config: Configuration dict with keys:
                - api_key: Deepgram API key (required)
                - model: Model name (default: 'nova-2')
                - language: Default language (optional)
        """
        super().__init__(config)

        self.api_key = config['api_key']
        self.model = config.get('model', 'nova-2')
        self.default_language = config.get('language')
        self.base_url = 'https://api.deepgram.com/v1/listen'

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('api_key'):
            raise ConfigurationError("api_key is required for Deepgram connector")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using Deepgram.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with segments and speaker information
        """
        try:
            # Build query parameters
            params = {
                'model': self.model,
                'punctuate': 'true',
                'utterances': 'true',
            }

            # Add diarization if requested
            if request.diarize:
                params['diarize'] = 'true'
                if request.min_speakers:
                    params['diarize_version'] = '2'  # Required for speaker count
                if request.max_speakers:
                    params['diarize_version'] = '2'

            # Add language
            language = request.language or self.default_language
            if language:
                params['language'] = language
            else:
                params['detect_language'] = 'true'

            # Prepare headers
            headers = {
                'Authorization': f'Token {self.api_key}',
                'Content-Type': request.mime_type or 'audio/wav',
            }

            # Read audio content
            audio_content = request.audio_file.read()

            # Make request
            timeout = httpx.Timeout(300.0, connect=60.0)

            with httpx.Client() as client:
                response = client.post(
                    self.base_url,
                    params=params,
                    headers=headers,
                    content=audio_content,
                    timeout=timeout
                )
                response.raise_for_status()
                data = response.json()

            return self._parse_response(data)

        except httpx.HTTPStatusError as e:
            raise ProviderError(
                f"Deepgram request failed with status {e.response.status_code}",
                provider=self.PROVIDER_NAME,
                status_code=e.response.status_code
            ) from e

        except Exception as e:
            raise TranscriptionError(f"Deepgram transcription failed: {e}") from e

    def _parse_response(self, data: Dict[str, Any]) -> TranscriptionResponse:
        """Parse Deepgram response into standardized format."""
        segments = []
        speakers = set()

        # Extract from utterances (for diarization)
        results = data.get('results', {})
        utterances = results.get('utterances', [])

        for utt in utterances:
            speaker = f"SPEAKER_{utt.get('speaker', 0):02d}"
            speakers.add(speaker)

            segments.append(TranscriptionSegment(
                text=utt.get('transcript', ''),
                speaker=speaker,
                start_time=utt.get('start'),
                end_time=utt.get('end'),
                confidence=utt.get('confidence')
            ))

        # Get full transcript
        channels = results.get('channels', [])
        full_text = ''
        if channels:
            alternatives = channels[0].get('alternatives', [])
            if alternatives:
                full_text = alternatives[0].get('transcript', '')

        # Get detected language
        detected_language = None
        if channels:
            detected_language = channels[0].get('detected_language')

        return TranscriptionResponse(
            text=full_text,
            segments=segments if segments else None,
            speakers=sorted(list(speakers)) if speakers else None,
            language=detected_language,
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=data
        )

    def health_check(self) -> bool:
        """Check if Deepgram API is reachable."""
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(
                    'https://api.deepgram.com/v1/projects',
                    headers={'Authorization': f'Token {self.api_key}'}
                )
                return response.status_code < 500
        except Exception:
            return False

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "Deepgram API key"
                },
                "model": {
                    "type": "string",
                    "default": "nova-2",
                    "description": "Deepgram model name"
                },
                "language": {
                    "type": "string",
                    "description": "Default language code (e.g., 'en')"
                }
            }
        }
```

### Step 2: Register the Connector

Add your connector to the registry in `src/services/transcription/registry.py`:

```python
def _register_builtin_connectors(self):
    """Register all built-in connectors."""
    from .connectors.openai_whisper import OpenAIWhisperConnector
    from .connectors.openai_transcribe import OpenAITranscribeConnector
    from .connectors.asr_endpoint import ASREndpointConnector
    from .connectors.deepgram import DeepgramConnector  # Add this

    self.register('openai_whisper', OpenAIWhisperConnector)
    self.register('openai_transcribe', OpenAITranscribeConnector)
    self.register('asr_endpoint', ASREndpointConnector)
    self.register('deepgram', DeepgramConnector)  # Add this
```

### Step 3: Add Environment Configuration

Add config building logic to `_build_config_from_env()` in the registry:

```python
def _build_config_from_env(self, connector_name: str) -> Dict[str, Any]:
    # ... existing code ...

    elif connector_name == 'deepgram':
        return {
            'api_key': os.environ.get('DEEPGRAM_API_KEY', ''),
            'model': os.environ.get('DEEPGRAM_MODEL', 'nova-2'),
            'language': os.environ.get('DEEPGRAM_LANGUAGE'),
        }

    # ... rest of method ...
```

### Step 4: Add Auto-Detection (Optional)

If you want auto-detection based on environment variables, add logic to `initialize_from_env()`:

```python
def initialize_from_env(self) -> BaseTranscriptionConnector:
    # ... existing code ...

    # Add before the default case:
    elif os.environ.get('DEEPGRAM_API_KEY'):
        connector_name = 'deepgram'
        logger.info("Auto-detected Deepgram from DEEPGRAM_API_KEY")

    # ... rest of method ...
```

## Usage

### Manual Selection

Explicitly select the connector in the `.env` file:

```bash
TRANSCRIPTION_CONNECTOR=deepgram
DEEPGRAM_API_KEY=your-api-key
DEEPGRAM_MODEL=nova-2
```

### Auto-Detection

If you added auto-detection logic, just setting the API key is enough:

```bash
DEEPGRAM_API_KEY=your-api-key
```

## Best Practices

### Error Handling

Use the appropriate exception types:

```python
from ..exceptions import TranscriptionError, ConfigurationError, ProviderError

# For configuration issues (missing API key, invalid settings)
raise ConfigurationError("api_key is required")

# For provider-specific errors (API errors, rate limits)
raise ProviderError(
    "Rate limit exceeded",
    provider=self.PROVIDER_NAME,
    status_code=429
)

# For general transcription failures
raise TranscriptionError("Failed to process audio")
```

### Logging

Use structured logging for debugging:

```python
import logging
logger = logging.getLogger(__name__)

logger.info(f"Sending request to {self.base_url}")
logger.debug(f"Request params: {params}")
logger.error(f"Transcription failed: {error}")
```

### Capability Declaration

Only declare capabilities your connector actually supports:

```python
CAPABILITIES: Set[TranscriptionCapability] = {
    TranscriptionCapability.DIARIZATION,      # If you support speaker labels
    TranscriptionCapability.TIMESTAMPS,        # If you return timing info
    TranscriptionCapability.LANGUAGE_DETECTION,# If you can detect language
    TranscriptionCapability.SPEAKER_COUNT_CONTROL, # If you support min/max speakers
}
```

### Specifications

Accurately declare your provider's constraints:

```python
SPECIFICATIONS = ConnectorSpecifications(
    max_file_size_bytes=100 * 1024 * 1024,  # Provider's file size limit
    max_duration_seconds=14400,              # 4 hours max
    handles_chunking_internally=True,        # Set True if provider handles large files
    unsupported_codecs=frozenset({'opus'}),  # Codecs to convert before sending
)
```

### Config Schema

Provide a config schema for documentation and validation:

```python
@classmethod
def get_config_schema(cls) -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["api_key"],
        "properties": {
            "api_key": {
                "type": "string",
                "description": "Your API key"
            },
            # ... other properties
        }
    }
```

## Testing Your Connector

Create a test file to verify your connector works:

```python
# tests/test_deepgram_connector.py

import os
import io
import pytest
from src.services.transcription.connectors.deepgram import DeepgramConnector
from src.services.transcription.base import TranscriptionRequest

@pytest.fixture
def connector():
    config = {
        'api_key': os.environ.get('DEEPGRAM_API_KEY', 'test-key'),
        'model': 'nova-2'
    }
    return DeepgramConnector(config)

def test_config_validation():
    with pytest.raises(ConfigurationError):
        DeepgramConnector({})  # Missing api_key

def test_capabilities(connector):
    assert connector.supports_diarization
    assert TranscriptionCapability.TIMESTAMPS in connector.CAPABILITIES

def test_transcribe(connector):
    # Load a test audio file
    with open('tests/fixtures/test_audio.wav', 'rb') as f:
        request = TranscriptionRequest(
            audio_file=f,
            filename='test.wav',
            mime_type='audio/wav',
            diarize=True
        )
        response = connector.transcribe(request)

    assert response.text
    assert response.provider == 'deepgram'
```

## Contributing

If you create a connector for a popular provider, consider contributing it back to the project:

1. Fork the repository
2. Add your connector following this guide
3. Add tests and documentation
4. Submit a pull request

Popular providers we'd love to see connectors for:
- Deepgram
- AssemblyAI
- Google Cloud Speech-to-Text
- Amazon Transcribe
- Azure Speech Services
