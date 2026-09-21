"""Contract tests for the OpenASR local-server connector (PR #354)."""

import io
from unittest.mock import patch

import httpx
import pytest

from src.services.transcription.base import TranscriptionCapability, TranscriptionRequest
from src.services.transcription.connectors.openasr import OpenASRTranscriptionConnector
from src.services.transcription.exceptions import ConfigurationError, TranscriptionError


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://127.0.0.1:8080/v1/audio/transcriptions")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(self.text, request=request, response=response)


class _Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _connector(**overrides):
    config = {'base_url': 'http://127.0.0.1:8080', 'model': 'qwen3-asr-0.6b'}
    config.update(overrides)
    return OpenASRTranscriptionConnector(config)


def _request(**overrides):
    kwargs = dict(audio_file=io.BytesIO(b'RIFFxxxx'), filename='a.wav', mime_type='audio/wav')
    kwargs.update(overrides)
    return TranscriptionRequest(**kwargs)


VERBOSE_JSON = {
    'text': 'hello world',
    'language': 'en',
    'segments': [
        {'text': ' hello ', 'start': 0.0, 'end': 1.0, 'speaker': 'SPEAKER_2'},
        {'text': 'world', 'start': 1.0, 'end': 2.0, 'speaker': 'SPEAKER_10'},
    ],
}


def test_missing_base_url_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        OpenASRTranscriptionConnector({})


def test_base_url_v1_normalization():
    assert _connector().base_url == 'http://127.0.0.1:8080/v1'
    assert _connector(base_url='http://h:1/v1').base_url == 'http://h:1/v1'
    assert _connector(base_url='http://h:1/v1/').base_url == 'http://h:1/v1'


def test_declared_prompt_capabilities():
    c = _connector()
    assert TranscriptionCapability.HOTWORDS in c.CAPABILITIES
    assert TranscriptionCapability.INITIAL_PROMPT in c.CAPABILITIES


def test_happy_path_without_diarization():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        resp = _connector().transcribe(_request())
    assert resp.text == 'hello world'
    assert resp.language == 'en'
    assert resp.provider == 'openasr'
    assert len(resp.segments) == 2
    # diarize off => speaker labels are not attached even if the server sends them
    assert all(s.speaker is None for s in resp.segments)
    assert resp.speakers is None
    url, kwargs = client.calls[0]
    assert url == 'http://127.0.0.1:8080/v1/audio/transcriptions'
    assert kwargs['files']['response_format'] == (None, 'verbose_json')
    assert 'diarize' not in kwargs['files']


def test_diarization_collects_and_naturally_sorts_speakers():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        resp = _connector().transcribe(_request(diarize=True))
    # Natural sort: SPEAKER_2 before SPEAKER_10 (lexicographic would invert).
    assert resp.speakers == ['SPEAKER_2', 'SPEAKER_10']
    assert [s.speaker for s in resp.segments] == ['SPEAKER_2', 'SPEAKER_10']
    _, kwargs = client.calls[0]
    assert kwargs['files']['diarize'] == (None, 'true')


def test_prompt_and_hotwords_map_to_openai_prompt_field():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request(prompt='A meeting about ASR.', hotwords='Speakr, WhisperX'))
    _, kwargs = client.calls[0]
    assert kwargs['files']['prompt'] == (None, 'A meeting about ASR.. Speakr, WhisperX')


def test_no_prompt_field_when_neither_given():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request())
    _, kwargs = client.calls[0]
    assert 'prompt' not in kwargs['files']


def test_language_and_model_override_forwarded():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        resp = _connector().transcribe(_request(language='de', model='parakeet-1.1b'))
    _, kwargs = client.calls[0]
    assert kwargs['files']['language'] == (None, 'de')
    assert kwargs['files']['model'] == (None, 'parakeet-1.1b')
    assert resp.model == 'parakeet-1.1b'


def test_api_key_sent_as_bearer_only_when_configured():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector(api_key='sk-x').transcribe(_request())
    _, kwargs = client.calls[0]
    assert kwargs['headers']['Authorization'] == 'Bearer sk-x'

    client2 = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client2):
        _connector().transcribe(_request())
    _, kwargs2 = client2.calls[0]
    assert 'Authorization' not in kwargs2['headers']


def test_http_error_surfaces_status_and_body_excerpt():
    client = _Client(response=_Response(status_code=422, text='unsupported audio codec'))
    with patch('httpx.Client', return_value=client):
        with pytest.raises(TranscriptionError) as exc:
            _connector().transcribe(_request())
    assert '422' in str(exc.value)
    assert 'unsupported audio codec' in str(exc.value)


def test_timeout_is_terminal_transcription_error():
    client = _Client(error=httpx.ReadTimeout('slow'))
    with patch('httpx.Client', return_value=client):
        with pytest.raises(TranscriptionError) as exc:
            _connector(timeout=7).transcribe(_request())
    assert 'timed out after 7' in str(exc.value)


# --- Exact speaker count (#362) ---
# OpenASR takes `speakers=N` (exact), not a min/max range. The app stores a
# single count as min == max; legacy range defaults resolve to the max.

