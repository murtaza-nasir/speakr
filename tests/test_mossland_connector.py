"""Contract and recovery tests for the MOSI/Mossland connector."""

import io
from unittest.mock import patch

import httpx
import pytest

from src.services.transcription.base import TranscriptionCapability, TranscriptionRequest
from src.services.transcription.connectors.mossland import MosslandTranscriptionConnector
from src.services.transcription.exceptions import ConfigurationError, TranscriptionError


class _Response:
    def __init__(self, status_code=200, payload=None, text="", lines=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self._lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        return self._payload

    def read(self):
        return self.text.encode()

    def iter_lines(self):
        for item in self._lines:
            if isinstance(item, Exception):
                raise item
            yield item

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.mosi.cn/v1/models")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(self.text, request=request, response=response)


class _Client:
    def __init__(self, stream_response=None, posts=None, gets=None, stream_error=None):
        self.stream_response = stream_response
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.stream_error = stream_error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, path, **kwargs):
        self.calls.append(("stream", path, kwargs))
        if self.stream_error:
            raise self.stream_error
        return self.stream_response

    def post(self, path, **kwargs):
        self.calls.append(("post", path, kwargs))
        return self.posts.pop(0)

    def get(self, path, **kwargs):
        self.calls.append(("get", path, kwargs))
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _connector(**config):
    values = {"api_key": "test-key", "poll_interval": 0}
    values.update(config)
    return MosslandTranscriptionConnector(values)


def _request(**values):
    values.setdefault("audio_file", io.BytesIO(b"audio-bytes"))
    values.setdefault("filename", "meeting.wav")
    values.setdefault("mime_type", "audio/wav")
    return TranscriptionRequest(**values)


def _success(text="One two"):
    return _Response(payload={
        "status": "SUCCESS",
        "text": text,
        "duration": 2.0,
        "segments": [
            {"speaker": "S01", "text": "One", "start": 0, "end": 1},
            {"speaker": "S02", "text": "two", "start": 1, "end": 2},
        ],
    })


def test_constructor_does_not_touch_network():
    with patch("src.services.transcription.connectors.mossland.httpx.Client") as client:
        connector = _connector()
    client.assert_not_called()
    assert connector.model == "moss-transcribe-diarize"


def test_long_form_specs_disable_speakr_chunking(monkeypatch):
    from src.audio_chunking import get_effective_chunking_config

    connector = _connector()
    specs = connector.specifications
    assert specs.handles_chunking_internally is True
    assert specs.max_duration_seconds is None
    assert specs.max_file_size_bytes is None
    assert specs.recommended_chunk_seconds == 0
    monkeypatch.setenv("ENABLE_CHUNKING", "true")
    monkeypatch.setenv("CHUNK_LIMIT", "20MB")
    effective = get_effective_chunking_config(specs)
    assert effective.enabled is False
    assert effective.source == "connector_internal"

    with pytest.raises(ConfigurationError, match="transcription model"):
        _connector(model="moss-tts")
    with pytest.raises(ConfigurationError, match="api_key"):
        MosslandTranscriptionConnector({"api_key": ""})


def test_sse_primary_uses_contract_and_parses_events():
    stream = _Response(lines=[
        'data: {"type":"task.created","task_id":"task-1"}',
        'data: {"type":"transcript.text.delta","delta":"Hello "}',
        'data: {"type":"transcript.segment.done","speaker":"S01","text":"Hello","start":0,"end":1.5}',
        'data: {"type":"transcript.text.done","text":"Hello there"}',
        "data: [DONE]",
    ])
    client = _Client(stream_response=stream)
    connector = _connector()

    with patch.object(connector, "_client", return_value=client):
        result = connector.transcribe(_request(hotwords="Speakr, MOSI"))

    assert result.text == "Hello there"
    assert result.speakers == ["S01"]
    _, path, kwargs = client.calls[0]
    assert path == "/v1/audio/transcriptions"
    assert kwargs["data"]["version"] == "v20260410-streamparam-20260703"
    assert kwargs["data"]["stream"] == "true"
    assert kwargs["data"]["response_format"] == "json"
    assert kwargs["data"]["hotwords"] == "Speakr, MOSI"
    assert '"max_new_tokens": 131072' in kwargs["data"]["sampling_params"]


