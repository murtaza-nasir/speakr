"""
ASR Endpoint connector for custom self-hosted ASR services.

Supports whisper-asr-webservice, WhisperX, and other compatible ASR services
that expose a /asr endpoint.
"""

import json
import logging
import httpx
from typing import Dict, Any, Set, Optional

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


class ASREndpointConnector(BaseTranscriptionConnector):
    """Connector for custom ASR webservice (whisper-asr-webservice, WhisperX, etc.)."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,  # Supports min/max speakers
    }
    PROVIDER_NAME = "asr_endpoint"

    # Self-hosted ASR endpoints typically handle files of any size internally
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,  # Unlimited
        max_duration_seconds=None,  # Unlimited
        handles_chunking_internally=True,  # ASR service handles it
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the ASR Endpoint connector.

        Args:
            config: Configuration dict with keys:
                - base_url: ASR service base URL (required)
                - timeout: Request timeout in seconds (default: 1800)
                - return_speaker_embeddings: Whether to request embeddings (default: False)
                - diarize: Whether to enable diarization by default (default: True)
        """
        super().__init__(config)

        self.base_url = config['base_url'].rstrip('/')
        self.timeout = config.get('timeout', 1800)  # 30 minutes default
        self.return_embeddings = config.get('return_speaker_embeddings', False)
        self.default_diarize = config.get('diarize', True)

        # Add speaker embeddings capability if enabled
        if self.return_embeddings:
            self.CAPABILITIES = self.CAPABILITIES | {TranscriptionCapability.SPEAKER_EMBEDDINGS}

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('base_url'):
            raise ConfigurationError("base_url is required for ASR endpoint connector")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using ASR webservice.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with segments and speaker information
        """
        try:
            url = f"{self.base_url}/asr"

            params = {
                'encode': True,
                'task': 'transcribe',
                'output': 'json'
            }

            if request.language:
                params['language'] = request.language
                logger.info(f"Using transcription language: {request.language}")

            # Determine if we should diarize
            should_diarize = request.diarize if request.diarize is not None else self.default_diarize

            if should_diarize:
                # Send both parameter names for compatibility:
                # - 'diarize' is used by whisper-asr-webservice
                # - 'enable_diarization' is used by WhisperX
                params['diarize'] = True
                params['enable_diarization'] = True

                if self.return_embeddings:
                    params['return_speaker_embeddings'] = True

            if request.min_speakers:
                params['min_speakers'] = request.min_speakers
            if request.max_speakers:
                params['max_speakers'] = request.max_speakers

            content_type = request.mime_type or 'application/octet-stream'
            files = {
                'audio_file': (request.filename, request.audio_file, content_type)
            }

            # Configure timeout: generous values for large file uploads
            # Write timeout needs to be high too - large files take time to upload
            timeout = httpx.Timeout(
                None,
                connect=60.0,
                read=float(self.timeout),
                write=float(self.timeout),
                pool=None
            )

            logger.info(f"Sending ASR request to {url} with params: {params} (timeout: {self.timeout}s)")

            with httpx.Client() as client:
                response = client.post(url, params=params, files=files, timeout=timeout)
                logger.info(f"ASR request completed with status: {response.status_code}")
                response.raise_for_status()

                # Parse the JSON response
                response_text = response.text
                try:
                    data = response.json()
                except Exception as json_err:
                    if response_text.strip().startswith('<'):
                        logger.error(f"ASR returned HTML error page (status {response.status_code})")
                        raise ProviderError(
                            f"ASR service returned HTML error page",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )
                    else:
                        raise ProviderError(
                            f"ASR service returned invalid response: {json_err}",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )

            return self._parse_response(data)

        except httpx.HTTPStatusError as e:
            logger.error(f"ASR request failed with status {e.response.status_code}")
            raise ProviderError(
                f"ASR request failed with status {e.response.status_code}",
                provider=self.PROVIDER_NAME,
                status_code=e.response.status_code
            ) from e

        except httpx.TimeoutException as e:
            logger.error(f"ASR request timed out after {self.timeout}s")
            raise TranscriptionError(f"ASR request timed out after {self.timeout}s") from e

        except Exception as e:
            error_msg = str(e)
            logger.error(f"ASR transcription failed: {error_msg}")
            raise TranscriptionError(f"ASR transcription failed: {error_msg}") from e

    def _parse_response(self, data: Dict[str, Any]) -> TranscriptionResponse:
        """
        Parse ASR webservice response into standardized format.

        The ASR response contains:
        - text: Full transcription text
        - language: Detected language
        - segments: Array of segments with speaker, text, start, end
        - speaker_embeddings: Optional speaker embeddings (WhisperX only)
        """
        segments = []
        speakers = set()
        full_text_parts = []
        last_speaker = None

        logger.info(f"ASR response keys: {list(data.keys())}")

        if 'segments' in data and isinstance(data['segments'], list):
            logger.info(f"Number of segments: {len(data['segments'])}")

            for seg in data['segments']:
                speaker = seg.get('speaker')

                # Handle missing speakers by carrying forward from previous segment
                if speaker is None:
                    if last_speaker is not None:
                        speaker = last_speaker
                    else:
                        speaker = 'UNKNOWN_SPEAKER'
                else:
                    last_speaker = speaker

                text = seg.get('text', '').strip()
                speakers.add(speaker)
                full_text_parts.append(f"[{speaker}]: {text}")

                segments.append(TranscriptionSegment(
                    text=text,
                    speaker=speaker,
                    start_time=seg.get('start'),
                    end_time=seg.get('end')
                ))

        # Get the full text
        if 'text' in data:
            full_text = data['text']
        elif full_text_parts:
            full_text = '\n'.join(full_text_parts)
        else:
            full_text = ''

        # Extract speaker embeddings if present
        speaker_embeddings = data.get('speaker_embeddings')
        if speaker_embeddings:
            logger.info(f"Received speaker embeddings for speakers: {list(speaker_embeddings.keys())}")

        logger.info(f"Parsed {len(segments)} segments with {len(speakers)} unique speakers: {sorted(speakers)}")

        return TranscriptionResponse(
            text=full_text,
            segments=segments,
            speakers=sorted(list(speakers)),
            speaker_embeddings=speaker_embeddings,
            language=data.get('language'),
            provider=self.PROVIDER_NAME,
            model="asr-endpoint",
            raw_response=data
        )

    def health_check(self) -> bool:
        """Check if ASR endpoint is reachable."""
        try:
            with httpx.Client(timeout=10.0) as client:
                # Try common health check endpoints
                for endpoint in ['/health', '/']:
                    try:
                        response = client.get(f"{self.base_url}{endpoint}")
                        if response.status_code < 500:
                            return True
                    except Exception:
                        continue
                return False
        except Exception:
            return False

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "ASR service base URL (e.g., http://whisper-asr:9000)"
                },
                "timeout": {
                    "type": "integer",
                    "default": 1800,
                    "description": "Request timeout in seconds"
                },
                "diarize": {
                    "type": "boolean",
                    "default": True,
                    "description": "Enable speaker diarization by default"
                },
                "return_speaker_embeddings": {
                    "type": "boolean",
                    "default": False,
                    "description": "Request speaker embeddings (WhisperX only)"
                }
            }
        }
