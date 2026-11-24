"""Admin API for transcription models: the eight canonical endpoints.

Every action is exercised through the HTTP surface: authorization, GET shape,
CRUD, default, import, real probes (mocked at httpx), secret masking, visible
enforcement, error mapping and the audit trail.
"""

import re
import uuid

import httpx
import pytest

from src.app import app, db
from src.models import AuditEvent, SystemSetting, User
from src.services import asr_config as ac
from src.services.transcription.base import ProbeResult
from src.services.transcription.registry import get_registry

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

ENV_NAMES = (
    "TRANSCRIPTION_CONNECTOR", "TRANSCRIPTION_BASE_URL", "TRANSCRIPTION_API_KEY",
    "TRANSCRIPTION_MODEL", "ASR_BASE_URL", "ASR_API_KEY", "ASR_DIARIZE",
    "USE_ASR_ENDPOINT", "WHISPER_MODEL",
)
LEGACY_KEYS = ("transcription_models_visible_json", "transcription_default_model")

ENDPOINTS = (
    ("get", "/admin/asr-config"),
    ("post", "/admin/asr-config/models"),
    ("post", "/admin/asr-config/models/m_missing"),
    ("delete", "/admin/asr-config/models/m_missing"),
    ("post", "/admin/asr-config/default"),
    ("post", "/admin/asr-config/test"),
    ("post", "/admin/asr-config/discover"),
    ("post", "/admin/asr-config/import"),
)


def _clear_settings():
    SystemSetting.query.filter(
        SystemSetting.key.in_((ac.SETTING_KEY,) + LEGACY_KEYS)).delete(
            synchronize_session=False)
    db.session.commit()


def _reset_registry():
    registry = get_registry()
    registry._active_connector = None
    registry._connector_name = ""


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
    _reset_registry()
    return monkeypatch


def _mk_user(is_admin):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"a_{suffix}", email=f"{suffix}@local.test",
                password="x", is_admin=is_admin)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def admin_user(ctx):
    user = _mk_user(True)
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture
def normal_user(ctx):
    user = _mk_user(False)
    yield user
    db.session.delete(user)
    db.session.commit()


