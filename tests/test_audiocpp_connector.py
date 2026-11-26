"""The audio.cpp connector (#700): requests, the details response, probes.

Responses are written after the documented /v1/audio/transcriptions/details
schema (app/server/api.md of audio.cpp v0.9.6): offsets in samples, converted
with sample_rate.
"""

import io
import json
import shutil
import wave

import httpx
import pytest

from src.services.transcription.base import TranscriptionCapability, TranscriptionRequest
from src.services.transcription.connectors.audiocpp import (
    AudioCppResponseError,
    AudioCppTranscriptionConnector,
)
from src.services.transcription.exceptions import (
    ConfigurationError,
    ProviderError,
    TranscriptionError,
)
from src.services.transcription.registry import ConnectorRegistry

RATE = 16000

DIARIZED = {
    "text": "Hello there. Hi, how are you? Fine.",
    "language": "en",
    "speaker_turns": [
        {"start_sample": 0, "end_sample": 2 * RATE, "speaker_id": "speaker_1",
         "confidence": 0.9, "text": "Hello there."},
        {"start_sample": 2 * RATE, "end_sample": 5 * RATE, "speaker_id": "speaker_0",
         "text": "Hi, how are you?"},
        {"start_sample": 5 * RATE, "end_sample": 6 * RATE, "speaker_id": "speaker_1",
         "text": "Fine."},
    ],
    "sample_rate": RATE,
    "timing": {"wall_ms": 100.0, "audio_duration_ms": 6000.0, "rtf": 0.02},
}

WORDS = [
    {"word": "Hello", "start_sample": 1000, "end_sample": 8000, "confidence": 0.9},
    {"word": "there.", "start_sample": 8000, "end_sample": 30000, "confidence": 0.9},
    {"word": "Hi,", "start_sample": 33000, "end_sample": 40000, "confidence": 0.9},
    {"word": "you?", "start_sample": 40000, "end_sample": 78000, "confidence": 0.9},
]


def connector(**config):
    values = {"base_url": "http://audiocpp.test:8080", "model": "vibevoice-asr"}
    values.update(config)
    return AudioCppTranscriptionConnector(values)


def wav_bytes(rate=RATE, channels=1, seconds=0.5):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * channels * int(rate * seconds))
    return buffer.getvalue()


# ------------------------------------------------------------------ config


def test_base_url_is_required_and_the_model_when_a_recording_is_sent():
    with pytest.raises(ConfigurationError):
        AudioCppTranscriptionConnector({"model": "m"})
    # Test and Discover models run before a model is chosen.
    without_model = AudioCppTranscriptionConnector({"base_url": "http://audiocpp.test:8080"})
    with pytest.raises(ConfigurationError, match="needs a model"):
        without_model.transcribe(request_for(wav_bytes()))


def test_no_key_field_and_a_trailing_v1_is_dropped():
    props = AudioCppTranscriptionConnector.get_config_schema()["properties"]
    assert "api_key" not in props
    assert connector(base_url="http://audiocpp.test:8080/v1/").base_url == "http://audiocpp.test:8080"


def test_diarization_capability_follows_the_model_setting():
    assert not connector().supports_diarization
    assert connector().default_diarize is False
    diarizing = connector(model_diarizes=True)
    assert diarizing.supports_diarization
    assert diarizing.default_diarize is True
    assert TranscriptionCapability.SPEAKER_COUNT_CONTROL not in diarizing.CAPABILITIES


def test_whole_recordings_unless_a_duration_limit_is_set():
    assert connector().specifications.handles_chunking_internally is True
    limited = connector(max_duration_seconds=3300).specifications
    assert limited.handles_chunking_internally is False
    assert limited.max_duration_seconds == 3300
    assert limited.recommended_chunk_seconds == 2640


def test_environment_config(monkeypatch):
    for name in ("TRANSCRIPTION_BASE_URL", "TRANSCRIPTION_MODEL", "ASR_DIARIZE",
                 "ASR_TIMEOUT", "AUDIOCPP_MAX_DURATION_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSCRIPTION_CONNECTOR", "audiocpp")
    monkeypatch.setenv("TRANSCRIPTION_BASE_URL", "http://audiocpp:8080  # comment")
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "vibevoice-asr")
    monkeypatch.setenv("ASR_DIARIZE", "true")
    monkeypatch.setenv("AUDIOCPP_MAX_DURATION_SECONDS", "3300")
    registry = ConnectorRegistry()
    assert registry._build_config_from_env("audiocpp") == {
        "base_url": "http://audiocpp:8080", "model": "vibevoice-asr",
        "model_diarizes": True, "timeout": 1800, "max_duration_seconds": 3300,
    }
    active = registry.initialize_from_env()
    assert isinstance(active, AudioCppTranscriptionConnector)
    assert active.supports_diarization


