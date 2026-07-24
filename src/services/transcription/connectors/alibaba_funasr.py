"""
Alibaba FunASR connector

Supports Alibaba Cloud FunASR (DAMO Academy speech recognition) service.
Docs: https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-restful-api
"""

import logging
import os
import httpx
import json
import time
from typing import Dict, Any, Set, Optional
from urllib.parse import urlparse, urlunparse

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


class AlibabaFunASRConnector(BaseTranscriptionConnector):
    """Alibaba Cloud FunASR connector."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,
    }
    PROVIDER_NAME = "funasr"

    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,  # FunASR handles its own chunking
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Alibaba FunASR connector.

        Args:
            config: Configuration dict with keys:
                - base_url: FunASR API base URL (required)
                - api_key: API key (required)
                - model: Model name (default: "fun-asr")
                - timeout: Transcription timeout in seconds (default: 1800)
                - poll_interval: Seconds between status polls (default: 10)
                - diarize: Enable speaker diarization (default: False)
                - disfluency_removal_enabled: Filter filler words (default: False)
                - timestamp_alignment_enabled: Timestamp alignment (default: False)
                - speaker_count: Speaker count hint (2-100)
                - vocabulary_id: Hot-word ID
                - language_hints: Language hint array (default: ["zh","en"])
                - channel_id: Channel indices (default: None = [0])
        """
        base_url = config.get('base_url', '')
        api_key = config.get('api_key', '')
        model = config.get('model', 'fun-asr')

        self.base_url = base_url.rstrip('/') if base_url else ''
        self.api_key = api_key
        self.model = model
        self._config_timeout = config.get('timeout', 1800)
        self.poll_interval = config.get('poll_interval', 10)
        self.default_diarize = config.get('diarize', False)
        self.disfluency_removal_enabled = config.get('disfluency_removal_enabled', False)
        self.timestamp_alignment_enabled = config.get('timestamp_alignment_enabled', False)
        self.speaker_count = config.get('speaker_count', None)
        self.vocabulary_id = config.get('vocabulary_id', None)
        self.language_hints = config.get('language_hints', ['zh', 'en'])
        self.channel_id = config.get('channel_id', None)

        super().__init__(config)

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.base_url:
            raise ConfigurationError(
                "base_url is required for Alibaba FunASR connector. "
                "Set ASR_BASE_URL environment variable, "
                "or provide base_url in config."
            )
        if not self.api_key:
            raise ConfigurationError(
                "api_key is required for Alibaba FunASR connector. "
                "Set ASR_API_KEY environment variable, "
                "or provide api_key in config."
            )

    @property
    def timeout(self):
        """Get timeout with env-var override, falling back to config default."""
        env_timeout = os.environ.get('ASR_TIMEOUT') or os.environ.get('asr_timeout_seconds')
        if env_timeout:
            try:
                return int(env_timeout)
            except (ValueError, TypeError):
                pass

        return self._config_timeout

    def _poll_task_result(self, task_id: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """
        Poll async task until completion or timeout.

        Args:
            task_id: Task ID from the async submission
            headers: Request headers (Authorization, etc.)

        Returns:
            Task result data on success
        """
        task_url = f"{self.base_url}/tasks/{task_id}"

        max_attempts = int(self.timeout / self.poll_interval)

        logger.info(f"Polling task result, task_id: {task_id}")

        with httpx.Client() as client:
            for attempt in range(max_attempts):
                try:
                    if attempt % 10 == 0:
                        logger.info(f"Poll attempt {attempt + 1}/{max_attempts}")

                    response = client.get(task_url, headers=headers, timeout=30.0)
                    response.raise_for_status()

                    data = response.json()
                    task_status = data.get('output', {}).get('task_status', 'UNKNOWN')

                    if attempt % 10 == 0:
                        logger.info(f"Task status: {task_status}")

                    if task_status == 'SUCCEEDED':
                        logger.info("Task completed successfully")
                        return data
                    elif task_status == 'FAILED':
                        output = data.get('output', {})
                        error_code = output.get('code', 'UNKNOWN_ERROR')
                        error_msg = output.get('message', 'Task failed')

                        logger.debug(f"FunASR full response: {json.dumps(data, indent=2)}")
                        logger.error(f"FunASR error code: {error_code}, message: {error_msg}")

                        if error_code == 'ASR_RESPONSE_HAVE_NO_WORDS':
                            raise TranscriptionError(
                                "No speech detected in audio file. Please check:\n"
                                "1. Audio contains clear speech\n"
                                "2. Audio volume is adequate\n"
                                "3. Audio format is correct (WAV, 16kHz, mono recommended)\n"
                                "4. Try with a known-good sample audio"
                            )

                        if error_code == 'SUCCESS_WITH_NO_VALID_FRAGMENT':
                            logger.warning(f"FunASR task succeeded but no valid audio fragments: {error_msg}")
                            return {
                                'output': {
                                    'task_status': 'SUCCEEDED',
                                    'results': [],
                                    'message': error_msg
                                }
                            }

                        raise ProviderError(
                            f"FunASR task failed: {error_msg} (code: {error_code})",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )
                    elif task_status in ('PENDING', 'RUNNING'):
                        time.sleep(self.poll_interval)
                    else:
                        logger.warning(f"Unknown task status: {task_status}")
                        time.sleep(self.poll_interval)

                except httpx.HTTPStatusError as e:
                    logger.error(f"Task query failed, status: {e.response.status_code}")
                    if attempt < max_attempts - 1:
                        time.sleep(self.poll_interval)
                    else:
                        raise
                except Exception as e:
                    logger.error(f"Task query error: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(self.poll_interval)
                    else:
                        raise

        raise TranscriptionError(f"Task polling timed out, task_id: {task_id}")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using Alibaba Cloud FunASR.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with segments and speaker info
        """
        try:
            url = f"{self.base_url}/services/audio/asr/transcription"

            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                'X-DashScope-Async': 'enable'
            }

            payload = {
                'model': self.model
            }

            # File URLs injected via extra_options (set up by caller before invoking)
            funasr_file_urls = (request.extra_options or {}).get('funasr_file_urls') or []
            if funasr_file_urls:
                payload['input'] = {
                    'file_urls': [funasr_file_urls[0]]
                }
            else:
                logger.error("FunASR requires file URL but none provided")
                raise TranscriptionError(
                    "FunASR requires file URL. Please upload file to S3 first."
                )

            # Build parameters dict
            parameters = {}
            if self.default_diarize:
                parameters['diarization_enabled'] = True
            if self.disfluency_removal_enabled:
                parameters['disfluency_removal_enabled'] = True
            if self.timestamp_alignment_enabled:
                parameters['timestamp_alignment_enabled'] = True
            if self.speaker_count:
                parameters['speaker_count'] = self.speaker_count
            if self.vocabulary_id:
                parameters['vocabulary_id'] = self.vocabulary_id
            if self.language_hints:
                parameters['language_hints'] = self.language_hints
            if self.channel_id:
                parameters['channel_id'] = self.channel_id
            if parameters:
                payload['parameters'] = parameters

            timeout = httpx.Timeout(
                None,
                connect=60.0,
                read=float(self.timeout),
                write=float(self.timeout),
                pool=None
            )

            logger.info(f"Sending FunASR request to {url}, payload: {json.dumps(payload, indent=2)[:200]}...")

            with httpx.Client() as client:
                response = client.post(url, json=payload, headers=headers, timeout=timeout)
                logger.info(f"FunASR request completed, status: {response.status_code}")
                response.raise_for_status()

                response_text = response.text
                try:
                    task_data = response.json()
                except Exception as json_err:
                    if response_text.strip().startswith('<'):
                        logger.error(f"FunASR returned HTML error page (status {response.status_code})")
                        raise ProviderError(
                            "FunASR service returned an HTML error page",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )
                    else:
                        raise ProviderError(
                            f"FunASR service returned invalid response: {json_err}",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )

                task_id = task_data.get('output', {}).get('task_id')
                if not task_id:
                    logger.error(f"No task_id in response: {task_data}")
                    raise ProviderError(
                        "FunASR did not return a task_id",
                        provider=self.PROVIDER_NAME,
                        status_code=response.status_code
                    )

                logger.info(f"Async task submitted, task_id: {task_id}")

                result_data = self._poll_task_result(task_id, headers)

                output = result_data.get('output', {})
                results = output.get('results', [])

                if not results:
                    raise TranscriptionError("FunASR returned empty results")

                subtask_status = results[0].get('subtask_status', 'UNKNOWN')
                if subtask_status == 'FAILED':
                    error_code = results[0].get('code', 'UNKNOWN')
                    error_msg = results[0].get('message', 'Subtask failed')
                    raise TranscriptionError(f"FunASR subtask failed: {error_msg} (code: {error_code})")

                transcription_url = results[0].get('transcription_url')
                if not transcription_url:
                    raise TranscriptionError("FunASR response missing transcription_url")

                logger.info(f"Downloading transcription result: {transcription_url[:100]}...")
                transcription_data = self._download_transcription_result(transcription_url)

                return self._parse_transcription_data(transcription_data)

        except httpx.HTTPStatusError as e:
            logger.error(f"FunASR request failed, status {e.response.status_code}")
            error_detail = ""
            try:
                error_response = e.response.json()
                error_detail = f": {json.dumps(error_response)}"
            except Exception:
                error_detail = f": {e.response.text[:200]}"

            raise ProviderError(
                f"FunASR request failed, status {e.response.status_code}{error_detail}",
                provider=self.PROVIDER_NAME,
                status_code=e.response.status_code
            ) from e

        except httpx.TimeoutException as e:
            logger.error(f"FunASR request timed out ({self.timeout}s)")
            raise TranscriptionError(f"FunASR request timed out ({self.timeout}s)") from e

        except Exception as e:
            error_msg = str(e)
            logger.error(f"FunASR transcription failed: {error_msg}")
            raise TranscriptionError(f"FunASR transcription failed: {error_msg}") from e

    def _download_transcription_result(self, transcription_url: str) -> Dict[str, Any]:
        """
        Download transcription result from the URL returned by FunASR.

        Args:
            transcription_url: URL to the transcription result JSON

        Returns:
            Transcription result data
        """
        try:
            logger.info(f"Downloading transcription result: {transcription_url[:100]}...")

            with httpx.Client(timeout=30.0) as client:
                response = client.get(transcription_url)
                response.raise_for_status()

                result_data = response.json()
                logger.info(f"Transcription result downloaded successfully, size: {len(str(result_data))} chars")
                return result_data

        except httpx.HTTPStatusError as e:
            logger.error(f"Transcription result download failed, status: {e.response.status_code}")
            raise ProviderError(
                f"Transcription result download failed, status: {e.response.status_code}",
                provider=self.PROVIDER_NAME,
                status_code=e.response.status_code
            ) from e
        except Exception as e:
            logger.error(f"Transcription result download error: {e}")
            raise TranscriptionError(f"Transcription result download failed: {e}") from e

    def _parse_transcription_data(self, data: Dict[str, Any]) -> TranscriptionResponse:
        """
        Parse actual transcription result downloaded from transcription_url.

        The data format (per Aliyun docs):
        {
          "file_url": "...",
          "transcripts": [{
            "channel_id": 0,
            "text": "...",
            "sentences": [{
              "begin_time": 100,
              "end_time": 3820,
              "text": "...",
              "sentence_id": 1,
              "speaker_id": 0,
              "words": [...]
            }]
          }]
        }
        """
        try:
            segments = []
            speakers = set()
            full_text_parts = []

            transcripts = data.get('transcripts', [])
            logger.info(f"Parsing transcription data, transcripts count: {len(transcripts)}")

            for transcript in transcripts:
                sentences = transcript.get('sentences', [])
                for sentence in sentences:
                    text = sentence.get('text', '').strip()
                    if not text:
                        continue

                    speaker_num = sentence.get('speaker_id', 0)
                    speaker_id = f"SPEAKER_{speaker_num}"

                    # Aliyun timestamps are in milliseconds, convert to seconds
                    begin_ms = sentence.get('begin_time', 0)
                    end_ms = sentence.get('end_time', 0)
                    try:
                        start_time = float(begin_ms) / 1000.0
                    except (ValueError, TypeError):
                        start_time = 0.0
                    try:
                        end_time = float(end_ms) / 1000.0
                    except (ValueError, TypeError):
                        end_time = 0.0

                    segment = TranscriptionSegment(
                        text=text,
                        speaker=speaker_id,
                        start_time=start_time,
                        end_time=end_time,
                    )
                    segments.append(segment)
                    speakers.add(speaker_id)
                    full_text_parts.append(f"[{speaker_id}]: {text}")

            full_text = '\n'.join(full_text_parts) if full_text_parts else ''

            logger.info(f"Parse complete: {len(segments)} segments, {len(speakers)} speakers")

            return TranscriptionResponse(
                text=full_text,
                segments=segments,
                speakers=sorted(list(speakers)),
                language=None,
                provider=self.PROVIDER_NAME,
                model=self.model,
                raw_response=data
            )

        except Exception as e:
            logger.error(f"Failed to parse FunASR response: {e}")
            raise TranscriptionError(f"Failed to parse FunASR response: {e}") from e

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for connector configuration."""
        return {
            "type": "object",
            "required": ["base_url", "api_key"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "FunASR base URL (e.g. https://dashscope.aliyuncs.com/api/v1)"
                },
                "api_key": {
                    "type": "string",
                    "description": "FunASR API key"
                },
                "model": {
                    "type": "string",
                    "default": "fun-asr",
                    "description": "Model name"
                },
                "timeout": {
                    "type": "integer",
                    "default": 1800,
                    "description": "Request timeout in seconds"
                },
                "diarize": {
                    "type": "boolean",
                    "default": True,
                    "description": "Enable speaker diarization"
                },
                "disfluency_removal_enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "Filter filler words"
                },
                "timestamp_alignment_enabled": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable timestamp alignment"
                },
                "language_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Language hint array"
                }
            }
        }


