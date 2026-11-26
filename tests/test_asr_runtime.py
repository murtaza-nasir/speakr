"""Runtime resolution: the selected profile drives transcription, chunking and
the voice-embedding backend; the public APIs expose only visible profiles.

SHARED-DB: each test removes its own users, recordings and ASR settings.
"""

import io
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.app import app, db
from src.models import Recording, SystemSetting, User
from src.services import asr_config as ac
from src.services import voice_embedding_check as vec
from src.services import recording_ingestion

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
    with app.app_context(), \
         patch("src.services.voice_embedding_check.check_voice_embeddings"):
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
    user = User(username=f"rt_{suffix}", email=f"rt_{suffix}@local.test",
                password="x")
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user):
    recording = Recording(user_id=user.id, original_filename="rt.mp3",
                          audio_path="local://rt.mp3", status="PENDING",
                          title="runtime")
    db.session.add(recording)
    db.session.commit()
    return recording


def _audio(recording):
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"rt_{recording.id}.mp3")
    with open(path, "wb") as handle:
        handle.write(b"\x00" * 1024)
    return path


def _client(user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def _connector(text="A transcript body."):
    connector = MagicMock()
    connector.PROVIDER_NAME = "test-connector"
    connector.supports_diarization = False
    connector.default_diarize = False
    specs = MagicMock()
    specs.max_duration_seconds = 1800
    specs.handles_chunking_internally = False
    specs.recommended_chunk_seconds = 600
    specs.unsupported_codecs = []
    connector.specifications = specs
    response = MagicMock()
    response.text = text
    response.segments = None
    response.speakers = None
    response.speaker_embeddings = None
    response.has_diarization.return_value = False
    connector.transcribe.return_value = response
    return connector


def _profile(label="Runtime profile", **overrides):
    values = {
        "label": label,
        "connector": "openai_whisper",
        "config": {"api_key": "profile-key", "base_url": "http://profile.lan/v1",
                   "model": "small"},
        "visible": True,
    }
    values.update(overrides)
    return values


def _run_transcription(recording, path, **kwargs):
    from src.tasks import processing as proc

    # The admin route commits right after saving the configuration; the task
    # runs in its own app context, on its own connection.
    db.session.commit()
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
            app.app_context(), recording.id, path, "rt.mp3", time.time(),
            mime_type="audio/mpeg", **kwargs)


# ----------------------------------------------------------------- runtime


def test_transcribe_uses_profile_connector_and_model(ctx, clean_env):
    user = _user()
    recording = _recording(user)
    created = ac.create_profile(_profile()).model
    ac.set_default(created['id'])

    profile_connector = _connector()
    env_connector = _connector()
    try:
        with patch("src.services.asr_config.resolve_asr_connector",
                   return_value=(profile_connector, "small")) as resolve, \
             patch("src.services.transcription.get_connector",
                   return_value=env_connector):
            _run_transcription(recording, _audio(recording),
                               transcription_model=created['id'])
        resolve.assert_called_once_with(created['id'])
        profile_connector.transcribe.assert_called_once()
        env_connector.transcribe.assert_not_called()
        request = profile_connector.transcribe.call_args.args[0]
        assert request.model == "small"
    finally:
        Recording.query.filter_by(id=recording.id).delete()
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_transcribe_legacy_model_name_uses_environment_connector(ctx, clean_env):
    user = _user()
    recording = _recording(user)
    env_connector = _connector()
    profile_connector = _connector()
    try:
        with patch("src.services.asr_config.resolve_asr_connector",
                   return_value=(env_connector, "large-v3")), \
             patch("src.services.transcription.get_connector",
                   return_value=env_connector):
            _run_transcription(recording, _audio(recording),
                               transcription_model="large-v3")
        env_connector.transcribe.assert_called_once()
        profile_connector.transcribe.assert_not_called()
        assert env_connector.transcribe.call_args.args[0].model == "large-v3"
    finally:
        Recording.query.filter_by(id=recording.id).delete()
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_incognito_uses_profile(ctx, clean_env, tmp_path):
    from src.tasks import processing as proc

    profile_connector = _connector(text="hello incognito")
    env_connector = _connector(text="wrong")
    seen = {}

    def profile_transcribe(request):
        seen['model'] = request.model
        response = MagicMock()
        response.text = 'hello incognito'
        response.segments = None
        response.speakers = None
        response.has_diarization.return_value = False
        return response

    profile_connector.transcribe.side_effect = profile_transcribe

    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 512)

    conv = MagicMock()
    conv.was_converted = False
    conv.output_path = str(wav)
    conv.mime_type = "audio/wav"
    chunker = MagicMock()
    chunker.needs_chunking.return_value = False
    chunker.get_audio_duration.return_value = 1.0

    registry = MagicMock()
    registry.get_active_connector.return_value = env_connector
    with patch("src.services.transcription.get_registry", return_value=registry), \
         patch("src.services.asr_config.resolve_asr_connector",
               return_value=(profile_connector, "small")), \
         patch.object(proc, "convert_if_needed", return_value=conv), \
         patch.object(proc, "chunking_service", chunker), \
         patch.object(proc, "get_codec_info", return_value={}), \
         patch.object(proc, "transcription_tracker") as tracker:
        tracker.check_budget.return_value = (True, 0, None)
        result = proc.transcribe_incognito(
            str(wav), 'incognito.wav', language='en', transcription_model='m_x')

    assert seen['model'] == 'small'
    env_connector.transcribe.assert_not_called()
    assert result['transcription'] == 'hello incognito'