# ---------------------------------------------------------------- response


def test_speaker_turns_become_speaker_segments_in_seconds():
    response = connector().parse_details(json.loads(json.dumps(DIARIZED)), "vibevoice-asr")
    assert [(s.speaker, s.text, s.start_time, s.end_time) for s in response.segments] == [
        ("SPEAKER_01", "Hello there.", 0.0, 2.0),
        ("SPEAKER_00", "Hi, how are you?", 2.0, 5.0),
        ("SPEAKER_01", "Fine.", 5.0, 6.0),
    ]
    assert response.speakers == ["SPEAKER_01", "SPEAKER_00"]
    assert response.has_diarization()
    assert response.language == "en"
    stored = json.loads(response.to_storage_format())
    assert stored[1] == {"speaker": "SPEAKER_00", "sentence": "Hi, how are you?",
                         "start_time": 2.0, "end_time": 5.0}


def test_turns_without_text_take_the_words_inside_them():
    data = {
        "text": "Hello there. Hi, you?",
        "speaker_turns": [
            {"start_sample": 0, "end_sample": 32000, "speaker_id": "speaker_0"},
            {"start_sample": 32000, "end_sample": 80000, "speaker_id": "speaker_1"},
        ],
        "words": WORDS, "sample_rate": RATE,
    }
    response = connector().parse_details(data, "m")
    assert [(s.speaker, s.text) for s in response.segments] == [
        ("SPEAKER_00", "Hello there."), ("SPEAKER_01", "Hi, you?")]
    assert response.segments[1].end_time == 5.0


def test_turns_without_any_text_give_a_plain_transcript():
    """A diarization-only model (Sortformer) returns spans and no words."""
    data = {"text": "", "speaker_turns": [
        {"start_sample": 0, "end_sample": 32000, "speaker_id": "speaker_0"}],
        "sample_rate": RATE}
    response = connector().parse_details(data, "sortformer")
    assert response.segments is None
    assert not response.has_diarization()
    assert response.text == ""


def test_segments_only_give_timed_segments_without_speakers():
    data = {"text": "One. Two.", "segments": [
        {"start_sample": 0, "end_sample": 8000, "confidence": 0.8, "text": "One."},
        {"start_sample": 8000, "end_sample": 24000, "text": " Two. "}],
        "sample_rate": RATE}
    response = connector().parse_details(data, "parakeet-tdt")
    assert [(s.speaker, s.text, s.start_time, s.end_time) for s in response.segments] == [
        (None, "One.", 0.0, 0.5), (None, "Two.", 0.5, 1.5)]
    # Without speakers Speakr stores the plain text (processing checks
    # has_diarization before it stores segments).
    assert not response.has_diarization()
    assert response.text == "One. Two."


def test_words_only_give_the_text():
    data = {"text": "", "words": WORDS, "sample_rate": RATE}
    response = connector().parse_details(data, "parakeet-tdt")
    assert response.segments is None
    assert response.text == "Hello there. Hi, you?"


def test_text_only_is_a_plain_transcript():
    response = connector().parse_details(
        {"text": "Just text.", "timing": {"wall_ms": 1.0}}, "qwen3-asr")
    assert response.text == "Just text."
    assert response.segments is None
    assert response.language is None


@pytest.mark.parametrize("rate", [None, 0, "16000", True])
def test_offsets_without_a_usable_sample_rate_are_refused(rate):
    data = {"text": "x", "segments": [{"start_sample": 0, "end_sample": 10, "text": "x"}]}
    if rate is not None:
        data["sample_rate"] = rate
    with pytest.raises(AudioCppResponseError, match="sample_rate"):
        connector().parse_details(data, "m")


# ------------------------------------------------------------------ requests


class FakeClient:
    """Stands in for httpx.Client and records the request."""

    calls = []
    answer = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, files=None, timeout=None, **kwargs):
        FakeClient.calls.append({"url": url, "files": files, "kwargs": kwargs})
        return FakeClient.answer(url)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return FakeClient


