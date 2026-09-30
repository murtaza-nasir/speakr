"""Every request names the model Speakr is configured to use (#409).

whisperx-asr-service falls back to its own default model when a request names
none, and that default is empty when PRELOAD_MODEL is empty. Speakr's startup
voice embedding check sent no model at all, incognito mode ignored the admin
default, and bulk reprocessing skipped the model, hotword and speaker hints
entirely. These tests pin that every path resolves the model the same way an
ordinary upload does, and that an empty or whitespace name is never sent.

SHARED-DB: every test restores the admin default setting and removes its rows.
"""

import hashlib
import io
import json
import logging
import os
import sys
import uuid
import wave
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, SystemSetting, Tag, User
from src.services import voice_embedding_check as vec
from src.services.transcription.base import TranscriptionRequest

app.config["WTF_CSRF_ENABLED"] = False
DEFAULT_KEY = 'transcription_default_model'
REAL_HTTPX_CLIENT = httpx.Client


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def ctx():
    with app.app_context():
        saved = SystemSetting.get_setting(DEFAULT_KEY, None)
        yield
        db.session.rollback()
        SystemSetting.set_setting(DEFAULT_KEY, saved or '', setting_type='string')


def _set_default(value):
    SystemSetting.set_setting(DEFAULT_KEY, value, setting_type='string')


@contextmanager
def _no_allowlist():
    with patch("src.config.app_config.TRANSCRIPTION_MODELS_AVAILABLE", []), \
         patch.object(SystemSetting, "get_setting", wraps=SystemSetting.get_setting) as get:
        original = get.side_effect
        yield


