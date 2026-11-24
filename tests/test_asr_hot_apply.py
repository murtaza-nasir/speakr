"""A default change applies to the next transcription without a restart, and
resolution keeps the documented fallback order.

SHARED-DB: each test removes its own users, recordings and ASR settings.
"""

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.app import app, db
from src.models import Recording, SystemSetting, TranscriptionUsage, User
from src.services import asr_config as ac
from src.services.transcription_defaults import resolve_transcription_model

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

ENV_NAMES = (
    "TRANSCRIPTION_CONNECTOR", "TRANSCRIPTION_BASE_URL", "TRANSCRIPTION_API_KEY",
    "TRANSCRIPTION_MODEL", "ASR_BASE_URL", "ASR_API_KEY", "ASR_DIARIZE",
    "USE_ASR_ENDPOINT", "WHISPER_MODEL",
)
LEGACY_KEYS = ("transcription_models_visible_json", "transcription_default_model")


def _clear_settings():
    SystemSetting.query.filter(
        SystemSetting.key.in_((ac.SETTING_KEY,) + LEGACY_KEYS)).delete(
            synchronize_session=False)
    db.session.commit()


@pytest.fixture
def ctx():
    with app.app_context():
        _clear_settings()
        yield
        _clear_settings()


@pytest.fixture
def clean_env(ctx, monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSCRIPTION_CONNECTOR", "openai_whisper")
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "env-key")
    monkeypatch.setenv("TRANSCRIPTION_BASE_URL", "http://env.lan/v1")
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "whisper-1")
    from src.services.transcription.registry import get_registry
    registry = get_registry()
    registry._active_connector = None
    registry._connector_name = ""
    return monkeypatch


def _user():
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"ha_{suffix}", email=f"ha_{suffix}@local.test",
                password="x")
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user):
    recording = Recording(user_id=user.id, original_filename="ha.mp3",
                          audio_path="local://ha.mp3", status="PENDING",
                          title="hot")
    db.session.add(recording)
    db.session.commit()
    return recording


def _audio(recording):
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"ha_{recording.id}.mp3")
    with open(path, "wb") as handle:
        handle.write(b"\x00" * 1024)
    return path


def _cleanup(user):
    """Remove the rows the run created, in foreign-key order.

    A bulk user delete bypasses the ORM cascade, so the usage rows go first.
    """
    TranscriptionUsage.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Recording.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    User.query.filter_by(id=user.id).delete(synchronize_session=False)
    db.session.commit()


def _connector(name="fake"):
    connector = MagicMock()
    connector.PROVIDER_NAME = name
    connector.supports_diarization = False
    connector.default_diarize = False
    specs = MagicMock()
    specs.max_duration_seconds = 1800
    specs.handles_chunking_internally = False
    specs.recommended_chunk_seconds = 600
    specs.unsupported_codecs = []
    connector.specifications = specs
    response = MagicMock()
    response.text = "A transcript body."
    response.segments = None
    response.speakers = None
    response.speaker_embeddings = None
    response.has_diarization.return_value = False
    connector.transcribe.return_value = response
    return connector


def _profile(label, model):
    return {
        "label": label,
        "connector": "openai_whisper",
        "config": {"api_key": f"{model}-key", "base_url": "http://profile.lan/v1",
                   "model": model},
        "visible": True,
    }


def _run(recording, **kwargs):
    from src.tasks import processing as proc

    conv = MagicMock()
    conv.was_converted = False
    chunker = MagicMock()
    chunker.needs_chunking.return_value = False
    chunker.get_audio_duration.return_value = 60.0
    with patch.object(proc, "is_video_file", return_value=False), \
         patch.object(proc, "convert_if_needed", return_value=conv), \
         patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
         patch.object(proc, "generate_title_task"), \
         patch.object(proc, "generate_summary_only_task"), \
         patch.object(proc, "chunking_service", chunker):
        return proc.transcribe_with_connector(
            app.app_context(), recording.id, _audio(recording), "ha.mp3",
            0.0, mime_type="audio/mpeg", **kwargs)


def _response():
    response = MagicMock()
    response.text = "A transcript body."
    response.segments = None
    response.speakers = None
    response.speaker_embeddings = None
    response.has_diarization.return_value = False
    return response


def _factory(used):
    def create(connector_name, config):
        connector = _connector(connector_name)
        connector.model = (config or {}).get('model')
        connector.transcribe.side_effect = lambda request: (
            used.append(request.model), _response())[1]
        return connector
    return create


def test_default_switch_applies_to_next_transcription(ctx, clean_env):
    user = _user()
    first = ac.create_profile(_profile("Small", "small")).model
    second = ac.create_profile(_profile("Medium", "medium")).model
    ac.set_default(first['id'])

    used = []
    env_connector = _connector('env')
    try:
        with patch('src.services.asr_config._create_connector',
                   side_effect=_factory(used)), \
             patch('src.services.voice_embedding_check.check_voice_embeddings'), \
             patch('src.services.transcription.get_connector',
                   return_value=env_connector):
            run_one = _recording(user)
            _run(run_one, transcription_model=None)
            ac.set_default(second['id'])
            run_two = _recording(user)
            _run(run_two, transcription_model=None)
        assert used == ['small', 'medium']
        env_connector.transcribe.assert_not_called()
    finally:
        _cleanup(user)


def test_environment_only_regression(ctx, clean_env):
    user = _user()
    env_connector = _connector('env')
    registry = MagicMock()
    registry.get_active_connector_name.return_value = 'openai_whisper'
    registry._build_config_from_env.return_value = {
        'api_key': 'env-key', 'base_url': 'http://env.lan/v1', 'model': 'whisper-1'}
    registry.create_connector.return_value = env_connector
    try:
        with patch('src.services.transcription.registry.get_registry',
                   return_value=registry), \
             patch('src.services.transcription.get_connector',
                   return_value=env_connector):
            recording = _recording(user)
            _run(recording, transcription_model=None)
        env_connector.transcribe.assert_called_once()
        request = env_connector.transcribe.call_args.args[0]
        assert request.model == 'whisper-1'
    finally:
        _cleanup(user)


def test_legacy_value_uses_environment_credentials(ctx, clean_env):
    connector, model = ac.resolve_asr_connector('large-v3')
    assert model == 'large-v3'
    assert connector.PROVIDER_NAME == 'openai_whisper'
    assert connector.config['base_url'] == 'http://env.lan/v1'
    assert connector.config['api_key'] == 'env-key'


def test_environment_id_resolves_with_environment_credentials(ctx, clean_env):
    connector, model = ac.resolve_asr_connector('env:openai-whisper:small')
    assert model == 'small'
    assert connector.PROVIDER_NAME == 'openai_whisper'
    assert connector.config['base_url'] == 'http://env.lan/v1'
    assert connector.config['model'] == 'small'


def test_hidden_profile_still_resolves_for_a_recording(ctx, clean_env):
    created = ac.create_profile(_profile("Hidden", "small") | {"visible": False}).model
    assert resolve_transcription_model(None) is None
    connector, model = ac.resolve_asr_connector(created['id'])
    assert model == 'small'
    assert connector.config['api_key'] == 'small-key'


def test_no_first_profile_fallback(ctx, clean_env):
    ac.create_profile(_profile("First", "small"))
    assert ac.default_profile_id() is None
    assert resolve_transcription_model(None) is None
    connector, model = ac.resolve_asr_connector(None)
    assert model == 'whisper-1'
    assert connector.config['base_url'] == 'http://env.lan/v1'