def json_answer(status, payload=None, text=None):
    def answer(url):
        request = httpx.Request("POST", url)
        if payload is not None:
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(status, text=text or "", request=request)
    return answer


def request_for(data, filename="meeting.wav", language=None, model=None):
    return TranscriptionRequest(audio_file=io.BytesIO(data), filename=filename,
                                language=language, model=model)


def test_transcribe_posts_wav_model_and_language_to_the_details_route(fake_client):
    fake_client.answer = json_answer(200, DIARIZED)
    audio = wav_bytes()
    response = connector().transcribe(request_for(audio, language="en"))
    call = fake_client.calls[0]
    assert call["url"] == "http://audiocpp.test:8080/v1/audio/transcriptions/details"
    assert call["files"]["model"] == (None, "vibevoice-asr")
    assert call["files"]["language"] == (None, "en")
    name, body, mime = call["files"]["file"]
    assert (name, mime) == ("meeting.wav", "audio/wav")
    assert body == audio
    assert "Authorization" not in str(call["kwargs"])
    assert response.has_diarization()


def test_a_profile_model_overrides_the_configured_one(fake_client):
    fake_client.answer = json_answer(200, {"text": "ok", "timing": {}})
    connector().transcribe(request_for(wav_bytes(), model="parakeet-tdt"))
    assert fake_client.calls[0]["files"]["model"] == (None, "parakeet-tdt")
    assert "language" not in fake_client.calls[0]["files"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_other_audio_is_converted_to_mono_16k_wav(fake_client):
    fake_client.answer = json_answer(200, {"text": "ok", "timing": {}})
    connector().transcribe(request_for(wav_bytes(rate=44100, channels=2), filename="talk.m4a"))
    name, body, _ = fake_client.calls[0]["files"]["file"]
    assert name == "talk.wav"
    with wave.open(io.BytesIO(body), "rb") as handle:
        assert (handle.getnchannels(), handle.getframerate(), handle.getsampwidth()) == (1, RATE, 2)


def test_http_errors_raise_provider_error_with_the_status(fake_client):
    fake_client.answer = json_answer(400, {"error": {"message": "unknown model 'x'"}})
    with pytest.raises(ProviderError) as raised:
        connector().transcribe(request_for(wav_bytes()))
    assert raised.value.status_code == 400
    assert "status 400" in str(raised.value) and "unknown model" in str(raised.value)


def test_a_body_that_is_not_json_is_an_error(fake_client):
    fake_client.answer = json_answer(200, text="<html>proxy</html>")
    with pytest.raises(AudioCppResponseError):
        connector().transcribe(request_for(wav_bytes()))


def test_a_timeout_is_a_transcription_error(fake_client):
    def answer(url):
        raise httpx.ReadTimeout("slow", request=httpx.Request("POST", url))
    fake_client.answer = answer
    with pytest.raises(TranscriptionError, match="timed out"):
        connector(timeout=5).transcribe(request_for(wav_bytes()))


# -------------------------------------------------------------------- probe


def probe_with(monkeypatch, routes):
    seen = []

    def fake_get(url, **kwargs):
        seen.append((url, kwargs.get("timeout")))
        status, payload = routes.get(url, (404, {"error": "not found"}))
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return connector().probe(), seen


def test_probe_lists_the_server_models(monkeypatch):
    result, seen = probe_with(monkeypatch, {
        "http://audiocpp.test:8080/v1/models": (200, {"object": "list", "data": [
            {"id": "vibevoice-asr", "object": "model", "configured": True},
            {"id": "parakeet-tdt", "object": "model", "configured": True}]}),
    })
    assert result.ok
    assert [model["id"] for model in result.models] == ["vibevoice-asr", "parakeet-tdt"]
    assert seen == [("http://audiocpp.test:8080/v1/models", 10.0)]


def test_probe_falls_back_to_health(monkeypatch):
    result, seen = probe_with(monkeypatch, {
        "http://audiocpp.test:8080/health": (200, {"status": "ok", "models": 1}),
    })
    assert result.ok and result.models == []
    assert [url for url, _ in seen][-1] == "http://audiocpp.test:8080/health"


def test_probe_reports_a_failed_server(monkeypatch):
    result, _ = probe_with(monkeypatch, {
        "http://audiocpp.test:8080/health": (503, {"status": "loading"}),
    })
    assert not result.ok and result.status == 503
