"""
OpenAI Whisper API connector (whisper-1 model).

This is the legacy Whisper API connector that supports the whisper-1 model.
It returns plain text transcriptions without speaker diarization.
"""

import logging
import os
import httpx
from openai import OpenAI
from typing import Dict, Any, Set

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError

logger = logging.getLogger(__name__)


class OpenAIWhisperConnector(BaseTranscriptionConnector):
    """Connector for OpenAI Whisper API (whisper-1 model)."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.CHUNKING,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "openai_whisper"

    # OpenAI Whisper has a 25MB file limit and doesn't handle chunking internally
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=25 * 1024 * 1024,  # 25MB
        handles_chunking_internally=False,
        recommended_chunk_seconds=600,  # 10 minutes
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Whisper connector.

        Args:
            config: Configuration dict with keys:
                - api_key: OpenAI API key (required)
                - base_url: API base URL (optional)
                - model: Model name (default: whisper-1)
                - http_client: Optional httpx.Client instance
        """
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
            base_url=config.get('base_url') or None,
            http_client=http_client
        )
        self.model = config.get('model', 'whisper-1')

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('api_key'):
            raise ConfigurationError("api_key is required for OpenAI Whisper connector")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using OpenAI Whisper API.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with plain text (no diarization)
        """
        try:
            params = {
                "model": self.model,
                "file": request.audio_file,
            }

            if request.language:
                params["language"] = request.language
                logger.info(f"Using transcription language: {request.language}")

            if request.prompt:
                params["prompt"] = request.prompt

            if request.temperature is not None:
                params["temperature"] = request.temperature

            logger.info(f"Sending request to Whisper API with model: {self.model}")
            transcript = self.client.audio.transcriptions.create(**params)

            return TranscriptionResponse(
                text=transcript.text,
                provider=self.PROVIDER_NAME,
                model=self.model
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Whisper transcription failed: {error_msg}")
            raise TranscriptionError(f"Whisper transcription failed: {error_msg}") from e

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
                    "description": "API base URL (optional, for OpenAI-compatible endpoints)"
                },
                "model": {
                    "type": "string",
                    "default": "whisper-1",
                    "description": "Whisper model to use"
                }
            }
        }
