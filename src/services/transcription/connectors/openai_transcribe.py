"""
OpenAI GPT-4o Transcribe connector.

Supports the newer GPT-4o based transcription models:
- gpt-4o-transcribe: High quality transcription
- gpt-4o-mini-transcribe: Cost-effective transcription
- gpt-4o-transcribe-diarize: Speaker diarization with labels A, B, C, D
"""

import logging
import httpx
from openai import OpenAI
from typing import Dict, Any, Set, Optional

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError

logger = logging.getLogger(__name__)


class OpenAITranscribeConnector(BaseTranscriptionConnector):
    """Connector for GPT-4o Transcribe models with optional diarization support."""

    # Base capabilities - diarization added dynamically based on model
    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "openai_transcribe"

    # GPT-4o Transcribe models have specific constraints
    # - 25MB file size limit
    # - 1400 second max duration for diarize model
    # - chunking_strategy="auto" handles files >30s internally
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=25 * 1024 * 1024,  # 25MB
        max_duration_seconds=1400,  # ~23.3 minutes max for diarize model
        min_duration_for_chunking=30,  # >30s needs chunking_strategy param
        handles_chunking_internally=True,  # Uses chunking_strategy="auto"
        requires_chunking_param=True,  # Must send chunking_strategy for >30s
    )

    # Models and their capabilities
    MODELS = {
        'gpt-4o-transcribe': {
            'supports_diarization': False,
            'description': 'High quality transcription'
        },
        'gpt-4o-mini-transcribe': {
            'supports_diarization': False,
            'description': 'Cost-effective transcription'
        },
        'gpt-4o-mini-transcribe-2025-12-15': {
            'supports_diarization': False,
            'description': 'Cost-effective transcription (dated version)'
        },
        'gpt-4o-transcribe-diarize': {
            'supports_diarization': True,
            'description': 'Speaker diarization with labels A, B, C, D'
        }
    }

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the GPT-4o Transcribe connector.

        Args:
            config: Configuration dict with keys:
                - api_key: OpenAI API key (required)
                - base_url: API base URL (default: https://api.openai.com/v1)
                - model: Model name (required, one of MODELS)
                - http_client: Optional httpx.Client instance
        """
        # Store model before calling super().__init__ since _validate_config needs it
        self.model = config.get('model', 'gpt-4o-transcribe')

        super().__init__(config)

        # Set up HTTP client with custom headers
        http_client = config.get('http_client')
        if not http_client:
            app_headers = {
                "HTTP-Referer": "https://github.com/murtaza-nasir/speakr",
                "X-Title": "Speakr - AI Audio Transcription",
                "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)"
            }
            http_client = httpx.Client(verify=True, headers=app_headers)

        self.client = OpenAI(
            api_key=config['api_key'],
            base_url=config.get('base_url', 'https://api.openai.com/v1'),
            http_client=http_client
        )

        # Dynamically update capabilities based on model
        if self._model_supports_diarization():
            self.CAPABILITIES = self.CAPABILITIES | {
                TranscriptionCapability.DIARIZATION,
                TranscriptionCapability.KNOWN_SPEAKERS
            }

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('api_key'):
            raise ConfigurationError("api_key is required for OpenAI Transcribe connector")

        model = self.config.get('model', 'gpt-4o-transcribe')
        if model not in self.MODELS:
            raise ConfigurationError(
                f"Unknown model: {model}. Valid models: {list(self.MODELS.keys())}"
            )

    def _model_supports_diarization(self) -> bool:
        """Check if the current model supports diarization."""
        model_info = self.MODELS.get(self.model, {})
        return model_info.get('supports_diarization', False)

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using GPT-4o Transcribe API.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse, with segments if using diarization model
        """
        try:
            params = {
                "model": self.model,
                "file": request.audio_file,
            }

            if request.language:
                params["language"] = request.language
                logger.info(f"Using transcription language: {request.language}")

            # Handle diarization model specifics
            if self.model == 'gpt-4o-transcribe-diarize':
                # Required: chunking_strategy for audio > 30 seconds
                params["chunking_strategy"] = "auto"

                if request.diarize:
                    params["response_format"] = "diarized_json"
                    logger.info("Using diarized_json response format for speaker diarization")

                    # Optional: known speaker support (for future implementation)
                    # if request.known_speaker_names:
                    #     params["known_speaker_names"] = request.known_speaker_names
            else:
                # Non-diarization models
                if request.prompt:
                    params["prompt"] = request.prompt

            logger.info(f"Sending request to GPT-4o Transcribe API with model: {self.model}")
            response = self.client.audio.transcriptions.create(**params)

            # Parse response based on format
            if self.model == 'gpt-4o-transcribe-diarize' and request.diarize:
                return self._parse_diarized_response(response)
            else:
                return self._parse_text_response(response)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"GPT-4o transcription failed: {error_msg}")
            raise TranscriptionError(f"GPT-4o transcription failed: {error_msg}") from e

    def _parse_text_response(self, response) -> TranscriptionResponse:
        """Parse a plain text response."""
        text = response.text if hasattr(response, 'text') else str(response)
        return TranscriptionResponse(
            text=text,
            provider=self.PROVIDER_NAME,
            model=self.model
        )

    def _parse_diarized_response(self, response) -> TranscriptionResponse:
        """
        Parse diarized JSON response into standardized format.

        The diarized_json response contains segments with:
        - speaker: "A", "B", "C", "D" etc.
        - text: The transcribed text
        - start: Segment start time
        - end: Segment end time
        """
        segments = []
        speakers = set()
        full_text_parts = []

        # Handle response object - could be dict or object with attributes
        if hasattr(response, 'segments'):
            raw_segments = response.segments
        elif isinstance(response, dict) and 'segments' in response:
            raw_segments = response['segments']
        else:
            # Fallback to text-only response
            logger.warning("No segments found in diarized response, falling back to text")
            return self._parse_text_response(response)

        for seg in raw_segments:
            # Handle both dict and object segments
            if isinstance(seg, dict):
                speaker = seg.get('speaker', 'Unknown')
                text = seg.get('text', '')
                start = seg.get('start')
                end = seg.get('end')
            else:
                speaker = getattr(seg, 'speaker', 'Unknown')
                text = getattr(seg, 'text', '')
                start = getattr(seg, 'start', None)
                end = getattr(seg, 'end', None)

            speakers.add(speaker)
            full_text_parts.append(f"[{speaker}]: {text}")

            segments.append(TranscriptionSegment(
                text=text,
                speaker=speaker,
                start_time=start,
                end_time=end
            ))

        # Get the full text if available
        if hasattr(response, 'text'):
            full_text = response.text
        elif isinstance(response, dict) and 'text' in response:
            full_text = response['text']
        else:
            full_text = '\n'.join(full_text_parts)

        logger.info(f"Parsed {len(segments)} segments with {len(speakers)} unique speakers: {sorted(speakers)}")

        return TranscriptionResponse(
            text=full_text,
            segments=segments,
            speakers=sorted(list(speakers)),
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=response if isinstance(response, dict) else None
        )

    def health_check(self) -> bool:
        """Check if the connector is properly configured."""
        return bool(self.config.get('api_key'))

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "OpenAI API key"
                },
                "base_url": {
                    "type": "string",
                    "default": "https://api.openai.com/v1",
                    "description": "API base URL"
                },
                "model": {
                    "type": "string",
                    "enum": list(cls.MODELS.keys()),
                    "default": "gpt-4o-transcribe",
                    "description": "GPT-4o transcription model to use"
                }
            }
        }
