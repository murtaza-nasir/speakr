"""Which connector a TRANSCRIPTION_MODEL selects, and how gpt-transcribe
keywords are cleaned (follow-ups to #647)."""
import io
import logging
from unittest.mock import MagicMock

import pytest

from tests.test_connector_architecture import reset_registry


@pytest.fixture
def registry_env(monkeypatch):
    # monkeypatch restores every variable afterwards; clear_env() alone would
    # leave the suite without the settings later tests in this worker need
    import os
    for key in [k for k in os.environ if k.startswith(("TRANSCRIPTION_", "ASR_", "WHISPER_", "AZURE_"))]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("USE_ASR_ENDPOINT", raising=False)
    reset_registry()
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "test-key")
    yield
    reset_registry()


def _active(monkeypatch, model):
    from src.services.transcription.registry import get_registry
    monkeypatch.setenv("TRANSCRIPTION_MODEL", model)
    registry = get_registry()
    registry.initialize_from_env()
    return registry.get_active_connector_name()


@pytest.mark.parametrize("model", ["gpt-transcribe", "GPT-Transcribe", " gpt-transcribe ",
                                   "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe-diarize"])
def test_a_known_model_selects_openai_transcribe(registry_env, monkeypatch, model):
    assert _active(monkeypatch, model) == "openai_transcribe"


@pytest.mark.parametrize("model", ["gpt-transcribe-2026-06-01", "openai/gpt-transcribe",
                                   "my-gpt-transcribe-proxy", "gpt-4o-audio-preview"])
def test_another_name_starts_with_the_whisper_connector(registry_env, monkeypatch, caplog, model):
    """Such a name used to select OpenAI Transcribe, which then refused to start."""
    with caplog.at_level(logging.WARNING):
        assert _active(monkeypatch, model) == "openai_whisper"
    assert "is not one of the OpenAI Transcribe models" in caplog.text


def test_a_whisper_model_logs_no_warning(registry_env, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        assert _active(monkeypatch, "whisper-1") == "openai_whisper"
    assert "OpenAI Transcribe models" not in caplog.text


def _sent(hotwords, caplog):
    from src.services.transcription.base import TranscriptionRequest
    from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector
    connector = OpenAITranscribeConnector({"api_key": "test", "model": "gpt-transcribe"})
    connector.client = MagicMock()
    connector.client.audio.transcriptions.create.return_value = MagicMock(spec=["text"], text="hi")
    with caplog.at_level(logging.WARNING):
        connector.transcribe(TranscriptionRequest(audio_file=io.BytesIO(b"a"), filename="a.mp3",
                                                  hotwords=hotwords))
    return connector.client.audio.transcriptions.create.call_args.kwargs


def test_a_rejected_keyword_is_left_out_and_logged(caplog):
    kwargs = _sent("Line1\nLine2, ok, x<y", caplog)
    assert kwargs["keywords"] == ["ok"]
    assert "Left out 2 hotword(s)" in caplog.text


def test_clean_keywords_log_nothing(caplog):
    kwargs = _sent("Acme, Zed, Acme", caplog)
    assert kwargs["keywords"] == ["Acme", "Zed"]
    assert "Left out" not in caplog.text


def test_the_model_name_is_stored_in_lower_case():
    from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector
    assert OpenAITranscribeConnector({"api_key": "t", "model": "GPT-Transcribe"}).model == "gpt-transcribe"