def test_chunking_specs_follow_profile(ctx, clean_env):
    from src.api import recordings

    ac.create_profile(_profile(
        "Tencent profile", connector='tencent_asr',
        config={'secret_id': 'id', 'secret_key': 'secret',
                'model': '16k_zh_en_meeting'}))
    document = ac.load_document()
    ac.set_default(document['models'][0]['id'])

    upload = io.BytesIO(b"x" * 1024)
    with patch.object(recordings, "USE_NEW_TRANSCRIPTION_ARCHITECTURE", True), \
         patch.object(recordings, "chunking_service", MagicMock()):
        policy = recording_ingestion.resolve_size_policy(
            file=upload, form={}, original_filename="talk.wav",
            enforce_upload_limits=True)

    assert policy.connector_specs is not None
    assert policy.connector_specs.handles_chunking_internally is True
    assert policy.connector_specs.max_duration_seconds == 18000


def test_voice_fingerprint_follows_default_profile(ctx, clean_env):
    with patch.dict(os.environ, {"ASR_BASE_URL": ""}, clear=False):
        before = vec.backend_fingerprint()
        created = ac.create_profile(_profile()).model
        ac.set_default(created['id'])
        after = vec.backend_fingerprint()
    assert before != after


def test_voice_support_follows_default_profile_then_environment(ctx, clean_env):
    assert vec.embeddings_supported() is False
    created = ac.create_profile({
        "label": "Embedded ASR",
        "connector": "asr_endpoint",
        "config": {"base_url": "http://asr.test:9000",
                   "return_speaker_embeddings": True},
        "visible": True,
    }).model
    ac.set_default(created['id'])
    assert vec.embeddings_supported() is True
    ac.clear_default()
    assert vec.embeddings_supported() is False


def test_deleted_profile_recording_falls_back_to_environment(ctx, clean_env):
    created = ac.create_profile(_profile()).model
    ac.set_default(created['id'])
    ac.delete_profile(created['id'])
    connector, model = ac.resolve_asr_connector(created['id'])
    assert connector.PROVIDER_NAME == 'openai_whisper'
    assert connector.config.get('base_url') == 'http://env.lan/v1'
    assert connector.config.get('api_key') == 'env-key'


# -------------------------------------------------------------- public API