def prepare_funasr_file_url(recording) -> tuple:
    """Prepare S3 file URL for FunASR transcription.

    Uploads the recording to S3-compatible storage and returns a pre-signed
    URL suitable for passing to the FunASR API.

    Returns:
        tuple: (bool, list) - (success, file URLs)
    """
    from flask import current_app
    from src.config.app_config import S3_INTRANET_ENDPOINT_URL

    from src.services.storage import get_storage_service
    from src.services.storage.locator import parse_locator

    storage = get_storage_service()
    if not storage.s3:
        raise ConfigurationError(
            "FunASR requires S3 storage to be configured. "
            "Please set S3_BUCKET_NAME, S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, "
            "and S3_SECRET_ACCESS_KEY."
        )

    from urllib.parse import urlparse, parse_qs
    import re
    import datetime

    if recording.bucket_urls:
        # Check if the pre-signed URL is still valid (not expired)
        _now = datetime.datetime.utcnow()
        _any_valid = False
        for _url in recording.bucket_urls:
            _parsed = urlparse(_url)
            _params = parse_qs(_parsed.query)
            _date_str = _params.get('X-Amz-Date', [None])[0]
            _expires = _params.get('X-Amz-Expires', [None])[0]
            if _date_str and _expires:
                try:
                    _sig_time = datetime.datetime.strptime(_date_str, '%Y%m%dT%H%M%SZ')
                    if (_now - _sig_time).total_seconds() < int(_expires):
                        _any_valid = True
                        break
                except (ValueError, TypeError):
                    _any_valid = True
                    break
            else:
                _any_valid = True
                break
        if _any_valid:
            current_app.logger.info(f"Recording {recording.id} bucket_urls still valid, reusing")
            return True, recording.bucket_urls
        else:
            current_app.logger.info(f"Recording {recording.id} bucket_urls expired, regenerating")

    locator_str = storage.maybe_normalize_local_legacy_locator(recording.audio_path)
    if not locator_str:
        locator_str = storage.build_local_locator_from_path(recording.audio_path)

    if not locator_str:
        current_app.logger.warning(f"Audio path not found for recording {recording.id}")
        return False, []

    locator_parsed = parse_locator(locator_str)
    if not locator_parsed:
        return False, []

    if not storage.exists(locator_str):
        current_app.logger.warning(f"Audio not found for recording {recording.id}")
        return False, []

    from os.path import splitext
    from werkzeug.utils import secure_filename

    _orig_name = recording.original_filename or f"recording_{recording.id}"
    _stem, _orig_ext = splitext(_orig_name)
    _mime_ext_map = {'audio/mpeg': '.mp3', 'audio/flac': '.flac', 'audio/opus': '.opus', 'audio/wav': '.wav'}
    _ext = _mime_ext_map.get(recording.mime_type, _orig_ext)
    safe_filename = secure_filename(_stem + _ext)
    object_key = f"funasr/{recording.id}/{safe_filename}"
    s3_locator = parse_locator(storage.s3.build_locator(object_key))

    if not storage.s3.exists(s3_locator):
        current_app.logger.info(f"Uploading recording {recording.id} to S3: {object_key}")
        with storage.materialize(locator_str) as materialized:
            storage.s3.upload_local_file(
                materialized.local_path,
                object_key,
                content_type=recording.mime_type or 'application/octet-stream',
            )

    url = storage.s3.presign_get_url(s3_locator, expires_seconds=86400)

    if S3_INTRANET_ENDPOINT_URL:
        from src.config.app_config import S3_ENDPOINT_URL
        url = url.replace(S3_ENDPOINT_URL, S3_INTRANET_ENDPOINT_URL)

    from src.database import db
    recording.bucket_urls = [url]
    db.session.flush()
    current_app.logger.info(f"FunASR URL ready for recording {recording.id}")
    return True, [url]
