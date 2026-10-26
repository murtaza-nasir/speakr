"""
Tests for the Apify audio-transcriber connector.

httpx is mocked so no network is touched. Covers registration, config
validation, the stage -> run-sync -> cleanup flow, response parsing and errors.
"""

import io
import pytest

import src.services.transcription.connectors.apify_audio_transcriber as mod
from src.services.transcription.connectors.apify_audio_transcriber import ApifyAudioTranscriberConnector
from src.services.transcription.base import TranscriptionRequest, TranscriptionCapability
from src.services.transcription.exceptions import TranscriptionError, ConfigurationError


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, run_resp=None, upload_status=200):
        self.run_resp = run_resp
        self.upload_status = upload_status
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, path, **kw):
        self.calls.append(('POST', path, kw))
        if path == '/v2/key-value-stores':
            return _Resp(201, {'data': {'id': 'store1'}})
        return self.run_resp

    def put(self, path, **kw):
        self.calls.append(('PUT', path, kw))
        if path.endswith('/records/audio'):
            return _Resp(self.upload_status)
        return _Resp(200)

    def delete(self, path, **kw):
        self.calls.append(('DELETE', path, kw))
        return _Resp(204)


def _patch(monkeypatch, client):
    monkeypatch.setattr(mod.httpx, 'Client', lambda *a, **k: client)


def _conn(**cfg):
    base = {'api_key': 'tok'}
    base.update(cfg)
    return ApifyAudioTranscriberConnector(base)


def _req(**kw):
    kw.setdefault('audio_file', io.BytesIO(b'audio-bytes'))
    kw.setdefault('filename', 'a.mp3')
    kw.setdefault('mime_type', 'audio/mpeg')
    return TranscriptionRequest(**kw)


OK_ITEM = {
    'status': 'ok', 'language': 'auto-detected', 'durationSeconds': 12.5,
    'text': 'hello world',
    'segments': [{'start': 0.0, 'end': 1.5, 'text': ' hello '}, {'start': 1.5, 'end': 3.0, 'text': 'world'}],
}


def test_registered_in_registry():
    from src.services.transcription.registry import get_registry
    names = [c['name'] for c in get_registry().list_connectors()]
    assert 'apify_audio_transcriber' in names


def test_capabilities():
    c = _conn()
    assert c.supports(TranscriptionCapability.TIMESTAMPS)
    assert not c.supports(TranscriptionCapability.DIARIZATION)
    assert c.specifications.handles_chunking_internally is True


def test_missing_token_raises():
    with pytest.raises(ConfigurationError):
        ApifyAudioTranscriberConnector({'api_key': ''})


def test_model_slash_normalised():
    assert _conn(model='pennydata/audio-transcriber').model == 'pennydata~audio-transcriber'


def test_transcribe_flow_and_cleanup(monkeypatch):
    client = _FakeClient(run_resp=_Resp(201, [OK_ITEM]))
    _patch(monkeypatch, client)
    resp = _conn().transcribe(_req(language='en'))
    assert resp.text == 'hello world'
    assert resp.language is None  # 'auto-detected' placeholder is not a language code
    assert resp.duration == 12.5
    assert [s.text for s in resp.segments] == ['hello', 'world']
    assert resp.segments[1].start_time == 1.5 and resp.segments[1].end_time == 3.0
    run = [c for c in client.calls if c[0] == 'POST' and 'run-sync' in c[1]][0]
    assert run[1] == '/v2/acts/pennydata~audio-transcriber/run-sync-get-dataset-items'
    assert run[2]['headers']['Authorization'] == 'Bearer tok'
    body = run[2]['json']
    assert body['language'] == 'en'
    assert body['urls'] == ['https://api.apify.com/v2/key-value-stores/store1/records/audio']
    assert 'tok' not in str(body)  # the user's token never goes into the Actor input
    assert client.calls[-1][:2] == ('DELETE', '/v2/key-value-stores/store1')


def test_auto_language_not_sent(monkeypatch):
    client = _FakeClient(run_resp=_Resp(201, [OK_ITEM]))
    _patch(monkeypatch, client)
    _conn().transcribe(_req(language='auto'))
    run = [c for c in client.calls if 'run-sync' in c[1]][0]
    assert 'language' not in run[2]['json']


def test_actor_error_item_raises_and_cleans_up(monkeypatch):
    client = _FakeClient(run_resp=_Resp(201, [{'status': 'error', 'error': 'unsupported codec'}]))
    _patch(monkeypatch, client)
    with pytest.raises(TranscriptionError, match='unsupported codec'):
        _conn().transcribe(_req())
    assert client.calls[-1][0] == 'DELETE'


def test_http_error_raises(monkeypatch):
    client = _FakeClient(run_resp=_Resp(402, None, text='not enough credits'))
    _patch(monkeypatch, client)
    with pytest.raises(TranscriptionError, match='402'):
        _conn().transcribe(_req())


def test_run_sync_408_has_clear_message(monkeypatch):
    client = _FakeClient(run_resp=_Resp(408, None, text='timeout'))
    _patch(monkeypatch, client)
    with pytest.raises(TranscriptionError, match='synchronous window'):
        _conn().transcribe(_req())


def test_upload_failure_cleans_up(monkeypatch):
    client = _FakeClient(upload_status=500)
    _patch(monkeypatch, client)
    with pytest.raises(TranscriptionError, match='upload failed'):
        _conn().transcribe(_req())
    assert client.calls[-1][:2] == ('DELETE', '/v2/key-value-stores/store1')


def test_empty_dataset_raises(monkeypatch):
    client = _FakeClient(run_resp=_Resp(201, []))
    _patch(monkeypatch, client)
    with pytest.raises(TranscriptionError, match='no dataset items'):
        _conn().transcribe(_req())
