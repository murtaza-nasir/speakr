"""MOSS transcription through the hosted MOSI/Mossland API.

Recordings sent through this connector leave the self-hosted Speakr instance
and are processed by the third-party service at api.mosi.cn.
"""

import json
import logging
import time
from typing import Any

import httpx

from ..base import (
    BaseTranscriptionConnector, ConnectorSpecifications, TranscriptionCapability,
    TranscriptionRequest, TranscriptionResponse, TranscriptionSegment,
)
from ..exceptions import ConfigurationError, ProviderError, TranscriptionError

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.mosi.cn"
_DEFAULT_MODEL = "moss-transcribe-diarize"
_STREAMING_VERSION = "v20260410-streamparam-20260703"
_DIARIZE_VERSION = "moss-transcribe-diarize-20260325"
_SAMPLING_PARAMS = json.dumps({"max_new_tokens": 131072, "temperature": 0})
_SAFE_ASYNC_FALLBACK_STATUSES = frozenset({400, 404, 405})
_MODELS: dict[str, dict[str, Any]] = {
    "moss-transcribe-diarize": {
        "diarize": True, "streaming_version": _STREAMING_VERSION,
        "async_version": _DIARIZE_VERSION,
        "description": "Multi-speaker transcription with diarization and timestamps",
    },
    "moss-transcribe": {"diarize": False, "description": "Single-speaker transcription"},
}


class _StreamInterrupted(TranscriptionError):
    def __init__(self, message: str, task_id: str | None = None):
        super().__init__(message)
        self.task_id = task_id