def test_stalled_sse_resumes_existing_task_without_duplicate_post():
    timeout = httpx.ReadTimeout(
        "stalled", request=httpx.Request("POST", "https://api.mosi.cn/v1/audio/transcriptions")
    )
    client = _Client(
        stream_response=_Response(lines=[
            'data: {"type":"task.created","task_id":"task-existing"}',
            timeout,
        ]),
        gets=[
            _Response(status_code=404),
            _Response(payload={"status": "PROCESSING", "retry_after": 0}),
            _success("Recovered"),
        ],
    )
    connector = _connector()

    with patch.object(connector, "_client", return_value=client):
        result = connector.transcribe(_request())

    assert result.text == "Recovered"
    assert not [call for call in client.calls if call[0] == "post"]
    assert [call[1] for call in client.calls if call[0] == "get"] == [
        "/v1/audio/transcriptions/task-existing",
        "/v1/audio/tasks/task-existing",
        "/v1/audio/tasks/task-existing",
    ]


def test_safe_sse_rejection_uses_one_async_submission():
    client = _Client(
        stream_response=_Response(status_code=400, text="stream unsupported"),
        posts=[_Response(payload={"task_id": "task-2", "status": "PENDING", "retry_after": 0})],
        gets=[_Response(status_code=404), _success()],
    )
    connector = _connector()

    with patch.object(connector, "_client", return_value=client):
        result = connector.transcribe(_request())

    assert result.text == "One two"
    posts = [call for call in client.calls if call[0] == "post"]
    assert len(posts) == 1
    assert posts[0][2]["data"]["async"] == "true"
    assert posts[0][2]["data"]["version"] == "moss-transcribe-diarize-20260325"


def test_stream_failure_without_task_id_is_not_resubmitted():
    timeout = httpx.ReadTimeout(
        "stalled", request=httpx.Request("POST", "https://api.mosi.cn/v1/audio/transcriptions")
    )
    client = _Client(stream_error=timeout)
    connector = _connector()

    with patch.object(connector, "_client", return_value=client):
        with pytest.raises(TranscriptionError, match="not resubmitting"):
            connector.transcribe(_request())
    assert not [call for call in client.calls if call[0] == "post"]


def test_poll_recovers_from_transport_and_server_errors():
    connection_error = httpx.ConnectError(
        "offline", request=httpx.Request("GET", "https://api.mosi.cn/task")
    )
    client = _Client(gets=[
        connection_error,
        _Response(status_code=503),
        _success("Eventually completed"),
    ])
    connector = _connector()

    result = connector._poll_task(client, "task-retry", connector.model, retry_after=0)

    assert result.text == "Eventually completed"
    assert len([call for call in client.calls if call[0] == "get"]) == 3


def test_poll_timeout_is_terminal():
    client = _Client(gets=[_Response(payload={"status": "PROCESSING"})])
    connector = _connector(poll_timeout=0)

    with pytest.raises(TranscriptionError, match="timed out"):
        connector._poll_task(client, "task-timeout", connector.model, retry_after=0)


def test_standard_model_uses_synchronous_json_contract():
    client = _Client(posts=[_Response(payload={"text": "plain transcript"})])
    connector = _connector(model="moss-transcribe")

    with patch.object(connector, "_client", return_value=client):
        result = connector.transcribe(_request())

    assert result.text == "plain transcript"
    assert connector.supports(TranscriptionCapability.DIARIZATION) is False
    assert [call[0] for call in client.calls] == ["post"]
    data = client.calls[0][2]["data"]
    assert data["model"] == "moss-transcribe"
    assert "stream" not in data and "version" not in data
    assert "sampling_params" not in data


def test_models_are_discovered_on_demand_and_filtered():
    client = _Client(gets=[_Response(payload={"data": [
        {"id": "moss-transcribe", "owned_by": "OpenMOSS-Team"},
        {"id": "moss-transcribe-diarize", "owned_by": "OpenMOSS-Team"},
        {"id": "moss-tts", "owned_by": "OpenMOSS-Team"},
    ]})])
    connector = _connector()

    with patch.object(connector, "_client", return_value=client):
        models = connector.list_models()

    assert [model["id"] for model in models] == [
        "moss-transcribe", "moss-transcribe-diarize"
    ]
    assert client.calls[0][1] == "/v1/models"


def test_model_discovery_failure_uses_static_fallback():
    error = httpx.ConnectError(
        "offline", request=httpx.Request("GET", "https://api.mosi.cn/v1/models")
    )
    connector = _connector()
    with patch.object(connector, "_client", return_value=_Client(gets=[error])):
        models = connector.list_models()
    assert {model["id"] for model in models} == {
        "moss-transcribe", "moss-transcribe-diarize"
    }


def test_registry_maps_shared_environment_variables(monkeypatch):
    from src.services.transcription.registry import ConnectorRegistry

    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "env-key")
    monkeypatch.setenv("TRANSCRIPTION_BASE_URL", "https://proxy.example/v1 # proxy")
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "moss-transcribe")
    registry = object.__new__(ConnectorRegistry)

    assert registry._build_config_from_env("mossland") == {
        "api_key": "env-key",
        "base_url": "https://proxy.example/v1",
        "model": "moss-transcribe",
    }
