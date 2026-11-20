"""Admin API for the LLM configuration.

An administrator reads, saves and reverts the configuration of each role.
Model discovery and connection testing are proxied through the backend and
work on form values that were never saved. Secrets never leave the process
unmasked and are never written to the audit log.
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
        yield admin, plain
        for key in llm_config.SETTING_KEYS.values():
            SystemSetting.query.filter_by(key=key).delete()
        db.session.commit()
        db.session.delete(db.session.get(User, admin.id))
        db.session.delete(db.session.get(User, plain.id))
        db.session.commit()


@pytest.fixture
def admin_client(users, monkeypatch):
    admin, _ = users
    monkeypatch.delenv('CHAT_MODEL_NAME', raising=False)
    monkeypatch.delenv('CHAT_MODEL_BASE_URL', raising=False)
    monkeypatch.delenv('CHAT_MODEL_API_KEY', raising=False)
    llm_config.clear_client_cache()
    return _client(admin)


def _max_audit_id():
    rows = AuditEvent.query.order_by(AuditEvent.id.desc()).first()
    return rows.id if rows else 0


def _audit_rows(start):
    db.session.expire_all()
    return AuditEvent.query.filter(AuditEvent.id > start).order_by(AuditEvent.id).all()


def _saved(role):
    return SystemSetting.query.filter_by(key=llm_config.SETTING_KEYS[role]).first()


# ---------------------------------------------------------------- authorization

def test_non_admins_are_rejected(users, monkeypatch):
    _, plain = users
    client = _client(plain)
    assert client.get('/admin/llm-config').status_code == 403
    assert client.post('/admin/llm-config/text', json={'model_name': 'x'}).status_code == 403
    assert client.delete('/admin/llm-config/text').status_code == 403
    assert client.post('/admin/llm-config/text/test', json={}).status_code == 403
    assert client.post('/admin/llm-models', json={'role': 'text'}).status_code == 403


# ---------------------------------------------------------------- read and save

def test_get_returns_each_role_with_a_masked_key(admin_client, monkeypatch):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'env-secret')
    response = admin_client.get('/admin/llm-config')
    assert response.status_code == 200
    roles = response.get_json()['roles']
    assert sorted(roles) == sorted(llm_config.ROLES)
    assert roles['text']['api_key'] == llm_config.MASKED_API_KEY
    assert roles['text']['source'] == 'environment'


def test_save_applies_to_the_resolver_and_masks_the_key(admin_client):
    response = admin_client.post('/admin/llm-config/text', json={
        'base_url': 'http://db.lan/v1',
        'model_name': 'db-model',
        'api_key': 'stored-secret',
        'extra_body': '{"reasoning_effort": "low"}',
    })
    body = response.get_json()
    assert response.status_code == 200, body
    assert body['config']['api_key'] == llm_config.MASKED_API_KEY
    assert 'stored-secret' not in json.dumps(body)

    resolved = llm_config.resolve_config('text', reveal_key=True)
    assert (resolved['base_url'], resolved['model_name'], resolved['api_key']) == (
        'http://db.lan/v1', 'db-model', 'stored-secret')
    assert resolved['extra_body'] == {'reasoning_effort': 'low'}
    assert resolved['source'] == 'database'


@pytest.mark.parametrize('incoming', ['', llm_config.MASKED_API_KEY])
def test_an_empty_or_masked_key_keeps_the_stored_secret(admin_client, incoming):
    admin_client.post('/admin/llm-config/text', json={'api_key': 'stored-secret'})
    response = admin_client.post('/admin/llm-config/text', json={'api_key': incoming})
    assert response.status_code == 200
    assert llm_config.resolve_config('text', reveal_key=True)['api_key'] == 'stored-secret'


@pytest.mark.parametrize('payload', [
    {'extra_body': '{not json}'},
    {'extra_body': '["low"]'},
    {'modle_name': 'typo'},
    {'gpt5_force': 'sometimes'},
])
def test_invalid_input_is_rejected_and_nothing_is_saved(admin_client, payload):
    response = admin_client.post('/admin/llm-config/text', json=payload)
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_llm_config'
    assert _saved('text') is None


def test_an_unknown_role_is_rejected(admin_client):
    assert admin_client.post('/admin/llm-config/audio', json={}).status_code == 400
    assert admin_client.get('/admin/llm-config/audio').status_code == 400


def test_revert_returns_the_role_to_the_environment(admin_client):
    admin_client.post('/admin/llm-config/text', json={'model_name': 'db-model'})
    response = admin_client.delete('/admin/llm-config/text')
    assert response.status_code == 200
    assert response.get_json()['config']['source'] == 'environment'
    assert _saved('text') is None


# ---------------------------------------------------------------- discovery and tests

class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f'{self.status_code} error', request=httpx.Request('GET', 'http://provider/v1/models'),
                response=httpx.Response(self.status_code))


def _model_response(*models):
    return _Response({'data': [{'id': model, 'owned_by': 'test'} for model in models]})


def test_discovery_proxies_to_the_provider(admin_client, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _model_response('model-a', 'model-b')

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post('/admin/llm-models', json={
        'role': 'text', 'base_url': 'http://provider.lan/v1', 'api_key': 'provider-key'})

    body = response.get_json()
    assert response.status_code == 200, body
    assert [model['id'] for model in body['models']] == ['model-a', 'model-b']
    assert calls[0][0] == 'http://provider.lan/v1/models'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer provider-key'
    assert 'provider-key' not in json.dumps(body)


def test_discovery_reuses_the_saved_key_when_the_form_masks_it(admin_client, monkeypatch):
    admin_client.post('/admin/llm-config/text', json={'base_url': 'http://saved.lan/v1', 'api_key': 'saved-key'})
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return _model_response('model-a')

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post('/admin/llm-models', json={
        'role': 'text', 'base_url': 'http://saved.lan/v1', 'api_key': llm_config.MASKED_API_KEY})
    assert response.status_code == 200
    assert seen['headers']['Authorization'] == 'Bearer saved-key'


def test_discovery_failure_returns_a_clear_error(admin_client, monkeypatch):
    def fake_get(url, **kwargs):
        raise httpx.ConnectError('connection refused')

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post('/admin/llm-models', json={'role': 'text', 'base_url': 'http://down.lan/v1'})
    body = response.get_json()
    assert response.status_code == 502
    assert body['success'] is False
    assert 'Could not reach' in body['error']


def test_connection_test_works_without_a_prior_save(admin_client, monkeypatch):
    monkeypatch.setattr(admin_api.httpx, 'get', lambda url, **kwargs: _model_response('model-a'))
    response = admin_client.post('/admin/llm-config/text/test', json={
        'base_url': 'http://provider.lan/v1', 'api_key': 'key'})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['success'] is True
    assert _saved('text') is None


def test_connection_test_reports_a_failure(admin_client, monkeypatch):
    def fake_get(url, **kwargs):
        return _Response({'error': 'bad key'}, status_code=401)

    monkeypatch.setattr(admin_api.httpx, 'get', fake_get)
    response = admin_client.post('/admin/llm-config/text/test', json={'base_url': 'http://provider.lan/v1'})
    body = response.get_json()
    assert response.status_code == 502
    assert body['success'] is False
    assert '401' in body['error']


def test_a_non_http_base_url_is_rejected(admin_client):
    response = admin_client.post('/admin/llm-models', json={'role': 'text', 'base_url': 'file:///etc/passwd'})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_llm_config'


# ---------------------------------------------------------------- audit

def test_the_audit_log_records_the_role_and_changed_fields(admin_client):
    start = _max_audit_id()
    admin_client.post('/admin/llm-config/text', json={
        'model_name': 'db-model', 'api_key': 'audit-secret',
        'extra_body': {'reasoning_effort': 'low'}})

    rows = [row for row in _audit_rows(start) if row.event == 'admin.llm_config.changed']
    assert len(rows) == 1
    details = rows[0].details
    assert details['role'] == 'text'
    assert set(details['fields']) == {'api_key', 'model_name', 'extra_body'}
    dumped = json.dumps(details)
    assert 'audit-secret' not in dumped
    assert 'reasoning_effort' not in dumped


def test_the_audit_log_records_a_revert(admin_client):
    admin_client.post('/admin/llm-config/chat', json={'model_name': 'db-chat', 'api_key': 'chat-secret'})
    start = _max_audit_id()
    admin_client.delete('/admin/llm-config/chat')

    rows = [row for row in _audit_rows(start) if row.event == 'admin.llm_config.changed']
    assert len(rows) == 1
    details = rows[0].details
    assert details['role'] == 'chat'
    assert 'chat-secret' not in json.dumps(details)


# ---------------------------------------------------------------- generic settings

def test_the_generic_settings_list_hides_the_configuration_keys(admin_client):
    admin_client.post('/admin/llm-config/text', json={'model_name': 'db-model'})
    keys = [row['key'] for row in admin_client.get('/admin/settings').get_json()]
    assert llm_config.SETTING_KEYS['text'] not in keys


def test_the_generic_settings_endpoint_cannot_write_the_configuration_keys(admin_client):
    response = admin_client.post('/admin/settings', json={
        'key': llm_config.SETTING_KEYS['text'], 'value': '{"model_name": "x"}', 'setting_type': 'string'})
    assert response.status_code == 400
    assert _saved('text') is None
