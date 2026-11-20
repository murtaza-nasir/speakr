"""Admin API for configured LLM models and role assignments.

An administrator reads the configured models and the effective configuration
of each role, creates, updates and deletes a model, assigns it to a role or
reverts the role to the environment, and asks the backend to discover the
models a provider offers. Secrets never leave unmasked, a model in use cannot
be deleted, and every change is audited by id and field names only.
"""

import json
import os
import sys
import uuid

import httpx
import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.admin as admin_api
from src.app import app, db
from src.models import AuditEvent, SystemSetting, User
from src.services import llm_config

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _client(user):
    client = _Client(app, app.response_class)
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def users():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        admin = User(username=f'adm_{suffix}', email=f'adm_{suffix}@local.test', password='x', is_admin=True)
        plain = User(username=f'usr_{suffix}', email=f'usr_{suffix}@local.test', password='x')
        db.session.add_all([admin, plain])
        db.session.commit()
        SystemSetting.query.filter_by(key='audit_log_enabled').delete()
        db.session.commit()
        yield admin, plain
        SystemSetting.query.filter_by(key=llm_config.SETTING_KEY).delete()
        db.session.commit()
        db.session.delete(db.session.get(User, admin.id))
        db.session.delete(db.session.get(User, plain.id))
        db.session.commit()


@pytest.fixture
def admin_client(users, monkeypatch):
    admin, _ = users
    for name in ('CHAT_MODEL_NAME', 'CHAT_MODEL_BASE_URL', 'CHAT_MODEL_API_KEY',
                 'TRANSLATION_MODEL_NAME', 'TRANSLATION_MODEL_BASE_URL',
                 'TRANSLATION_MODEL_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    llm_config.clear_client_cache()
    return _client(admin)


def _max_audit_id():
    row = AuditEvent.query.order_by(AuditEvent.id.desc()).first()
    return row.id if row else 0


def _audit_rows(start, event):
    db.session.expire_all()
    return (AuditEvent.query
            .filter(AuditEvent.id > start, AuditEvent.event == event)
            .order_by(AuditEvent.id)
            .all())


def _document():
    return llm_config.load_document()


def _create(client, **overrides):
    payload = {
        'label': 'OpenRouter',
        'base_url': 'http://provider.lan/v1',
        'model_name': 'openai/gpt-5',
        'api_key': 'stored-secret',
    }
    payload.update(overrides)
    return client.post('/admin/llm-config/models', json=payload)


# ---------------------------------------------------------------- authorization


def test_non_admins_are_rejected(users, monkeypatch):
    _, plain = users
    client = _client(plain)
    assert client.get('/admin/llm-config').status_code == 403
    assert client.post('/admin/llm-config/models', json={}).status_code == 403
    assert client.post('/admin/llm-config/models/m_x', json={}).status_code == 403
    assert client.delete('/admin/llm-config/models/m_x').status_code == 403
    assert client.post('/admin/llm-config/assignments/text', json={}).status_code == 403
    assert client.delete('/admin/llm-config/assignments/text').status_code == 403
    assert client.post('/admin/llm-config/test', json={}).status_code == 403
    assert client.post('/admin/llm-config/models/m_x/test', json={}).status_code == 403


# ---------------------------------------------------------------- read


def test_get_returns_models_assignments_and_the_effective_roles(admin_client, monkeypatch):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'env-secret')
    response = admin_client.get('/admin/llm-config')
    body = response.get_json()
    assert response.status_code == 200
    assert body['models'] == []
    assert body['assignments'] == {role: None for role in llm_config.ROLES}
    assert body['masked_api_key'] == llm_config.MASKED_API_KEY
    assert body['roles']['text']['source'] == 'environment'
    assert body['roles']['text']['api_key'] == llm_config.MASKED_API_KEY
    assert 'env-secret' not in json.dumps(body)


def test_get_lists_a_created_model_with_its_key_masked(admin_client):
    created = _create(admin_client).get_json()['model']
    body = admin_client.get('/admin/llm-config').get_json()
    assert [model['id'] for model in body['models']] == [created['id']]
    assert body['models'][0]['api_key'] == llm_config.MASKED_API_KEY
    assert 'stored-secret' not in json.dumps(body)


