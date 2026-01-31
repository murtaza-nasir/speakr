"""
Azure OpenAI Transcribe connector.

Supports Azure OpenAI audio transcription models:
- whisper-1: Basic transcription (no diarization)
- gpt-4o-transcribe: High quality transcription
- gpt-4o-mini-transcribe: Cost-effective transcription
- gpt-4o-transcribe-diarize: Speaker diarization with labels A, B, C, D

Azure OpenAI uses a different API format than standard OpenAI:
- Endpoint: https://{resource}.openai.azure.com/openai/deployments/{deployment}/audio/transcriptions
- Requires api-version query parameter
- Uses api-key header for authentication
"""

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
from ..exceptions import TranscriptionError, ConfigurationError

logger = logging.getLogger(__name__)


class AzureOpenAITranscribeConnector(BaseTranscriptionConnector):
    """Connector for Azure OpenAI audio transcription models."""

    # Base capabilities - diarization added dynamically based on model
    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "azure_openai_transcribe"

    # Default specifications (will be overridden per-model in __init__)
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=25 * 1024 * 1024,  # 25MB
        max_duration_seconds=1400,  # Default to most restrictive (diarize model)
        min_duration_for_chunking=30,
        handles_chunking_internally=False,
        requires_chunking_param=True,
        recommended_chunk_seconds=1200,
        unsupported_codecs=frozenset({'opus'}),
    )

    # Models and their capabilities
    MODELS = {
        'whisper-1': {
            'supports_diarization': False,
            'max_duration_seconds': 1500,
            'recommended_chunk_seconds': 1200,
            'description': 'OpenAI Whisper model on Azure'
        },
        'gpt-4o-transcribe': {
            'supports_diarization': False,
            'max_duration_seconds': 1500,
            'recommended_chunk_seconds': 1200,
            'description': 'High quality transcription'
        },
        'gpt-4o-mini-transcribe': {
            'supports_diarization': False,
            'max_duration_seconds': 1500,
            'recommended_chunk_seconds': 1200,
            'description': 'Cost-effective transcription'
        },
        'gpt-4o-mini-transcribe-2025-12-15': {
            'supports_diarization': False,
            'max_duration_seconds': 1500,
            'recommended_chunk_seconds': 1200,
            'description': 'Cost-effective transcription (dated version)'
        },
        'gpt-4o-transcribe-diarize': {
            'supports_diarization': True,
            'max_duration_seconds': 1400,
            'recommended_chunk_seconds': 1200,
            'description': 'Speaker diarization with labels A, B, C, D'
        }
    }

    # Default API version - can be overridden in config
    DEFAULT_API_VERSION = "2025-04-01-preview"

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Azure OpenAI Transcribe connector.

        Args:
            config: Configuration dict with keys:
                - api_key: Azure OpenAI API key (required)
                - endpoint: Azure OpenAI endpoint URL (required)
                    e.g., https://your-resource.openai.azure.com
                - deployment_name: The deployment name for the model (required)
                - api_version: API version (default: 2025-04-01-preview)
                - model: Model name for validation (optional, defaults to deployment_name)
        """
        # Store model/deployment before calling super().__init__
        self.deployment_name = config.get('deployment_name', '')
        self.model = config.get('model', self.deployment_name)
        self.api_version = config.get('api_version', self.DEFAULT_API_VERSION)

        # Set model-specific specifications
        model_info = self.MODELS.get(self.model, {})
        if model_info:
            self.SPECIFICATIONS = ConnectorSpecifications(
                max_file_size_bytes=25 * 1024 * 1024,
                max_duration_seconds=model_info.get('max_duration_seconds', 1400),
                min_duration_for_chunking=30,
                handles_chunking_internally=False,
                requires_chunking_param=True,
                recommended_chunk_seconds=model_info.get('recommended_chunk_seconds', 1200),
                unsupported_codecs=frozenset({'opus'}),
            )

        super().__init__(config)

        # Parse endpoint URL
        self.endpoint = config['endpoint'].rstrip('/')

        # Set up HTTP client
        self.http_client = httpx.Client(
            timeout=httpx.Timeout(
                connect=60.0,
                read=1800.0,  # 30 minutes for long transcriptions
                write=1800.0,
                pool=None
            ),
            headers={
                "api-key": config['api_key'],
                "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)"
            }
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
            raise ConfigurationError("api_key is required for Azure OpenAI Transcribe connector")
        if not self.config.get('endpoint'):
            raise ConfigurationError("endpoint is required for Azure OpenAI Transcribe connector")
        if not self.config.get('deployment_name'):
            raise ConfigurationError("deployment_name is required for Azure OpenAI Transcribe connector")

    def _model_supports_diarization(self) -> bool:
        """Check if the current model supports diarization."""
        model_info = self.MODELS.get(self.model, {})
        return model_info.get('supports_diarization', False)

    def _build_url(self) -> str:
        """Build the Azure OpenAI transcription API URL."""
        return f"{self.endpoint}/openai/deployments/{self.deployment_name}/audio/transcriptions?api-version={self.api_version}"

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using Azure OpenAI API.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse, with segments if using diarization model
        """
        try:
            url = self._build_url()

            # Build form data
            data = {}

            if request.language:
                data["language"] = request.language
                logger.info(f"Using transcription language: {request.language}")

            # Handle diarization model specifics
            is_diarize_model = 'diarize' in self.model.lower()

            if is_diarize_model:
                # Required: chunking_strategy for audio > 30 seconds
                data["chunking_strategy"] = "auto"

                if request.diarize:
                    data["response_format"] = "diarized_json"
                    logger.info("Using diarized_json response format for speaker diarization")

                    # Known speaker support
                    if request.known_speaker_names and request.known_speaker_references:
                        for i, name in enumerate(request.known_speaker_names):
                            if name in request.known_speaker_references:
                                data[f"known_speaker_names[{i}]"] = name
                                data[f"known_speaker_references[{i}]"] = request.known_speaker_references[name]
                        logger.info(f"Using known speaker references for {len(request.known_speaker_names)} speakers")
            else:
                # Non-diarization models - request verbose_json for timestamps
                data["response_format"] = "verbose_json"
                if request.prompt:
                    data["prompt"] = request.prompt

            # Prepare file for upload
            content_type = request.mime_type or 'application/octet-stream'
            files = {
                "file": (request.filename, request.audio_file, content_type)
            }

            logger.info(f"Sending request to Azure OpenAI: {url}")
            logger.info(f"Model: {self.model}, Deployment: {self.deployment_name}")

            response = self.http_client.post(url, data=data, files=files)

            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    if 'error' in error_json:
                        error_detail = error_json['error'].get('message', error_detail)
                except:
                    pass
                logger.error(f"Azure OpenAI transcription failed: {response.status_code} - {error_detail}")
                raise TranscriptionError(f"Azure OpenAI transcription failed: {response.status_code} - {error_detail}")

            result = response.json()

            # Parse response based on format
            if is_diarize_model and request.diarize:
                return self._parse_diarized_response(result)
            else:
                return self._parse_response(result)

        except TranscriptionError:
            raise
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Azure OpenAI transcription failed: {error_msg}")
            raise TranscriptionError(f"Azure OpenAI transcription failed: {error_msg}") from e

    def _parse_response(self, response: Dict) -> TranscriptionResponse:
        """Parse a standard (non-diarized) response."""
        text = response.get('text', '')

        # Check for segments (verbose_json format)
        segments = []
        if 'segments' in response:
            for seg in response['segments']:
                segments.append(TranscriptionSegment(
                    text=seg.get('text', ''),
                    start_time=seg.get('start'),
                    end_time=seg.get('end')
                ))

        return TranscriptionResponse(
            text=text,
            segments=segments if segments else None,
            language=response.get('language'),
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=response
        )

    def _parse_diarized_response(self, response: Dict) -> TranscriptionResponse:
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

        raw_segments = response.get('segments', [])

        if not raw_segments:
            # Fallback to text-only response
            logger.warning("No segments found in diarized response, falling back to text")
            return self._parse_response(response)

        for seg in raw_segments:
            speaker = seg.get('speaker', 'Unknown')
            text = seg.get('text', '')
            start = seg.get('start')
            end = seg.get('end')

            # Skip empty segments
            if not text or not text.strip():
                continue

            speakers.add(speaker)
            full_text_parts.append(f"[{speaker}]: {text}")

            segments.append(TranscriptionSegment(
                text=text,
                speaker=speaker,
                start_time=start,
                end_time=end
            ))

        # Build full text with speaker labels
        full_text = '\n'.join(full_text_parts)

        logger.info(f"Parsed {len(segments)} segments with {len(speakers)} unique speakers: {sorted(speakers)}")

        return TranscriptionResponse(
            text=full_text,
            segments=segments,
            speakers=sorted(list(speakers)),
            language=response.get('language'),
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=response
        )

    def health_check(self) -> bool:
        """Check if the connector is properly configured."""
        return bool(
            self.config.get('api_key') and
            self.config.get('endpoint') and
            self.config.get('deployment_name')
        )

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "type": "object",
            "required": ["api_key", "endpoint", "deployment_name"],
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "Azure OpenAI API key"
                },
                "endpoint": {
                    "type": "string",
                    "description": "Azure OpenAI endpoint URL (e.g., https://your-resource.openai.azure.com)"
                },
                "deployment_name": {
                    "type": "string",
                    "description": "The deployment name for your transcription model"
                },
                "api_version": {
                    "type": "string",
                    "default": cls.DEFAULT_API_VERSION,
                    "description": "Azure OpenAI API version"
                },
                "model": {
                    "type": "string",
                    "enum": list(cls.MODELS.keys()),
                    "description": "Model type (for capability detection, defaults to deployment_name)"
                }
            }
        }