def test_config_transcription_model_options_visible_only(ctx, clean_env):
    user = _user()
    try:
        visible = ac.create_profile(_profile("Visible")).model
        ac.create_profile(_profile("Hidden", visible=False))
        options = _client(user).get('/api/config').get_json()['transcription_model_options']
        assert [option['value'] for option in options] == [visible['id']]
    finally:
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_config_transcription_model_options_empty_without_profiles(ctx, clean_env):
    user = _user()
    try:
        options = _client(user).get('/api/config').get_json()['transcription_model_options']
        assert options == []
    finally:
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_config_options_include_capabilities(ctx, clean_env):
    user = _user()
    try:
        created = ac.create_profile(_profile()).model
        option = _client(user).get('/api/config').get_json()[
            'transcription_model_options'][0]
        assert option['capabilities'] == created['capabilities']
        assert option['connector'] == 'openai_whisper'
    finally:
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_api_v1_transcription_lists_visible_profiles(ctx, clean_env):
    user = _user()
    try:
        visible = ac.create_profile(_profile("Visible")).model
        ac.create_profile(_profile("Hidden", visible=False))
        body = _client(user).get('/api/v1/transcription').get_json()
        assert [model['value'] for model in body['models']] == [visible['id']]
        assert body['models'][0]['capabilities'] == visible['capabilities']
    finally:
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_api_v1_transcription_global_capabilities_describe_default(ctx, clean_env):
    user = _user()
    try:
        created = ac.create_profile(_profile()).model
        ac.set_default(created['id'])
        body = _client(user).get('/api/v1/transcription').get_json()
        assert body['connector'] == 'openai_whisper'
        assert body['capabilities'] == created['capabilities']
        assert body['default_model'] == created['id']
    finally:
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


# ---------------------------------------- a chosen profile that cannot be built


def _broken_profile(label="Broken profile"):
    # OpenAI Whisper refuses to build without a key.
    return ac.create_profile(_profile(label=label, config={
        "api_key": "", "base_url": "http://profile.lan/v1", "model": "small"})).model


def test_a_broken_chosen_profile_raises_instead_of_using_the_environment(ctx, clean_env):
    broken = _broken_profile()
    with pytest.raises(ac.ASRProfileUnavailable) as raised:
        ac.resolve_asr_connector(broken['id'])
    assert raised.value.profile_id == broken['id']
    assert 'Broken profile' in str(raised.value)
    assert 'env-key' not in str(raised.value)


def test_a_broken_default_profile_raises_instead_of_using_the_environment(ctx, clean_env):
    broken = _broken_profile()
    ac.set_default(broken['id'])
    with pytest.raises(ac.ASRProfileUnavailable):
        ac.resolve_asr_connector(None)


def test_no_choice_and_no_default_still_uses_the_environment(ctx, clean_env):
    _broken_profile()
    connector, model = ac.resolve_asr_connector(None)
    assert connector.config["api_key"] == "env-key"
    assert model == "whisper-1"


def test_a_job_on_a_broken_profile_fails_without_the_environment(ctx, clean_env, caplog):
    user = _user()
    recording = _recording(user)
    broken = _broken_profile()
    env_connector = _connector()
    try:
        with caplog.at_level("ERROR"), \
             patch("src.services.transcription.get_connector", return_value=env_connector):
            message = _run_transcription(recording, _audio(recording),
                                         transcription_model=broken['id'])
        env_connector.transcribe.assert_not_called()
        stored = db.session.get(Recording, recording.id)
        db.session.refresh(stored)
        assert stored.status == "FAILED"
        assert 'Transcription model "Broken profile" cannot be used' in stored.error_message
        assert "System settings > Transcription models" in stored.error_message
        assert message == stored.error_message
        assert broken['id'] in caplog.text
    finally:
        Recording.query.filter_by(id=recording.id).delete()
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def test_registry_transcribe_does_not_fall_back_for_a_broken_profile(ctx, clean_env):
    from src.services.transcription import registry
    from src.services.transcription.base import TranscriptionRequest

    broken = _broken_profile()
    env_connector = _connector()
    with patch.object(registry.get_registry(), "get_active_connector", return_value=env_connector):
        with pytest.raises(ac.ASRProfileUnavailable):
            registry.transcribe(TranscriptionRequest(audio_file=None, filename="x.mp3",
                                                     model=broken['id']))
        with pytest.raises(ac.ASRProfileUnavailable):
            registry.get_connector(broken['id'])
    env_connector.transcribe.assert_not_called()