# ---------------------------------------------------------------- create and update


def test_create_saves_the_model_and_applies_it_once_assigned(admin_client):
    response = _create(admin_client)
    body = response.get_json()
    assert response.status_code == 201, body
    assert body['success'] is True
    model = body['model']
    assert model['api_key'] == llm_config.MASKED_API_KEY
    assert 'stored-secret' not in json.dumps(body)
    assert model['id'].startswith('m_')

    assigned = admin_client.post('/admin/llm-config/assignments/text',
                                 json={'model_id': model['id']}).get_json()
    assert assigned['roles']['text']['source'] == 'model'
    assert assigned['roles']['text']['model_name'] == 'openai/gpt-5'


@pytest.mark.parametrize('payload', [
    {'label': 'x', 'base_url': 'not-a-url', 'model_name': 'm'},
    {'label': 'x', 'base_url': 'file:///etc/passwd', 'model_name': 'm'},
    {'label': '', 'base_url': 'http://a.lan/v1', 'model_name': 'm'},
    {'label': 'x', 'base_url': 'http://a.lan/v1'},
    {'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'extra_body': '{not json}'},
    {'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'modle_name': 'typo'},
    {'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'gpt5_force': 'sometimes'},
])
def test_invalid_input_is_rejected_and_nothing_is_saved(admin_client, payload):
    response = admin_client.post('/admin/llm-config/models', json=payload)
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_llm_config'
    assert _document()['models'] == []


def test_a_duplicate_label_is_rejected_without_case(admin_client):
    _create(admin_client, label='OpenRouter')
    response = _create(admin_client, label='openrouter')
    assert response.status_code == 400
    assert response.get_json()['code'] == 'llm_model_label_taken'
    assert len(_document()['models']) == 1


def test_update_keeps_omitted_fields_and_the_masked_key(admin_client):
    model = _create(admin_client).get_json()['model']
    response = admin_client.post(f"/admin/llm-config/models/{model['id']}", json={
        'model_name': 'openai/gpt-5-mini',
        'api_key': llm_config.MASKED_API_KEY,
    })
    body = response.get_json()
    assert response.status_code == 200, body
    assert body['model']['model_name'] == 'openai/gpt-5-mini'
    assert body['model']['base_url'] == 'http://provider.lan/v1'
    stored = llm_config._find_model(_document(), model['id'])[1]
    assert stored['api_key'] == 'stored-secret'


def test_update_applies_the_new_key_and_can_clear_it(admin_client):
    model = _create(admin_client).get_json()['model']
    admin_client.post(f"/admin/llm-config/models/{model['id']}", json={'api_key': 'new-secret'})
    assert llm_config._find_model(_document(), model['id'])[1]['api_key'] == 'new-secret'

    admin_client.post(f"/admin/llm-config/models/{model['id']}", json={'clear_api_key': True})
    assert llm_config._find_model(_document(), model['id'])[1]['api_key'] == ''
    listed = admin_client.get('/admin/llm-config').get_json()['models'][0]
    assert listed['api_key'] == ''


def test_an_unknown_model_is_a_404(admin_client):
    response = admin_client.post('/admin/llm-config/models/m_missing', json={'model_name': 'x'})
    assert response.status_code == 404
    assert response.get_json()['code'] == 'llm_model_not_found'

    response = admin_client.delete('/admin/llm-config/models/m_missing')
    assert response.status_code == 404


# ---------------------------------------------------------------- delete guard


def test_a_model_in_use_cannot_be_deleted_and_the_roles_are_named(admin_client):
    model = _create(admin_client).get_json()['model']
    admin_client.post('/admin/llm-config/assignments/text', json={'model_id': model['id']})
    admin_client.post('/admin/llm-config/assignments/chat', json={'model_id': model['id']})

    response = admin_client.delete(f"/admin/llm-config/models/{model['id']}")
    body = response.get_json()
    assert response.status_code == 409
    assert body['code'] == 'llm_model_in_use'
    assert sorted(body['roles']) == ['chat', 'text']
    assert len(_document()['models']) == 1


def test_reassigning_both_roles_lets_the_model_be_deleted(admin_client):
    model = _create(admin_client).get_json()['model']
    admin_client.post('/admin/llm-config/assignments/text', json={'model_id': model['id']})
    admin_client.post('/admin/llm-config/assignments/chat', json={'model_id': model['id']})
    admin_client.delete('/admin/llm-config/assignments/text')
    admin_client.delete('/admin/llm-config/assignments/chat')

    response = admin_client.delete(f"/admin/llm-config/models/{model['id']}")
    assert response.status_code == 200
    assert _document()['models'] == []


# ---------------------------------------------------------------- assignments


def test_assigning_and_reverting_changes_the_effective_role(admin_client):
    model = _create(admin_client).get_json()['model']
    response = admin_client.post('/admin/llm-config/assignments/chat',
                                 json={'model_id': model['id']})
    body = response.get_json()
    assert response.status_code == 200
    assert body['assignments']['chat'] == model['id']
    assert body['roles']['chat']['source'] == 'model'
    assert body['roles']['chat']['profile']['id'] == model['id']

    reverted = admin_client.delete('/admin/llm-config/assignments/chat').get_json()
    assert reverted['assignments']['chat'] is None
    assert reverted['roles']['chat']['source'] == 'environment'
    assert reverted['roles']['chat']['uses_text_model'] is True


def test_a_null_model_id_reverts_the_role(admin_client):
    model = _create(admin_client).get_json()['model']
    admin_client.post('/admin/llm-config/assignments/translation', json={'model_id': model['id']})
    body = admin_client.post('/admin/llm-config/assignments/translation',
                             json={'model_id': None}).get_json()
    assert body['assignments']['translation'] is None


def test_assigning_an_unknown_model_is_a_404(admin_client):
    response = admin_client.post('/admin/llm-config/assignments/text',
                                 json={'model_id': 'm_missing'})
    assert response.status_code == 404
    assert response.get_json()['code'] == 'llm_model_not_found'


def test_an_unknown_role_is_a_400(admin_client):
    assert admin_client.post('/admin/llm-config/assignments/audio',
                             json={'model_id': None}).status_code == 400
    assert admin_client.delete('/admin/llm-config/assignments/audio').status_code == 400


def test_one_model_serves_several_roles(admin_client):
    model = _create(admin_client).get_json()['model']
    for role in ('text', 'chat', 'translation'):
        admin_client.post(f'/admin/llm-config/assignments/{role}',
                          json={'model_id': model['id']})
    body = admin_client.get('/admin/llm-config').get_json()
    assert set(body['assignments'].values()) == {model['id']}
    for role in ('text', 'chat', 'translation'):
        assert body['roles'][role]['source'] == 'model'


# ---------------------------------------------------------------- discovery and testing


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f'{self.status_code} error',
                request=httpx.Request('GET', 'http://provider/v1/models'),
                response=httpx.Response(self.status_code))


def _model_response(*models):
    return _Response({'data': [{'id': model, 'owned_by': 'test'} for model in models]})


def test_discovery_proxies_to_the_provider(admin_client, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _model_response('model-a', 'model-b')

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post('/admin/llm-config/test', json={
        'base_url': 'http://provider.lan/v1', 'api_key': 'provider-key'})

    body = response.get_json()
    assert response.status_code == 200, body
    assert [model['id'] for model in body['models']] == ['model-a', 'model-b']
    assert calls[0][0] == 'http://provider.lan/v1/models'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer provider-key'
    assert 'provider-key' not in json.dumps(body)


def test_discovery_reuses_the_saved_key_for_a_saved_model(admin_client, monkeypatch):
    model = _create(admin_client).get_json()['model']
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return _model_response('model-a')

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post(f"/admin/llm-config/models/{model['id']}/test", json={})
    assert response.status_code == 200
    assert seen['headers']['Authorization'] == 'Bearer stored-secret'


def test_discovery_failure_returns_a_clear_error(admin_client, monkeypatch):
    def fake_get(url, **kwargs):
        raise httpx.ConnectError('connection refused')

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post('/admin/llm-config/test', json={'base_url': 'http://down.lan/v1'})
    body = response.get_json()
    assert response.status_code == 502
    assert body['success'] is False
    assert 'Could not reach' in body['error']


def test_discovery_reports_an_http_error_status(admin_client, monkeypatch):
    monkeypatch.setattr(admin_api.httpx, 'get',
                        lambda url, **kwargs: _Response({'error': 'bad key'}, status_code=401))
    response = admin_client.post('/admin/llm-config/test', json={'base_url': 'http://provider.lan/v1'})
    body = response.get_json()
    assert response.status_code == 502
    assert body['success'] is False
    assert '401' in body['error']


def test_testing_a_saved_model_does_not_require_a_body(admin_client, monkeypatch):
    model = _create(admin_client).get_json()['model']
    monkeypatch.setattr(admin_api.httpx, 'get', lambda url, **kwargs: _model_response('model-a'))
    response = admin_client.post(f"/admin/llm-config/models/{model['id']}/test")
    assert response.status_code == 200
    assert response.get_json()['success'] is True


def test_an_invalid_base_url_is_rejected_before_any_request(admin_client, monkeypatch):
    monkeypatch.setattr(admin_api.httpx, 'get',
                        lambda *args, **kwargs: pytest.fail('the provider must not be called'))
    response = admin_client.post('/admin/llm-config/test', json={'base_url': 'file:///etc/passwd'})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_llm_config'


# ---------------------------------------------------------------- audit


def test_create_is_audited_with_the_id_and_field_names(admin_client):
    start = _max_audit_id()
    model = _create(admin_client).get_json()['model']

    rows = _audit_rows(start, 'admin.llm_model.created')
    assert len(rows) == 1
    details = rows[0].details
    assert details['model'] == model['id']
    assert set(details['fields']) == {'label', 'base_url', 'model_name', 'api_key'}
    dumped = json.dumps(details)
    assert 'stored-secret' not in dumped
    assert 'OpenRouter' not in dumped


def test_update_is_audited_with_only_the_changed_fields(admin_client):
    model = _create(admin_client).get_json()['model']
    start = _max_audit_id()
    admin_client.post(f"/admin/llm-config/models/{model['id']}", json={'model_name': 'other-model'})

    rows = _audit_rows(start, 'admin.llm_model.changed')
    assert len(rows) == 1
    assert rows[0].details == {'model': model['id'], 'fields': ['model_name']}


def test_delete_is_audited(admin_client):
    model = _create(admin_client).get_json()['model']
    start = _max_audit_id()
    admin_client.delete(f"/admin/llm-config/models/{model['id']}")

    rows = _audit_rows(start, 'admin.llm_model.deleted')
    assert len(rows) == 1
    assert rows[0].details == {'model': model['id']}


def test_assignment_and_revert_are_audited(admin_client):
    model = _create(admin_client).get_json()['model']
    start = _max_audit_id()
    admin_client.post('/admin/llm-config/assignments/chat', json={'model_id': model['id']})
    assigned = _audit_rows(start, 'admin.llm_role.assigned')
    assert len(assigned) == 1
    assert assigned[0].details == {'role': 'chat', 'model': model['id']}

    start = _max_audit_id()
    admin_client.delete('/admin/llm-config/assignments/chat')
    reverted = _audit_rows(start, 'admin.llm_role.reverted')
    assert len(reverted) == 1
    assert reverted[0].details == {'role': 'chat', 'old': model['id']}


def test_reverting_an_unassigned_role_writes_no_audit_row(admin_client):
    start = _max_audit_id()
    admin_client.delete('/admin/llm-config/assignments/text')
    assert _audit_rows(start, 'admin.llm_role.reverted') == []


# ---------------------------------------------------------------- generic settings


def test_the_generic_settings_list_hides_the_configuration_key(admin_client):
    _create(admin_client)
    keys = [row['key'] for row in admin_client.get('/admin/settings').get_json()]
    assert llm_config.SETTING_KEY not in keys


def test_the_generic_settings_endpoint_cannot_write_the_configuration_key(admin_client):
    response = admin_client.post('/admin/settings', json={
        'key': llm_config.SETTING_KEY,
        'value': '{"models": []}',
        'setting_type': 'string'})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'llm_config_managed_elsewhere'
    assert _document()['models'] == []
