"""
Coverage for the AssemblyAI transcription connector.

AssemblyAI uses an async upload -> submit -> poll REST flow with diarization
output in an `utterances` array (times in milliseconds). These tests mock
httpx so no network is touched, and verify: registration, capabilities, config
validation, request-payload shaping (diarization, speaker hints, hotwords,
language), the poll loop (processing -> completed and -> error), and the
milliseconds-to-seconds utterance parsing.
"""

import io
import json
import pytest
from unittest.mock import patch

import src.services.transcription.connectors.assemblyai as aai_mod
from src.services.transcription.connectors.assemblyai import AssemblyAITranscriptionConnector
from src.services.transcription.base import (
    TranscriptionRequest, TranscriptionCapability,
)
from src.services.transcription.exceptions import TranscriptionError, ConfigurationError


# ---------------------------------------------------------------------------
# Fake httpx client: scripts canned responses for upload/submit/poll by path.
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, poll_statuses=None, final=None, submit_status=200):
        # poll_statuses: list of status strings returned by successive GETs;
        # the final 'completed' GET returns `final`.
        self.poll_statuses = list(poll_statuses or ['completed'])
        self.final = final or {}
        self.submit_status = submit_status
        self.posted = []   # (path, json/content)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, path, content=None, json=None, headers=None):
        self.posted.append((path, json if json is not None else content))
        if path == '/v2/upload':
            return _Resp(200, {'upload_url': 'https://cdn.assemblyai.com/upload/x'})
        if path == '/v2/transcript':
            return _Resp(self.submit_status, {'id': 'tid-1', 'status': 'queued'})
        raise AssertionError(f"unexpected POST {path}")
    def get(self, path):
        status = self.poll_statuses.pop(0)
        if status == 'completed':
            return _Resp(200, dict(self.final, status='completed'))
        if status == 'error':
            return _Resp(200, {'status': 'error', 'error': 'boom'})
        return _Resp(200, {'status': status})


def _conn(**cfg):
    base = {'api_key': 'fake', 'poll_interval': 0}
    base.update(cfg)
    return AssemblyAITranscriptionConnector(base)


def _req(**kw):
    kw.setdefault('audio_file', io.BytesIO(b'audio-bytes'))
    kw.setdefault('filename', 'a.mp3')
    return TranscriptionRequest(**kw)


# --- registration / capabilities / config ---------------------------------

def test_registered_in_registry():
    from src.services.transcription.registry import get_registry
    names = [c['name'] for c in get_registry().list_connectors()]
    assert 'assemblyai' in names


def test_capabilities():
    caps = _conn().get_capabilities()
    assert TranscriptionCapability.DIARIZATION in caps
    assert TranscriptionCapability.HOTWORDS in caps
    assert TranscriptionCapability.SPEAKER_COUNT_CONTROL in caps
    assert TranscriptionCapability.LANGUAGE_DETECTION in caps
    # No free-text initial prompt on AssemblyAI base transcription.
    assert TranscriptionCapability.INITIAL_PROMPT not in caps


def test_handles_chunking_internally():
    assert _conn().specifications.handles_chunking_internally is True


def test_missing_api_key_raises():
    with pytest.raises(ConfigurationError):
        AssemblyAITranscriptionConnector({'api_key': ''})


# --- payload shaping -------------------------------------------------------

def test_payload_diarize_range_hotwords_language():
    p = _conn()._build_payload(
        _req(language='en', diarize=True, min_speakers=2, max_speakers=5,
             hotwords='Speakr, PyAnnote'),
        'u')
    assert p['language_code'] == 'en'
    assert p['speaker_labels'] is True
    assert p['speaker_options'] == {'min_speakers_expected': 2, 'max_speakers_expected': 5}
    assert p['word_boost'] == ['Speakr', 'PyAnnote'] and p['boost_param'] == 'default'
    assert 'language_detection' not in p


def test_payload_equal_min_max_uses_speakers_expected():
    p = _conn()._build_payload(_req(language='auto', diarize=True, min_speakers=3, max_speakers=3), 'u')
    assert p['speakers_expected'] == 3
    assert 'speaker_options' not in p
    assert p['language_detection'] is True  # 'auto' -> detection