class MosslandTranscriptionConnector(BaseTranscriptionConnector):
    """Use MOSI streaming and recover stalled jobs through task polling."""

    CAPABILITIES: set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.HOTWORDS,
    }
    PROVIDER_NAME = "mossland"

    # Any declared hard limit makes Speakr chunk before it checks this flag.
    # Leave limits unset so one MOSI task retains speaker identity end to end.
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None, max_duration_seconds=None,
        handles_chunking_internally=True, recommended_chunk_seconds=0,
    )

    def __init__(self, config: dict[str, Any]):
        self.api_key = (config.get("api_key") or "").strip()
        self.base_url = (config.get("base_url") or _DEFAULT_BASE_URL).strip().rstrip("/")
        self.model = (config.get("model") or _DEFAULT_MODEL).strip()
        self.poll_interval = float(config.get("poll_interval", 5.0))
        self.poll_timeout = float(config.get("poll_timeout", 7200.0))
        self.stream_read_timeout = float(config.get("stream_read_timeout", 180.0))
        super().__init__(config)
        if not self._model_info(self.model)["diarize"]:
            self.CAPABILITIES = self.CAPABILITIES - {TranscriptionCapability.DIARIZATION}

    def _validate_config(self) -> None:
        if not self.api_key:
            raise ConfigurationError("api_key is required (set TRANSCRIPTION_API_KEY)")
        if not self.model.startswith("moss-transcribe"):
            raise ConfigurationError(
                "Mossland model must be a transcription model returned by GET /v1/models"
            )

    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(
            connect=120.0, read=self.stream_read_timeout, write=300.0, pool=120.0
        )
        return httpx.Client(
            base_url=self.base_url, timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)",
            },
        )

    @staticmethod
    def _read_audio(request: TranscriptionRequest) -> bytes:
        if hasattr(request.audio_file, "seek"):
            request.audio_file.seek(0)
        data = request.audio_file.read()
        if not data:
            raise TranscriptionError("Mossland: empty audio file")
        return data

    def _effective_mossland_model(self, request: TranscriptionRequest) -> str:
        model = self._effective_model(request) or self.model
        if model.startswith("moss-transcribe"):
            return model
        logger.warning("Ignoring non-transcription model %r; using %r", model, self.model)
        return self.model

    @staticmethod
    def _model_info(model: str) -> dict[str, Any]:
        if model in _MODELS:
            return _MODELS[model]
        diarize = "diarize" in model.lower()
        return {
            "diarize": diarize,
            "streaming_version": _STREAMING_VERSION if diarize else None,
            "async_version": _DIARIZE_VERSION if diarize else None,
            "description": model,
        }

    def _form_data(
        self, request: TranscriptionRequest, model: str, mode: str
    ) -> dict[str, str]:
        info = self._model_info(model)
        data = {"model": model, "response_format": "json"}
        if info["diarize"]:
            data.update(diarize="true", sampling_params=_SAMPLING_PARAMS)
        if version := info.get(f"{mode}_version"):
            data["version"] = version
        if mode in ("streaming", "async"):
            data["stream" if mode == "streaming" else "async"] = "true"
        if request.hotwords:
            data["hotwords"] = request.hotwords
        return data

    @staticmethod
    def _files(request: TranscriptionRequest, audio: bytes) -> dict:
        return {"file": (request.filename or "audio", audio, request.mime_type or "audio/mpeg")}

    @staticmethod
    def _segments(raw_segments: list[dict[str, Any]]) -> list[TranscriptionSegment]:
        result = []
        for raw in raw_segments:
            text = (raw.get("text") or "").strip()
            if text:
                result.append(TranscriptionSegment(
                    text=text, speaker=raw.get("speaker") or None,
                    start_time=float(raw.get("start", 0) or 0),
                    end_time=float(raw.get("end", 0) or 0),
                ))
        return result

    @classmethod
    def _result(
        cls, text: str, segments: list[TranscriptionSegment], model: str,
        raw: dict[str, Any] | None = None,
    ) -> TranscriptionResponse:
        text = text or " ".join(segment.text for segment in segments)
        speakers = sorted({segment.speaker for segment in segments if segment.speaker})
        metadata = raw or {}
        return TranscriptionResponse(
            text=text, segments=segments or None, speakers=speakers or None,
            language=metadata.get("language"), duration=metadata.get("duration"),
            provider=cls.PROVIDER_NAME, model=model, raw_response=raw,
        )

    @classmethod
    def _api_error(cls, label: str, response: httpx.Response) -> ProviderError:
        return ProviderError(
            f"Mossland {label} failed ({response.status_code}): {response.text[:300]}",
            provider=cls.PROVIDER_NAME, status_code=response.status_code,
        )

    def _transcribe_primary(
        self, client: httpx.Client, request: TranscriptionRequest,
        audio: bytes, model: str,
    ) -> TranscriptionResponse:
        if not self._model_info(model)["diarize"]:
            response = client.post(
                "/v1/audio/transcriptions", files=self._files(request, audio),
                data=self._form_data(request, model, "sync"),
            )
            if response.status_code != 200:
                raise self._api_error("transcription", response)
            payload = response.json()
            return self._result(
                payload.get("text") or "", self._segments(payload.get("segments") or []),
                model, payload,
            )

        segments, deltas = [], []
        text, task_id = "", None
        try:
            with client.stream(
                "POST", "/v1/audio/transcriptions",
                files=self._files(request, audio),
                data=self._form_data(request, model, "streaming"),
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise self._api_error("streaming", response)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if not value or value == "[DONE]":
                        continue
                    try:
                        event = json.loads(value)
                    except json.JSONDecodeError:
                        logger.debug("Ignoring malformed Mossland SSE event")
                        continue
                    kind = event.get("type")
                    if kind == "task.created":
                        task_id = event.get("task_id") or event.get("id") or task_id
                    elif kind == "transcript.segment.done":
                        segments.extend(self._segments([event]))
                    elif kind == "transcript.text.delta":
                        deltas.append(event.get("delta") or "")
                    elif kind == "transcript.text.done":
                        text = event.get("text") or text
        except httpx.HTTPError as error:
            raise _StreamInterrupted(f"Mossland SSE interrupted: {error}", task_id) from error

        text = text or "".join(deltas)
        if text or segments:
            return self._result(text, segments, model)
        if task_id:
            raise _StreamInterrupted("Mossland SSE ended before transcript", task_id)
        raise TranscriptionError("Mossland streaming returned no transcript or task id")

    @staticmethod
    def _sleep_for_poll(delay: float, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(max(0.0, delay), remaining))

    def _poll_task(
        self, client: httpx.Client, task_id: str, model: str,
        retry_after: float | None = None,
    ) -> TranscriptionResponse:
        deadline = time.monotonic() + self.poll_timeout
        paths = [f"/v1/audio/transcriptions/{task_id}", f"/v1/audio/tasks/{task_id}"]
        path_index = 0
        delay = self.poll_interval if retry_after is None else float(retry_after)
        while True:
            try:
                response = client.get(paths[path_index], timeout=30.0)
            except httpx.HTTPError as error:
                if time.monotonic() >= deadline:
                    raise TranscriptionError(f"Mossland task {task_id} unreachable") from error
                logger.warning("Mossland poll transport error; retrying: %s", error)
                self._sleep_for_poll(delay, deadline)
                continue
            if response.status_code in (404, 405) and path_index == 0:
                path_index = 1
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if time.monotonic() >= deadline:
                    raise TranscriptionError(f"Mossland task {task_id} did not recover")
                self._sleep_for_poll(delay, deadline)
                continue
            if response.status_code != 200:
                raise self._api_error("poll", response)

            payload = response.json()
            status = (payload.get("status") or "").upper()
            if status in ("SUCCESS", "COMPLETED"):
                return self._result(
                    payload.get("text") or "", self._segments(payload.get("segments") or []),
                    model, payload,
                )
            if status in ("FAILED", "ERROR"):
                detail = payload.get("error") or payload.get("message") or status
                if isinstance(detail, dict):
                    detail = detail.get("error_msg") or detail.get("message") or str(detail)
                raise TranscriptionError(f"Mossland task failed: {detail}")
            if time.monotonic() >= deadline:
                raise TranscriptionError(
                    f"Mossland transcription timed out after {self.poll_timeout:.0f}s "
                    f"(task {task_id} still {status or 'pending'})"
                )
            if payload.get("retry_after") is not None:
                delay = float(payload["retry_after"])
            self._sleep_for_poll(delay, deadline)

    def _transcribe_async(
        self, client: httpx.Client, request: TranscriptionRequest,
        audio: bytes, model: str,
    ) -> TranscriptionResponse:
        # Never retry this POST: an ambiguous retry can create a second billable task.
        response = client.post(
            "/v1/audio/transcriptions", files=self._files(request, audio),
            data=self._form_data(request, model, "async"),
        )
        if response.status_code not in (200, 201):
            raise self._api_error("async submit", response)
        payload = response.json()
        segments = self._segments(payload.get("segments") or [])
        status = (payload.get("status") or "").upper()
        if status in ("SUCCESS", "COMPLETED") and (payload.get("text") or segments):
            return self._result(payload.get("text") or "", segments, model, payload)
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            if payload.get("text") or segments:
                return self._result(payload.get("text") or "", segments, model, payload)
            raise ProviderError("Mossland async submit returned no task id", self.PROVIDER_NAME)
        return self._poll_task(client, task_id, model, payload.get("retry_after"))

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        audio = self._read_audio(request)
        model = self._effective_mossland_model(request)
        try:
            with self._client() as client:
                try:
                    return self._transcribe_primary(client, request, audio, model)
                except _StreamInterrupted as error:
                    if error.task_id:
                        logger.warning("Mossland SSE stalled; polling task %s", error.task_id)
                        return self._poll_task(client, error.task_id, model)
                    raise TranscriptionError(
                        "Mossland SSE failed before returning a task id; not resubmitting "
                        "to avoid a duplicate billable task"
                    ) from error
                except ProviderError as error:
                    safe = error.status_code in _SAFE_ASYNC_FALLBACK_STATUSES
                    if self._model_info(model)["diarize"] and safe:
                        logger.warning("Mossland SSE rejected; using async fallback")
                        return self._transcribe_async(client, request, audio, model)
                    raise
        except (ProviderError, TranscriptionError):
            raise
        except Exception as error:
            logger.error("Mossland transcription failed: %s", error)
            raise TranscriptionError(f"Mossland transcription failed: {error}") from error

    def health_check(self) -> bool:
        return bool(self.api_key)

    def list_models(self) -> list[dict[str, str]]:
        """Discover models on demand; use documented models while offline."""
        try:
            with self._client() as client:
                response = client.get("/v1/models", timeout=15.0)
                response.raise_for_status()
            models = [
                {"id": item["id"], "label": item["id"],
                 "owned_by": item.get("owned_by", "MOSI")}
                for item in response.json().get("data", [])
                if (item.get("id") or "").startswith("moss-transcribe")
            ]
            if models:
                return models
        except (httpx.HTTPError, ValueError, TypeError) as error:
            logger.warning("Mossland GET /v1/models failed; using fallback: %s", error)
        return [
            {"id": model, "label": model, "description": info["description"]}
            for model, info in _MODELS.items()
        ]

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object", "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "MOSI/Mossland API key"},
                "base_url": {"type": "string", "default": _DEFAULT_BASE_URL,
                             "description": "MOSI/Mossland API base URL"},
                "model": {"type": "string", "default": _DEFAULT_MODEL,
                          "description": "Model returned by MOSI GET /v1/models"},
            },
        }