def test_declares_exact_speaker_count_not_range():
    c = _connector()
    assert TranscriptionCapability.EXACT_SPEAKER_COUNT in c.CAPABILITIES
    assert TranscriptionCapability.SPEAKER_COUNT_CONTROL not in c.CAPABILITIES
    assert c.supports_exact_speaker_count is True
    assert c.supports_speaker_count_control is False


def test_exact_count_sent_when_min_equals_max():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request(diarize=True, min_speakers=3, max_speakers=3))
    _, kwargs = client.calls[0]
    assert kwargs['files']['speakers'] == (None, '3')


def test_legacy_range_resolves_to_max():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request(diarize=True, min_speakers=2, max_speakers=5))
    _, kwargs = client.calls[0]
    assert kwargs['files']['speakers'] == (None, '5')


def test_min_only_resolves_to_min():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request(diarize=True, min_speakers=2))
    _, kwargs = client.calls[0]
    assert kwargs['files']['speakers'] == (None, '2')


def test_no_speakers_param_without_counts():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request(diarize=True))
    _, kwargs = client.calls[0]
    assert 'speakers' not in kwargs['files']


def test_no_speakers_param_without_diarize():
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        _connector().transcribe(_request(min_speakers=3, max_speakers=3))
    _, kwargs = client.calls[0]
    assert 'speakers' not in kwargs['files']


# --- Speaker embeddings (#380) ---------------------------------------------
#
# OpenASR (openasr#379) mirrors the WhisperX contract: request with
# return_speaker_embeddings=true, receive a speaker_embeddings map keyed by
# speaker label. Off by default because older servers reject unknown fields.

EMBEDDINGS = {'SPEAKER_2': [0.1] * 192, 'SPEAKER_10': [0.2] * 192}


def test_embeddings_are_not_requested_by_default():
    connector = _connector(diarize=True)
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        response = connector.transcribe(_request(diarize=True))
    _, kwargs = client.calls[0]
    assert 'return_speaker_embeddings' not in kwargs['files']
    assert response.speaker_embeddings is None
    assert TranscriptionCapability.SPEAKER_EMBEDDINGS not in connector.CAPABILITIES


def test_enabling_embeddings_declares_the_capability():
    assert TranscriptionCapability.SPEAKER_EMBEDDINGS in _connector(return_speaker_embeddings=True).CAPABILITIES
    # The class attribute must not be mutated by one enabled instance.
    assert TranscriptionCapability.SPEAKER_EMBEDDINGS not in OpenASRTranscriptionConnector.CAPABILITIES


def test_embeddings_requested_and_parsed_when_enabled_with_diarization():
    connector = _connector(diarize=True, return_speaker_embeddings=True)
    payload = dict(VERBOSE_JSON, speaker_embeddings=EMBEDDINGS)
    client = _Client(response=_Response(payload=payload))
    with patch('httpx.Client', return_value=client):
        response = connector.transcribe(_request(diarize=True))
    _, kwargs = client.calls[0]
    assert kwargs['files']['return_speaker_embeddings'] == (None, 'true')
    assert response.speaker_embeddings == EMBEDDINGS
    assert len(response.speaker_embeddings['SPEAKER_2']) == 192
    # The rest of the response is unaffected.
    assert response.speakers == ['SPEAKER_2', 'SPEAKER_10']


def test_embeddings_not_requested_without_diarization():
    connector = _connector(diarize=False, return_speaker_embeddings=True)
    payload = dict(VERBOSE_JSON, speaker_embeddings=EMBEDDINGS)
    client = _Client(response=_Response(payload=payload))
    with patch('httpx.Client', return_value=client):
        response = connector.transcribe(_request(diarize=False))
    _, kwargs = client.calls[0]
    assert 'return_speaker_embeddings' not in kwargs['files']
    # A map without speaker labels to key on is dropped rather than stored.
    assert response.speaker_embeddings is None


def test_server_without_embedding_support_still_transcribes():
    connector = _connector(diarize=True, return_speaker_embeddings=True)
    client = _Client(response=_Response(payload=VERBOSE_JSON))
    with patch('httpx.Client', return_value=client):
        response = connector.transcribe(_request(diarize=True))
    assert response.speaker_embeddings is None
    assert response.text == 'hello world'


def test_empty_embedding_map_is_stored_as_none():
    connector = _connector(diarize=True, return_speaker_embeddings=True)
    client = _Client(response=_Response(payload=dict(VERBOSE_JSON, speaker_embeddings={})))
    with patch('httpx.Client', return_value=client):
        response = connector.transcribe(_request(diarize=True))
    assert response.speaker_embeddings is None


def test_registry_reads_the_shared_env_flag(monkeypatch):
    from src.services.transcription.registry import ConnectorRegistry
    registry = ConnectorRegistry()
    monkeypatch.setenv('TRANSCRIPTION_BASE_URL', 'http://127.0.0.1:8080')
    monkeypatch.setenv('ASR_RETURN_SPEAKER_EMBEDDINGS', 'true')
    assert registry._build_config_from_env('openasr')['return_speaker_embeddings'] is True
    monkeypatch.setenv('ASR_RETURN_SPEAKER_EMBEDDINGS', 'false')
    assert registry._build_config_from_env('openasr')['return_speaker_embeddings'] is False
    monkeypatch.delenv('ASR_RETURN_SPEAKER_EMBEDDINGS')
    assert registry._build_config_from_env('openasr')['return_speaker_embeddings'] is False