def test_payload_no_diarize_no_speaker_fields():
    p = _conn()._build_payload(_req(language='de', diarize=False), 'u')
    assert 'speaker_labels' not in p and 'speaker_options' not in p
    assert p['language_code'] == 'de'


def test_payload_model_passthrough_uses_plural_array():
    # Single model -> one-element speech_models array (plural field).
    p = _conn(model='universal-3-pro')._build_payload(_req(diarize=False, language='en'), 'u')
    assert p['speech_models'] == ['universal-3-pro']
    assert 'speech_model' not in p


def test_payload_model_comma_list_becomes_fallback_array():
    p = _conn(model='universal-3-pro, universal-2')._build_payload(_req(diarize=False, language='en'), 'u')
    assert p['speech_models'] == ['universal-3-pro', 'universal-2']


def test_payload_no_model_omits_speech_models():
    p = _conn()._build_payload(_req(diarize=False, language='en'), 'u')
    assert 'speech_models' not in p and 'speech_model' not in p


def test_payload_foreign_model_dropped():
    # A model name resolved from shared settings for another connector
    # (WhisperX 'large-v3', OpenAI 'gpt-4o-...') must NOT be sent to AssemblyAI.
    for bad in ('large-v3', 'gpt-4o-transcribe-diarize'):
        p = _conn(model=bad)._build_payload(_req(diarize=False, language='en'), 'u')
        assert 'speech_models' not in p, f"{bad} should have been dropped"
    # A per-request override that's foreign is dropped too.
    p = _conn(model='universal-2')._build_payload(_req(diarize=False, language='en', model='large-v3'), 'u')
    assert 'speech_models' not in p


# --- end-to-end flow (mocked) ---------------------------------------------

DIARIZED_FINAL = {
    'text': 'Hello there Hi back',
    'language_code': 'en',
    'audio_duration': 12.5,
    'utterances': [
        {'speaker': 'A', 'text': 'Hello there', 'start': 250, 'end': 2950, 'confidence': 0.93},
        {'speaker': 'B', 'text': 'Hi back', 'start': 3000, 'end': 4100, 'confidence': 0.88},
    ],
}


def test_transcribe_diarized_flow_parses_utterances():
    fake = _FakeClient(poll_statuses=['processing', 'completed'], final=DIARIZED_FINAL)
    c = _conn()
    with patch.object(c, '_client', return_value=fake):
        resp = c.transcribe(_req(language='en', diarize=True))
    assert [(s.speaker, s.text, s.start_time, s.end_time) for s in resp.segments] == [
        ('A', 'Hello there', 0.25, 2.95),
        ('B', 'Hi back', 3.0, 4.1),
    ]
    assert resp.speakers == ['A', 'B']
    assert resp.duration == 12.5
    assert resp.provider == 'assemblyai'
    # storage format round-trips with ms converted to seconds
    stored = json.loads(resp.to_storage_format())
    assert stored[0] == {'speaker': 'A', 'sentence': 'Hello there', 'start_time': 0.25, 'end_time': 2.95}
    # uploaded the audio bytes and submitted with speaker_labels
    paths = [p for p, _ in fake.posted]
    assert paths == ['/v2/upload', '/v2/transcript']
    assert fake.posted[1][1]['speaker_labels'] is True


def test_transcribe_plain_text_when_no_utterances():
    fake = _FakeClient(poll_statuses=['completed'],
                       final={'text': 'just text', 'language_code': 'en', 'audio_duration': 3.0})
    c = _conn()
    with patch.object(c, '_client', return_value=fake):
        resp = c.transcribe(_req(language='en', diarize=False))
    assert resp.segments is None
    assert resp.text == 'just text'
    assert resp.to_storage_format() == 'just text'


def test_transcribe_error_status_raises():
    fake = _FakeClient(poll_statuses=['processing', 'error'])
    c = _conn()
    with patch.object(c, '_client', return_value=fake):
        with pytest.raises(TranscriptionError, match='boom'):
            c.transcribe(_req(diarize=True))