def _client(user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def admin_client(admin_user):
    return _client(admin_user)


@pytest.fixture
def normal_client(normal_user):
    return _client(normal_user)


def _profile(**overrides):
    profile = {
        "label": "Profile A",
        "connector": "openai_whisper",
        "config": {"api_key": "profile-key", "base_url": "http://profile.lan/v1",
                   "model": "whisper-1"},
        "visible": True,
    }
    profile.update(overrides)
    return profile


def _create(client, **overrides):
    response = client.post('/admin/asr-config/models', json=_profile(**overrides))
    assert response.status_code in (200, 201), response.get_json()
    return response.get_json()['model']


def _tencent_payload(**overrides):
    payload = {
        "label": "Tencent A",
        "connector": "tencent_asr",
        "config": {"secret_id": "id", "secret_key": "secret",
                   "model": "16k_zh_en_meeting"},
        "visible": True,
    }
    payload.update(overrides)
    return payload


def _json_response(status, payload):
    request = httpx.Request('GET', 'http://provider.lan/v1/models')
    return httpx.Response(status, json=payload, request=request)


# ------------------------------------------------------------ authorization


@pytest.mark.parametrize('method,path', ENDPOINTS)
def test_every_endpoint_403_for_non_admin(normal_client, clean_env, method, path):
    response = getattr(normal_client, method)(path, json={})
    assert response.status_code == 403
    assert response.get_json()['code'] == 'unauthorized'


# ---------------------------------------------------------------- GET shape


def test_get_shape_models_limit_default_connectors_environment(admin_client, clean_env):
    created = _create(admin_client)
    response = admin_client.get('/admin/asr-config')
    assert response.status_code == 200
    body = response.get_json()

    assert body['masked_api_key'] == ac.MASKED_API_KEY
    assert body['model_limit'] == ac.MAX_MODELS
    assert body['default'] is None
    assert [model['id'] for model in body['models']] == [created['id']]

    connector = body['connectors'][0]
    for field in ('name', 'display_name', 'description', 'config_schema', 'capabilities'):
        assert field in connector
    assert all(connector['display_name'] for connector in body['connectors'])

    environment = body['environment']
    assert environment['connector'] == 'openai_whisper'
    assert environment['config']['model'] == 'whisper-1'
    assert environment['visible'] is True


def test_get_masks_the_stored_api_key(admin_client, clean_env):
    _create(admin_client)
    body = admin_client.get('/admin/asr-config').get_json()
    assert body['models'][0]['config']['api_key'] == ac.MASKED_API_KEY
    assert 'profile-key' not in str(body)


def test_get_masks_every_write_only_secret(admin_client, clean_env):
    payload = _tencent_payload(config={'secret_id': 'id', 'secret_key': 'sk-live-abc',
                                       'model': '16k_zh_en_meeting'})
    admin_client.post('/admin/asr-config/models', json=payload)
    body = admin_client.get('/admin/asr-config').get_json()
    config = body['models'][0]['config']
    assert config['secret_key'] == ac.MASKED_API_KEY
    assert 'sk-live-abc' not in str(body)


def test_get_connectors_have_display_name_description_schema_capabilities(
        admin_client, clean_env):
    body = admin_client.get('/admin/asr-config').get_json()
    by_name = {connector['name']: connector for connector in body['connectors']}
    whisper = by_name['openai_whisper']
    assert whisper['display_name'] == 'OpenAI Whisper'
    assert whisper['description']
    assert 'properties' in whisper['config_schema']
    assert 'HOTWORDS' in whisper['capabilities']


def test_environment_id_is_env_format(admin_client, clean_env):
    body = admin_client.get('/admin/asr-config').get_json()
    environment_id = body['environment']['id']
    assert environment_id == 'env:openai-whisper:whisper-1'
    assert re.match(r'^env:[a-z0-9-]+:.*$', environment_id)
    assert not environment_id.startswith('m_')


def test_no_secret_in_list_or_public_payloads(admin_client, clean_env):
    _create(admin_client)
    listed = ac.list_models()
    assert listed[0]['config']['api_key'] == ac.MASKED_API_KEY
    public = ac.visible_models()
    assert 'config' not in public[0]


# --------------------------------------------------------------------- CRUD


def test_create_profile(admin_client, clean_env):
    created = _create(admin_client)
    assert created['label'] == 'Profile A'
    assert created['connector'] == 'openai_whisper'
    assert created['visible'] is True
    assert created['config']['api_key'] == ac.MASKED_API_KEY
    assert ac.list_models(reveal_key=True)[0]['config']['api_key'] == 'profile-key'


def test_create_invalid_connector_400(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/models',
                                 json=_profile(connector='nope'))
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_asr_config'


def test_create_label_taken_400(admin_client, clean_env):
    _create(admin_client)
    response = admin_client.post('/admin/asr-config/models', json=_profile())
    assert response.status_code == 400
    assert response.get_json()['code'] == 'asr_profile_label_taken'


def test_create_limit_reached_400(admin_client, clean_env):
    for index in range(ac.MAX_MODELS):
        _create(admin_client, label=f'Profile {index}')
    response = admin_client.post('/admin/asr-config/models', json=_profile(label='One too many'))
    assert response.status_code == 400
    assert response.get_json()['code'] == 'asr_profile_limit_reached'


def test_update_profile(admin_client, clean_env):
    created = _create(admin_client)
    response = admin_client.post(
        f'/admin/asr-config/models/{created["id"]}',
        json={'label': 'Renamed', 'config': {'model': 'small'}})
    assert response.status_code == 200
    model = response.get_json()['model']
    assert model['label'] == 'Renamed'
    assert model['config']['model'] == 'small'
    assert model['config']['api_key'] == ac.MASKED_API_KEY
    # An omitted or empty key never replaces the stored one.
    assert ac.get_model(created['id'], reveal_key=True)['config']['api_key'] == 'profile-key'


def test_update_typed_key_replaces_the_stored_one(admin_client, clean_env):
    created = _create(admin_client)
    response = admin_client.post(
        f'/admin/asr-config/models/{created["id"]}',
        json={'config': {'api_key': 'new-key'}})
    assert response.status_code == 200
    assert response.get_json()['model']['config']['api_key'] == ac.MASKED_API_KEY
    assert ac.get_model(created['id'], reveal_key=True)['config']['api_key'] == 'new-key'


def test_update_clear_secrets_removes_the_stored_key(admin_client, clean_env):
    created = _create(admin_client)
    response = admin_client.post(
        f'/admin/asr-config/models/{created["id"]}',
        json={'clear_secrets': True})
    assert response.status_code == 200
    assert ac.get_model(created['id'], reveal_key=True)['config']['api_key'] == ''


def test_update_missing_404(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/models/m_missing', json={'label': 'X'})
    assert response.status_code == 404
    assert response.get_json()['code'] == 'asr_profile_not_found'


def test_delete_profile(admin_client, clean_env):
    created = _create(admin_client)
    response = admin_client.delete(f'/admin/asr-config/models/{created["id"]}')
    assert response.status_code == 200
    assert ac.list_models() == []


def test_delete_missing_404(admin_client, clean_env):
    response = admin_client.delete('/admin/asr-config/models/m_missing')
    assert response.status_code == 404
    assert response.get_json()['code'] == 'asr_profile_not_found'


def test_default_set_and_clear(admin_client, clean_env):
    created = _create(admin_client)
    response = admin_client.post('/admin/asr-config/default', json={'profile_id': created['id']})
    assert response.status_code == 200
    assert response.get_json()['default'] == created['id']
    assert admin_client.get('/admin/asr-config').get_json()['default'] == created['id']

    response = admin_client.post('/admin/asr-config/default', json={'profile_id': None})
    assert response.status_code == 200
    assert response.get_json()['default'] is None


def test_default_hidden_rejected_400(admin_client, clean_env):
    created = _create(admin_client, visible=False)
    response = admin_client.post('/admin/asr-config/default', json={'profile_id': created['id']})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'asr_profile_not_visible'


def test_default_missing_404(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/default', json={'profile_id': 'm_missing'})
    assert response.status_code == 404
    assert response.get_json()['code'] == 'asr_profile_not_found'


def test_hiding_default_rejected_400(admin_client, clean_env):
    created = _create(admin_client)
    admin_client.post('/admin/asr-config/default', json={'profile_id': created['id']})
    response = admin_client.post(f'/admin/asr-config/models/{created["id"]}',
                                 json={'visible': False})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'asr_profile_not_visible'


def test_import_from_environment(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/import', json={})
    assert response.status_code == 201
    model = response.get_json()['model']
    assert model['connector'] == 'openai_whisper'
    assert model['config']['model'] == 'whisper-1'
    assert model['config']['api_key'] == ac.MASKED_API_KEY

    again = admin_client.post('/admin/asr-config/import', json={})
    assert again.status_code == 201
    assert again.get_json()['model']['id'] == model['id']
    assert len(ac.list_models()) == 1


def test_import_without_environment_400(admin_client, clean_env):
    for name in ENV_NAMES:
        clean_env.delenv(name, raising=False)
    _reset_registry()
    response = admin_client.post('/admin/asr-config/import', json={})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_asr_config'


# ------------------------------------------------------------ strict bodies


def test_missing_body_is_400_no_data_provided(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/models')
    assert response.status_code == 400
    assert response.get_json()['code'] == 'no_data_provided'


def test_non_json_content_type_is_415(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/models', data='label=A',
                                 content_type='application/x-www-form-urlencoded')
    assert response.status_code == 415
    assert response.get_json()['code'] == 'invalid_json'


def test_malformed_json_is_400(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/models', data='{',
                                 content_type='application/json')
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_json'


def test_non_object_body_is_400(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/models', json=['label'])
    assert response.status_code == 400
    assert response.get_json()['code'] == 'body_must_be_object'


def test_default_requires_profile_id(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/default', json={})
    assert response.status_code == 400
    body = response.get_json()
    assert body['code'] == 'missing_required_field'
    assert body['error'] == 'Missing required field: profile_id'


def test_default_null_still_returns_to_environment(admin_client, clean_env):
    created = _create(admin_client)
    admin_client.post('/admin/asr-config/default', json={'profile_id': created['id']})
    response = admin_client.post('/admin/asr-config/default', json={'profile_id': None})
    assert response.status_code == 200
    assert response.get_json()['default'] is None


# ------------------------------------------------------------ test/discover


def test_test_success_returns_models_and_ok(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)
    seen = {}

    def fake_get(url, **kwargs):
        seen['url'] = url
        seen['headers'] = kwargs.get('headers')
        return _json_response(200, {'data': [{'id': 'whisper-1'}, {'id': 'tts-1'}]})

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/test',
                                 json={'profile_id': created['id']})
    assert response.status_code == 200
    body = response.get_json()
    assert body['success'] is True and body['ok'] is True
    assert [model['id'] for model in body['models']] == ['whisper-1', 'tts-1']
    assert seen['url'] == 'http://profile.lan/v1/models'
    assert seen['headers'] == {'Authorization': 'Bearer profile-key'}


def test_test_unsaved_profile(admin_client, clean_env, monkeypatch):
    def fake_get(url, **kwargs):
        return _json_response(200, {'data': [{'id': 'whisper-1'}]})

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/test', json={
        'connector': 'openai_whisper',
        'config': {'base_url': 'http://unsaved.lan/v1', 'api_key': 'k', 'model': 'whisper-1'},
    })
    assert response.status_code == 200
    assert response.get_json()['ok'] is True


def test_test_401_returns_502_with_status(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)

    def fake_get(url, **kwargs):
        return _json_response(401, {'error': {'message': 'Unauthorized'}})

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/test',
                                 json={'profile_id': created['id']})
    assert response.status_code == 502
    body = response.get_json()
    assert body['ok'] is False
    assert body['error'] == 'asr_provider_http_error'
    assert body['status'] == 401
    assert 'Unauthorized' in body['detail']


def test_test_timeout_returns_502(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)

    def fake_get(url, **kwargs):
        raise httpx.TimeoutException('timed out')

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/test',
                                 json={'profile_id': created['id']})
    assert response.status_code == 502
    body = response.get_json()
    assert body['ok'] is False
    assert body['error'] == 'asr_provider_unreachable'
    assert 'timed out' in body['detail']


def test_test_connection_error_returns_502(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)

    def fake_get(url, **kwargs):
        raise httpx.ConnectError('refused')

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/test',
                                 json={'profile_id': created['id']})
    assert response.status_code == 502
    body = response.get_json()
    assert body['ok'] is False
    assert body['error'] == 'asr_provider_unreachable'
    assert 'refused' in body['detail']


def test_test_unsupported_returns_ok_false_unsupported(admin_client, clean_env):
    created = admin_client.post('/admin/asr-config/models',
                                json=_tencent_payload()).get_json()['model']
    response = admin_client.post('/admin/asr-config/test',
                                 json={'profile_id': created['id']})
    assert response.status_code == 200
    body = response.get_json()
    assert body['success'] is True
    assert body['ok'] is False
    assert body['unsupported'] is True


def test_test_invalid_config_400(admin_client, clean_env):
    response = admin_client.post('/admin/asr-config/test',
                                 json={'connector': 'nope', 'config': {}})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_asr_config'

    response = admin_client.post('/admin/asr-config/test', json={})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_asr_config'

    response = admin_client.post('/admin/asr-config/test', json={
        'connector': 'tencent_asr', 'config': {'secret_id': '', 'secret_key': ''}})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_asr_config'


def test_invalid_config_400_says_what_is_wrong(admin_client, clean_env):
    """The UI shows detail or error; a bare code or a raw key tells nobody why
    the connection test failed."""
    response = admin_client.post('/admin/asr-config/test',
                                 json={'connector': 'nope', 'config': {}})
    body = response.get_json()
    assert body['detail']
    assert not body['error'].startswith('server.apiError.')


def test_discover_returns_filtered_models(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)

    def fake_get(url, **kwargs):
        return _json_response(200, {'data': [
            {'id': 'whisper-1'}, {'id': 'whisper-large-v3'}, {'id': 'tts-1'},
        ]})

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/discover',
                                 json={'profile_id': created['id']})
    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True
    assert [model['id'] for model in body['models']] == ['whisper-1', 'whisper-large-v3']


def test_discover_empty_list(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)

    def fake_get(url, **kwargs):
        return _json_response(200, {'data': []})

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/discover',
                                 json={'profile_id': created['id']})
    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True
    assert body['models'] == []


