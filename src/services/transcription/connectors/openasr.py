"""
OpenASR local server connector.

OpenASR is an open-source, local-first speech-to-text server
that provides an OpenAI-compatible HTTP API at /v1/audio/transcriptions.
https://openasr.org/docs/server/
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
from ..exceptions import TranscriptionError, ConfigurationError

logger = logging.getLogger(__name__)


class OpenASRTranscriptionConnector(BaseTranscriptionConnector):
    """Connector for local OpenASR server."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.CHUNKING,
        TranscriptionCapability.HOTWORDS,
        TranscriptionCapability.INITIAL_PROMPT,
    }
    PROVIDER_NAME = "openasr"

    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        base_url = config.get('base_url', '').rstrip('/')
        if not base_url.endswith('/v1'):
            base_url = base_url + '/v1'
        self.base_url = base_url
        self.api_key = config.get('api_key', '')
        self.model = config.get('model', '')
        self.default_diarize = config.get('diarize', False)
        self._config_timeout = config.get('timeout', 1800)
        super().__init__(config)

    def _validate_config(self) -> None:
        if not self.config.get('base_url'):
            raise ConfigurationError(
                "base_url is required for OpenASR connector. "
                "Set TRANSCRIPTION_BASE_URL environment variable."
            )

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        try:
            effective_model = self._effective_model(request) or self.model
            model = effective_model or 'qwen3-asr-0.6b'
            url = self.base_url + '/audio/transcriptions'

            files = {
                'file': (request.filename, request.audio_file, request.mime_type or 'application/octet-stream'),
                'model': (None, model),
                'response_format': (None, 'verbose_json'),
            }

            should_diarize = request.diarize if request.diarize is not None else self.default_diarize
            if should_diarize:
                files['diarize'] = (None, 'true')

            if request.language:
                files['language'] = (None, request.language)

            # Combine initial prompt and hotwords into the OpenAI-compatible
            # 'prompt' field, the same way the openai_whisper connector does —
            # the declared HOTWORDS / INITIAL_PROMPT capabilities are honored
            # through this single parameter.
            prompt_parts = []
            if request.prompt:
                prompt_parts.append(request.prompt)
            if request.hotwords:
                prompt_parts.append(request.hotwords)
            if prompt_parts:
                files['prompt'] = (None, '. '.join(prompt_parts))

            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'

            timeout = httpx.Timeout(
                None, connect=30.0, read=float(self._config_timeout), write=float(self._config_timeout)
            )

            logger.info(f"OpenASR transcription: model={model}, diarize={should_diarize}")
            with httpx.Client() as client:
                response = client.post(url, files=files, headers=headers, timeout=timeout)
                response.raise_for_status()
                data = response.json()

            return self._parse_response(data, model, should_diarize)

        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text
            except Exception:
                body = ''
            body_excerpt = body.strip()[:800]
            logger.error(f"OpenASR request failed: {e.response.status_code} {body_excerpt}")
            raise TranscriptionError(
                f"OpenASR request failed (status {e.response.status_code}): {body_excerpt}"
            ) from e

        except httpx.TimeoutException as e:
            logger.error(f"OpenASR request timed out after {self._config_timeout}s")
            raise TranscriptionError(f"OpenASR request timed out after {self._config_timeout}s") from e

        except Exception as e:
            error_msg = str(e)
            logger.error(f"OpenASR transcription failed: {error_msg}")
            raise TranscriptionError(f"OpenASR transcription failed: {error_msg}") from e

    def _parse_response(self, data: Dict[str, Any], model: str, diarize: bool) -> TranscriptionResponse:
        segments = []
        speakers = set()
        full_text = data.get('text', '')

        for seg in data.get('segments', []):
            text = seg.get('text', '').strip()
            if diarize:
                speaker = seg.get('speaker')
                if speaker:
                    speakers.add(speaker)
            else:
                speaker = None
            segments.append(TranscriptionSegment(
                text=text,
                speaker=speaker,
                start_time=seg.get('start'),
                end_time=seg.get('end'),
            ))

        # Natural sort so SPEAKER_2 orders before SPEAKER_10.
        def _speaker_key(name):
            head, _, tail = name.rpartition('_')
            return (head, int(tail)) if tail.isdigit() else (name, -1)

        return TranscriptionResponse(
            text=full_text,
            segments=segments or None,
            speakers=sorted(speakers, key=_speaker_key) if speakers else None,
            language=data.get('language'),
            provider=self.PROVIDER_NAME,
            model=model,
            raw_response=data,
        )

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "OpenASR server URL (e.g. http://127.0.0.1:8080)"
                },
                "api_key": {
                    "type": "string",
                    "description": "API key (optional for loopback)"
                },
                "model": {
                    "type": "string",
                    "description": "Model name (e.g. qwen3-asr-0.6b)"
                },
                "diarize": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable speaker diarization"
                },
                "timeout": {
                    "type": "integer",
                    "default": 1800,
                    "description": "Timeout in seconds"
                },
            },
        }
