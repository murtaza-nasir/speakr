"""
AssemblyAI transcription connector.

AssemblyAI is not OpenAI-compatible: it uses an async upload-then-poll REST API
(`/v2/upload` -> `/v2/transcript` -> poll `/v2/transcript/{id}`) with its own
diarization output (an `utterances` array, times in milliseconds). It handles
long multi-speaker files in a single job (up to 10 hours), so no app-side
chunking is needed.

Config (from env via the registry):
- api_key:  TRANSCRIPTION_API_KEY            (required; sent as the `authorization` header, no Bearer)
- base_url: TRANSCRIPTION_BASE_URL           (default https://api.assemblyai.com)
- model:    ASSEMBLYAI_SPEECH_MODEL          (optional; one or comma-separated AssemblyAI models
            -> `speech_models`; foreign/blank values fall back to the account default)
"""

import io
import time
import logging
from typing import Dict, Any, Set, Optional, List

import httpx

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


class AssemblyAITranscriptionConnector(BaseTranscriptionConnector):
    """Connector for AssemblyAI's async transcription API with diarization."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,  # speaker_options min/max
        TranscriptionCapability.HOTWORDS,               # word_boost
    }
    PROVIDER_NAME = "assemblyai"

    # AssemblyAI's own speech models. The transcription-model that reaches a
    # connector can be resolved from shared settings (env, admin default, tag /
    # folder defaults) that were meant for a DIFFERENT connector (e.g. WhisperX
    # 'large-v3' or OpenAI 'gpt-4o-transcribe-diarize'). Sending one of those to
    # AssemblyAI is a hard error, so we only forward models AssemblyAI knows and
    # otherwise fall back to the account default.
    KNOWN_MODELS = frozenset({'universal-3-pro', 'universal-2', 'universal', 'nano', 'best', 'slam-1'})

    # AssemblyAI ingests long files in a single async job (10h / up to 2.2GB on
    # upload), so the app must not chunk for this connector.
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        self.api_key = (config.get('api_key') or '').strip()
        base = (config.get('base_url') or 'https://api.assemblyai.com').strip()
        self.base_url = base.rstrip('/')
        self.model = (config.get('model') or '').strip()
        # Poll behaviour: AssemblyAI jobs for long meetings take minutes, so the
        # ceiling is generous. Both are overridable via config for tuning/tests.
        self.poll_interval = float(config.get('poll_interval', 3.0))
        self.poll_timeout = float(config.get('poll_timeout', 3600.0))  # 1 hour
        # `boost_param` strength for word_boost (low | default | high).
        self.boost_param = (config.get('boost_param') or 'default').strip()
        super().__init__(config)

    def _validate_config(self) -> None:
        if not self.api_key:
            raise ConfigurationError("api_key is required for the AssemblyAI connector (set TRANSCRIPTION_API_KEY)")

    # -- HTTP helpers -------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        # AssemblyAI takes the raw key in `authorization`, with NO Bearer prefix.
        return {"authorization": self.api_key}

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, headers=self._headers(), timeout=60.0)

    def _upload(self, client: httpx.Client, audio_bytes: bytes) -> str:
        resp = client.post("/v2/upload", content=audio_bytes,
                            headers={"content-type": "application/octet-stream"})
        if resp.status_code != 200:
            raise ProviderError(f"AssemblyAI upload failed ({resp.status_code}): {resp.text[:300]}",
                                provider=self.PROVIDER_NAME, status_code=resp.status_code)
        url = resp.json().get('upload_url')
        if not url:
            raise ProviderError("AssemblyAI upload returned no upload_url", provider=self.PROVIDER_NAME)
        return url

    def _build_payload(self, request: TranscriptionRequest, audio_url: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "audio_url": audio_url,
            "punctuate": True,
            "format_text": True,
        }

        # Language: an explicit code disables auto-detection; otherwise detect.
        lang = (request.language or '').strip().lower()
        if lang and lang not in ('auto', 'any', 'detect'):
            payload["language_code"] = request.language
        else:
            payload["language_detection"] = True

        # Diarization + speaker-count hinting.
        if request.diarize:
            payload["speaker_labels"] = True
            mn, mx = request.min_speakers, request.max_speakers
            if mn and mx and int(mn) == int(mx):
                payload["speakers_expected"] = int(mn)
            else:
                opts = {}
                if mn:
                    opts["min_speakers_expected"] = int(mn)
                if mx:
                    opts["max_speakers_expected"] = int(mx)
                if opts:
                    payload["speaker_options"] = opts

        # Hotwords -> word_boost (comma-separated string to list).
        if request.hotwords:
            words = [w.strip() for w in request.hotwords.split(',') if w.strip()]
            if words:
                payload["word_boost"] = words
                payload["boost_param"] = self.boost_param

        # Optional model selection (account default when blank). AssemblyAI
        # deprecated the singular `speech_model` in favour of `speech_models`
        # (an ordered fallback array). Only forward models AssemblyAI recognises;
        # a foreign model name resolved from shared settings is dropped so we
        # fall back to the account default rather than erroring.
        model = self._effective_model(request)
        if model:
            candidates = [m.strip() for m in model.split(',') if m.strip()]
            valid = [m for m in candidates if m.lower() in self.KNOWN_MODELS]
            if valid:
                payload["speech_models"] = valid
            elif candidates:
                logger.info("AssemblyAI: ignoring non-AssemblyAI model(s) %s; using account default", candidates)

        return payload

    def _poll(self, client: httpx.Client, transcript_id: str) -> Dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            resp = client.get(f"/v2/transcript/{transcript_id}")
            if resp.status_code != 200:
                raise ProviderError(
                    f"AssemblyAI poll failed ({resp.status_code}): {resp.text[:300]}",
                    provider=self.PROVIDER_NAME, status_code=resp.status_code)
            data = resp.json()
            status = data.get('status')
            if status == 'completed':
                return data
            if status == 'error':
                raise TranscriptionError(f"AssemblyAI transcription failed: {data.get('error', 'unknown error')}")
            if time.monotonic() > deadline:
                raise TranscriptionError(
                    f"AssemblyAI transcription timed out after {self.poll_timeout:.0f}s "
                    f"(transcript {transcript_id} still '{status}')")
            time.sleep(self.poll_interval)

    # -- Main entry ---------------------------------------------------------

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        try:
            audio_bytes = self._read_audio(request)
            with self._client() as client:
                audio_url = self._upload(client, audio_bytes)
                payload = self._build_payload(request, audio_url)
                logger.info(
                    "AssemblyAI: submitting transcript (diarize=%s, lang=%s, models=%s)",
                    request.diarize, payload.get('language_code', 'auto'), payload.get('speech_models', 'default'))
                resp = client.post("/v2/transcript", json=payload,
                                   headers={"content-type": "application/json"})
                if resp.status_code not in (200, 201):
                    raise ProviderError(
                        f"AssemblyAI submit failed ({resp.status_code}): {resp.text[:300]}",
                        provider=self.PROVIDER_NAME, status_code=resp.status_code)
                transcript_id = resp.json().get('id')
                if not transcript_id:
                    raise ProviderError("AssemblyAI submit returned no transcript id", provider=self.PROVIDER_NAME)
                result = self._poll(client, transcript_id)
            return self._parse_result(result)
        except (TranscriptionError, ConfigurationError):
            raise
        except Exception as e:
            logger.error("AssemblyAI transcription failed: %s", e)
            raise TranscriptionError(f"AssemblyAI transcription failed: {e}") from e

    @staticmethod
    def _read_audio(request: TranscriptionRequest) -> bytes:
        f = request.audio_file
        try:
            if hasattr(f, 'seek'):
                f.seek(0)
        except (OSError, io.UnsupportedOperation):
            pass
        data = f.read()
        if not data:
            raise TranscriptionError("AssemblyAI: empty audio file")
        return data

    def _parse_result(self, result: Dict[str, Any]) -> TranscriptionResponse:
        text = result.get('text') or ''
        language = result.get('language_code')
        duration = result.get('audio_duration')  # seconds

        utterances = result.get('utterances')
        if utterances:
            segments: List[TranscriptionSegment] = []
            speakers = set()
            text_parts = []
            for u in utterances:
                spk = u.get('speaker') or 'Unknown'
                utext = (u.get('text') or '').strip()
                if not utext:
                    continue
                speakers.add(spk)
                text_parts.append(f"[{spk}]: {utext}")
                segments.append(TranscriptionSegment(
                    text=utext,
                    speaker=spk,
                    start_time=_ms_to_s(u.get('start')),
                    end_time=_ms_to_s(u.get('end')),
                    confidence=u.get('confidence'),
                ))
            logger.info("AssemblyAI: %d utterances, %d speakers", len(segments), len(speakers))
            return TranscriptionResponse(
                text='\n'.join(text_parts) if text_parts else text,
                segments=segments,
                speakers=sorted(speakers),
                language=language,
                duration=duration,
                provider=self.PROVIDER_NAME,
                model=self.model or 'default',
                raw_response=result,
            )

        # No diarization: plain text.
        return TranscriptionResponse(
            text=text,
            language=language,
            duration=duration,
            provider=self.PROVIDER_NAME,
            model=self.model or 'default',
            raw_response=result,
        )

    def health_check(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "AssemblyAI API key"},
                "base_url": {"type": "string", "default": "https://api.assemblyai.com",
                              "description": "API base URL"},
                "model": {"type": "string", "default": "",
                           "description": "Optional speech_model (account default when blank)"},
            },
        }


def _ms_to_s(ms: Optional[int]) -> Optional[float]:
    """AssemblyAI reports times in milliseconds; the app stores seconds."""
    if ms is None:
        return None
    try:
        return round(ms / 1000.0, 3)
    except (TypeError, ValueError):
        return None