def test_discover_unsupported(admin_client, clean_env):
    created = admin_client.post('/admin/asr-config/models',
                                json=_tencent_payload()).get_json()['model']
    response = admin_client.post('/admin/asr-config/discover',
                                 json={'profile_id': created['id']})
    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is False
    assert body['unsupported'] is True


def test_discover_401_returns_502(admin_client, clean_env, monkeypatch):
    created = _create(admin_client)

    def fake_get(url, **kwargs):
        return _json_response(403, {'error': {'message': 'Forbidden'}})

    from src.services.transcription.connectors import openai_whisper
    monkeypatch.setattr(openai_whisper.httpx, 'get', fake_get)

    response = admin_client.post('/admin/asr-config/discover',
                                 json={'profile_id': created['id']})
    assert response.status_code == 502
    body = response.get_json()
    assert body['error'] == 'asr_provider_http_error'
    assert body['status'] == 403


# -------------------------------------------------------------------- audit

CANONICAL_EVENTS = (
    'admin.asr_model.created',
    'admin.asr_model.changed',
    'admin.asr_model.deleted',
    'admin.asr_model.imported',
    'admin.asr_default.changed',
)


def _last_event_id():
    return db.session.query(db.func.max(AuditEvent.id)).scalar() or 0


def _events_since(event_id):
    return [event.event for event in AuditEvent.query.filter(
        AuditEvent.id > event_id).order_by(AuditEvent.id).all()
        if event.event.startswith('admin.asr')]