def _user():
    s = uuid.uuid4().hex[:8]
    u = User(username=f"md_{s}", email=f"md_{s}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


# ------------------------------------------------------------ the resolver

def test_the_stored_admin_default_is_stripped(ctx):
    from src.services.transcription_defaults import resolve_transcription_model
    _set_default('  large-v3 \t')
    assert resolve_transcription_model(None) == 'large-v3'
    _set_default('   ')
    assert resolve_transcription_model(None) is None


# ------------------------------------------------------------ asr_endpoint

def _asr_connector():
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
    return ASREndpointConnector({'base_url': 'http://asr.test:9000'})


def _send(connector, model, status=200, body=None):
    """Transcribe through the connector against a fake service; return the
    query parameters it received."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        if status != 200:
            return httpx.Response(status, json=body or {"detail": "boom"})
        return httpx.Response(200, json={"text": "hello", "language": "en", "segments": []})

    transport = httpx.MockTransport(handler)
    with patch("src.services.transcription.connectors.asr_endpoint.httpx.Client",
               lambda *a, **k: REAL_HTTPX_CLIENT(transport=transport)):
        try:
            connector.transcribe(TranscriptionRequest(
                audio_file=io.BytesIO(b"RIFF"), filename="clip.wav", mime_type="audio/wav", model=model))
        except Exception:
            pass
    return seen


@pytest.mark.parametrize("model", [None, "", "   ", "\t\n"])
def test_asr_endpoint_never_sends_an_empty_or_whitespace_model(ctx, model):
    assert 'model' not in _send(_asr_connector(), model)


def test_asr_endpoint_sends_a_real_model_stripped(ctx):
    assert _send(_asr_connector(), "  distil-large-v3.5 ")['model'] == 'distil-large-v3.5'


def test_an_asr_failure_names_the_file_it_was_for(ctx, caplog):
    with caplog.at_level(logging.ERROR):
        _send(_asr_connector(), None, status=500, body={"detail": "Invalid model size ''"})
    assert any("'clip.wav'" in r.getMessage() and "Invalid model size" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------ the canary

class _EmbeddingResponse:
    speaker_embeddings = {'SPEAKER_00': [0.1] * 256}


def _probe_capturing():
    seen = {}

    class _Conn:
        def transcribe(self, request):
            seen['model'] = request.model
            seen['filename'] = request.filename
            return _EmbeddingResponse()

    with patch('src.services.transcription.registry.get_registry') as reg:
        reg.return_value.get_active_connector.return_value = _Conn()
        vec.probe_backend()
    return seen


def test_the_canary_sends_the_admin_default_model(ctx):
    _set_default('large-v3')
    assert _probe_capturing()['model'] == 'large-v3'


def test_the_canary_sends_no_model_when_none_is_configured(ctx):
    _set_default('')
    assert _probe_capturing()['model'] is None


def _fingerprint_with(connector_model):
    conn = MagicMock()
    conn.base_url = 'http://asr.test:9000'
    conn.model = connector_model
    with patch('src.services.transcription.registry.get_registry') as reg:
        reg.return_value.get_active_connector.return_value = conn
        reg.return_value.get_active_connector_name.return_value = 'asr_endpoint'
        return vec.backend_fingerprint()


def test_the_fingerprint_is_unchanged_without_an_admin_default(ctx):
    """Existing installations keep their stored fingerprint, so upgrading
    does not re-run the probe for nothing."""
    _set_default('')
    before = hashlib.sha256('asr_endpoint|http://asr.test:9000|'.encode()).hexdigest()[:16]
    assert _fingerprint_with('') == before


def test_choosing_an_admin_default_changes_the_fingerprint(ctx):
    _set_default('')
    without = _fingerprint_with('')
    _set_default('large-v3')
    assert _fingerprint_with('') != without


def test_a_failed_canary_says_where_it_came_from_and_what_to_do(ctx, caplog):
    message = vec.canary_failure_message(vec.CanaryUnavailable(
        "transcription of the canary failed: ASR request failed with status 500: "
        "{\"detail\":\"Invalid model size '', expected one of: tiny\"}"))
    assert 'voice embedding check' in message and vec.CANARY_FILENAME in message
    assert 'no recording was affected' in message
    assert 'PRELOAD_MODEL' in message and 'default transcription model' in message


def test_an_unrelated_canary_failure_gets_no_model_hint(ctx):
    message = vec.canary_failure_message(vec.CanaryUnavailable('connection refused'))
    assert 'PRELOAD_MODEL' not in message and 'no recording was affected' in message


def test_the_check_logs_the_explanatory_warning(ctx, caplog):
    with patch.object(vec, 'embeddings_supported', return_value=True), \
         patch.object(vec, 'probe_backend', side_effect=vec.CanaryUnavailable("Invalid model size ''")), \
         caplog.at_level(logging.WARNING):
        vec.check_voice_embeddings(app, force=True)
    assert any('no recording was affected' in r.getMessage() and 'PRELOAD_MODEL' in r.getMessage()
               for r in caplog.records)


# ------------------------------------------------------------ incognito

def _wav(tmp_path):
    path = os.path.join(str(tmp_path), 'incognito.wav')
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 1600)
    return path


def _passthrough(filepath, **kwargs):
    r = MagicMock()
    r.output_path = filepath
    r.was_converted = r.was_compressed = False
    return r


@pytest.mark.parametrize("chunked", [False, True])
def test_incognito_uses_the_admin_default_model(ctx, tmp_path, chunked):
    from src.tasks import processing
    _set_default('large-v3')
    seen = {}

    class _Resp:
        segments = []
        text = 'hello'
        def has_diarization(self):
            return False

    class _Conn:
        specifications = MagicMock()
        supports_diarization = False
        def transcribe(self, request):
            seen['model'] = request.model
            return _Resp()

    def _chunks(*args, **kwargs):
        seen['model'] = kwargs.get('transcription_model')
        return _Resp()

    chunker = MagicMock()
    chunker.needs_chunking.return_value = chunked
    chunker.get_audio_duration.return_value = 1
    with patch('src.services.transcription.get_registry') as reg, \
         patch.object(processing, 'convert_if_needed', side_effect=_passthrough), \
         patch.object(processing, 'is_video_file', return_value=False), \
         patch.object(processing, 'chunking_service', chunker), \
         patch.object(processing, 'transcribe_chunks_with_connector', side_effect=_chunks):
        reg.return_value.get_active_connector.return_value = _Conn()
        processing.transcribe_incognito(_wav(tmp_path), 'incognito.wav', language='en')
    assert seen.get('model') == 'large-v3'


# ------------------------------------------------------------ bulk reprocess

def test_bulk_reprocess_transcription_uses_the_resolved_params(ctx):
    user = _user()
    tag = Tag(name=f"t_{uuid.uuid4().hex[:6]}", user_id=user.id, default_transcription_model='medium',
              default_hotwords='Speakr, WhisperX', default_min_speakers=2, default_max_speakers=4)
    db.session.add(tag)
    db.session.commit()
    rec = Recording(user_id=user.id, title='r', status='COMPLETED', audio_path='local://x.mp3',
                    original_filename='x.mp3', transcription='[]')
    db.session.add(rec)
    db.session.commit()
    from src.models import RecordingTag
    db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=1))
    db.session.commit()
    try:
        storage = MagicMock()
        storage.exists.return_value = True
        with patch('src.api.recordings.get_storage_service', return_value=storage), \
             patch('src.api.recordings.job_queue.enqueue', return_value=1) as enqueue:
            r = _client(user).post('/api/recordings/bulk-reprocess',
                                   json={'recording_ids': [rec.id], 'type': 'transcription'})
        assert r.status_code == 200, r.get_data(as_text=True)
        params = enqueue.call_args.kwargs['params']
        assert params['user_id'] == user.id
        assert params['transcription_model'] == 'medium'
        assert params['hotwords'] == 'Speakr, WhisperX'
        assert params['min_speakers'] == 2 and params['max_speakers'] == 4
    finally:
        RecordingTag.query.filter_by(recording_id=rec.id).delete()
        db.session.delete(db.session.get(Recording, rec.id))
        db.session.delete(db.session.get(Tag, tag.id))
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


# ------------------------------------------------------------ API v1

def test_api_capabilities_report_the_default_the_resolver_uses(ctx):
    from src.services.transcription_defaults import resolve_transcription_model
    user = _user()
    try:
        _set_default('  large-v3 ')
        body = _client(user).get('/api/v1/transcription').get_json()
        assert body['default_model'] == resolve_transcription_model(None) == 'large-v3'
    finally:
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


def test_api_capabilities_list_env_models_without_an_admin_list(ctx):
    """The endpoint imported a name that does not exist and returned 500
    whenever the admin had not saved a model list."""
    user = _user()
    saved = SystemSetting.get_setting('transcription_models_visible_json', None)
    try:
        SystemSetting.set_setting('transcription_models_visible_json', '', setting_type='string')
        options = [{'value': 'large-v3', 'label': 'Large v3'}, {'value': 'medium', 'label': 'medium'}]
        with patch('src.config.app_config.TRANSCRIPTION_MODEL_OPTIONS', options):
            r = _client(user).get('/api/v1/transcription')
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()['models'] == options
    finally:
        SystemSetting.set_setting('transcription_models_visible_json', saved or '', setting_type='string')
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()
