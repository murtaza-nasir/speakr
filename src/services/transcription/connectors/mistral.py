"""
Mistral Voxtral API connector for audio transcription.

Supports Mistral's Voxtral models which provide high-quality transcription
with diarization, context biasing (hotwords), and language detection.
Particularly strong for French and multilingual audio.
"""

import logging
import re
import httpx
from typing import Dict, Any, Set, List, Optional

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


class MistralTranscriptionConnector(BaseTranscriptionConnector):
    """Connector for Mistral Voxtral transcription API."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "mistral"

    # Voxtral supports up to 3 hours per request, no app-level chunking needed
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,  # No hard limit for chunking decisions
        handles_chunking_internally=True,
        recommended_chunk_seconds=0,  # Disable — Mistral handles up to 3hrs natively
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Mistral Voxtral connector.

        Args:
            config: Configuration dict with keys:
                - api_key: Mistral API key (required)
                - base_url: API base URL (default: https://api.mistral.ai)
                - model: Model name (default: voxtral-mini-latest)
        """
        super().__init__(config)

        self.api_key = config['api_key']
        self.base_url = (config.get('base_url') or 'https://api.mistral.ai').rstrip('/')
        self.model = config.get('model', 'voxtral-mini-latest')

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'User-Agent': 'Speakr/1.0 (https://github.com/murtaza-nasir/speakr)',
            },
            timeout=httpx.Timeout(60.0, read=1800.0, write=300.0),
            verify=True,
        )

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('api_key'):
            raise ConfigurationError("api_key is required for Mistral connector")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using Mistral Voxtral API.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with text, segments, and optional diarization
        """
        try:
            # Build multipart form data using tuples for proper array encoding
            file_tuple = ('file', (request.filename or 'audio.wav', request.audio_file, request.mime_type or 'application/octet-stream'))

            fields: List[tuple] = [
                ('model', self.model),
            ]

            # Language param
            if request.language:
                fields.append(('language', request.language))
                logger.info(f"Using transcription language: {request.language}")

            # Diarization
            if request.diarize:
                fields.append(('diarize', 'true'))
                logger.info("Diarization enabled for Mistral request")

            # Context bias (hotwords) - Mistral accepts an array of strings
            # Each item must match ^[^,\s]+$ (no commas or whitespace per item)
            # So we split on both commas and whitespace to produce individual tokens
            if request.hotwords:
                context_bias = [w for w in re.split(r'[,\s]+', request.hotwords) if w]
                for term in context_bias:
                    fields.append(('context_bias', term))
                if context_bias:
                    logger.info(f"Using context bias with {len(context_bias)} terms")

            # Timestamp granularities - always request segment-level timestamps
            fields.append(('timestamp_granularities', 'segment'))

            # Log prompt warning if provided (Mistral doesn't support prompt/initial_prompt)
            if request.prompt:
                logger.warning("Mistral Voxtral does not support initial_prompt parameter, ignoring")

            logger.info(f"Sending request to Mistral API with model: {self.model}")
            response = self.client.post(
                '/v1/audio/transcriptions',
                files=[file_tuple] + [(name, (None, value)) for name, value in fields],
            )

            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    error_detail = error_json.get('message', error_json.get('detail', response.text))
                except Exception:
                    pass
                raise ProviderError(
                    f"Mistral API error: {error_detail}",
                    provider=self.PROVIDER_NAME,
                    status_code=response.status_code,
                )

            result = response.json()
            logger.info(f"Mistral API response keys: {list(result.keys())}")
            if result.get('segments'):
                logger.info(f"First segment sample: {result['segments'][0] if result['segments'] else 'none'}")
                logger.info(f"Total segments: {len(result['segments'])}")
            else:
                logger.warning(f"No segments in Mistral response. Full response (truncated): {str(result)[:500]}")

            # Parse segments if available
            segments = self._parse_segments(result.get('segments', []))

            # Determine detected language
            detected_language = result.get('language', request.language)

            # Build speaker list from segments
            speakers = list({s.speaker for s in segments if s.speaker})

            return TranscriptionResponse(
                text=result.get('text', ''),
                segments=segments,
                language=detected_language,
                speakers=speakers if speakers else None,
                provider=self.PROVIDER_NAME,
                model=self.model,
                raw_response=result,
            )

        except ProviderError:
            raise
        except httpx.TimeoutException as e:
            logger.error(f"Mistral API request timed out: {e}")
            raise TranscriptionError(f"Mistral API request timed out: {e}") from e
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Mistral transcription failed: {error_msg}")
            raise TranscriptionError(f"Mistral transcription failed: {error_msg}") from e

    def _parse_segments(self, raw_segments: List[Dict[str, Any]]) -> List[TranscriptionSegment]:
        """
        Convert Mistral segment format to TranscriptionSegment objects.

        Args:
            raw_segments: List of segment dicts from Mistral API

        Returns:
            List of TranscriptionSegment objects
        """
        segments = []
        for seg in raw_segments:
            segment = TranscriptionSegment(
                text=seg.get('text', ''),
                speaker=seg.get('speaker_id', seg.get('speaker', None)),
                start_time=seg.get('start', None),
                end_time=seg.get('end', None),
                confidence=seg.get('score', None),
            )
            segments.append(segment)
        return segments

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
                    "description": "Mistral API key"
                },
                "base_url": {
                    "type": "string",
                    "default": "https://api.mistral.ai",
                    "description": "API base URL"
                },
                "model": {
                    "type": "string",
                    "default": "voxtral-mini-latest",
                    "description": "Voxtral model to use"
                }
            }
        }