def test_each_action_writes_exactly_one_event(admin_client, admin_user, clean_env):
    start = _last_event_id()
    created = _create(admin_client)
    admin_client.post(f'/admin/asr-config/models/{created["id"]}',
                      json={'label': 'Renamed'})
    admin_client.post('/admin/asr-config/default', json={'profile_id': created['id']})
    admin_client.delete(f'/admin/asr-config/models/{created["id"]}')
    admin_client.post('/admin/asr-config/import', json={})

    events = _events_since(start)
    assert events == [
        'admin.asr_model.created',
        'admin.asr_model.changed',
        'admin.asr_default.changed',
        'admin.asr_model.deleted',
        'admin.asr_model.imported',
    ]
    assert set(events) <= set(CANONICAL_EVENTS)


def test_removed_alias_helper(admin_client, admin_user, clean_env):
    import src.api.admin as admin_module

    assert not hasattr(admin_module, '_asr_audit_variants')
    start = _last_event_id()
    _create(admin_client)
    assert 'admin.asr_model.created' in _events_since(start)
    for alias in (
        'admin.transcription_model.created',
        'admin.transcription_model.changed',
        'admin.transcription_model.deleted',
        'admin.transcription_model.imported',
        'admin.transcription_default.changed',
    ):
        assert alias not in _events_since(start)


def test_removed_alias_routes(admin_client, clean_env):
    assert admin_client.get('/admin/asr').status_code == 404
    assert admin_client.get('/admin/transcription-config').status_code == 404
    assert admin_client.get('/admin/asr-models').status_code == 404
    assert admin_client.get('/admin/asr/profiles').status_code == 404
    assert admin_client.get('/admin/transcription/asr-config').status_code == 404
    assert admin_client.get('/admin/asr-config/connectors').status_code == 404
    assert admin_client.post('/admin/asr-config/set-default', json={}).status_code == 404
    assert admin_client.post('/admin/asr-config/import-from-environment', json={}).status_code == 404
    assert admin_client.post('/admin/asr-config/models/m_x/test', json={}).status_code == 404
