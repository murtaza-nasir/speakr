"""The resolver guarantees that a regression would quietly undo.

A role dedicated its own server keeps its own Extra Body and never borrows the
text one, the text key travels only to the text server, environment base URLs
lose their trailing comment, an assignment is validated instead of reverted,
and an assignment made in System Settings reaches the next request's
availability gate and its token row without a restart.
"""

import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import SystemSetting, User
from src.services import llm_config

app.config['WTF_CSRF_ENABLED'] = False

ENV_NAMES = (
    'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL_NAME', 'TEXT_MODEL_EXTRA_BODY',
    'CHAT_MODEL_API_KEY', 'CHAT_MODEL_BASE_URL', 'CHAT_MODEL_NAME', 'CHAT_MODEL_EXTRA_BODY',
    'TRANSLATION_MODEL_API_KEY', 'TRANSLATION_MODEL_BASE_URL', 'TRANSLATION_MODEL_NAME',
    'TRANSLATION_MODEL_EXTRA_BODY',
    'GPT5_FORCE_MODE', 'GPT5_REASONING_EFFORT', 'GPT5_VERBOSITY',
    'CHAT_GPT5_FORCE_MODE', 'CHAT_GPT5_REASONING_EFFORT', 'CHAT_GPT5_VERBOSITY',
    'TRANSLATION_GPT5_FORCE_MODE', 'TRANSLATION_GPT5_REASONING_EFFORT',
    'TRANSLATION_GPT5_VERBOSITY',
)


@pytest.fixture
def ctx():
    with app.app_context():
        _clear_document()
        yield
        db.session.rollback()
        _clear_document()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _clear_document():
    SystemSetting.query.filter_by(key=llm_config.SETTING_KEY).delete()
    db.session.commit()
    llm_config.clear_client_cache()


def _write_document(document):
    SystemSetting.set_setting(llm_config.SETTING_KEY, json.dumps(document))
    db.session.commit()
    llm_config.clear_client_cache()


def _text_env(monkeypatch):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'text-key')
    monkeypatch.setenv('TEXT_MODEL_BASE_URL', 'http://text.lan/v1')
    monkeypatch.setenv('TEXT_MODEL_NAME', 'text-model')


def _login_client(user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def admin_client(ctx):
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        admin = User(username=f'llmreg_{suffix}', email=f'llmreg_{suffix}@local.test',
                     password='x', is_admin=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    try:
        yield _login_client(SimpleNamespace(id=admin_id))
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, admin_id))
            db.session.commit()


# ---------------------------------------------------------------- environment


def test_an_environment_base_url_loses_a_trailing_comment(clean_env):
    """``TEXT_MODEL_BASE_URL=http://host/v1#note`` still means ``http://host/v1``."""
    clean_env.setenv('TEXT_MODEL_BASE_URL', 'http://text.lan/v1#local server')
    clean_env.setenv('TEXT_MODEL_NAME', 'text-model')
    clean_env.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1#cheap')
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    clean_env.setenv('TRANSLATION_MODEL_BASE_URL', 'http://tr.lan/v1#fast')
    clean_env.setenv('TRANSLATION_MODEL_NAME', 'tr-model')

    assert llm_config.resolve_config('text')['base_url'] == 'http://text.lan/v1'
    assert llm_config.resolve_config('chat')['base_url'] == 'http://chat.lan/v1'
    assert llm_config.resolve_config('translation')['base_url'] == 'http://tr.lan/v1'


def test_a_dedicated_role_does_not_inherit_the_text_extra_body(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('TEXT_MODEL_EXTRA_BODY', '{"reasoning_effort": "low"}')
    clean_env.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1')
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')

    text = llm_config.resolve_config('text')
    chat = llm_config.resolve_config('chat')

    assert text['extra_body'] == {'reasoning_effort': 'low'}
    assert chat['base_url'] == 'http://chat.lan/v1'
    assert chat['extra_body'] == {}
    assert chat['extra_body_error'] is None


def test_a_dedicated_role_keeps_only_its_own_extra_body(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('TEXT_MODEL_EXTRA_BODY', '{"reasoning_effort": "low"}')
    clean_env.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1')
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    clean_env.setenv('CHAT_MODEL_EXTRA_BODY',
                     '{"chat_template_kwargs": {"enable_thinking": false}}')

    chat = llm_config.resolve_config('chat')

    assert chat['extra_body'] == {'chat_template_kwargs': {'enable_thinking': False}}
    assert chat['extra_body_error'] is None


def test_a_role_without_its_own_configuration_inherits_text(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('TEXT_MODEL_EXTRA_BODY', '{"reasoning_effort": "low"}')

    chat = llm_config.resolve_config('chat', reveal_key=True)

    assert chat['uses_text_model'] is True
    assert chat['model_name'] == 'text-model'
    assert chat['base_url'] == 'http://text.lan/v1'
    assert chat['api_key'] == 'text-key'
    assert chat['extra_body'] == {'reasoning_effort': 'low'}


def test_the_text_key_reaches_only_the_text_server(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1')
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    clean_env.setenv('TRANSLATION_MODEL_NAME', 'tr-model')

    chat = llm_config.resolve_config('chat', reveal_key=True)
    translation = llm_config.resolve_config('translation', reveal_key=True)

    assert chat['api_key'] is None
    assert translation['base_url'] == 'http://text.lan/v1'
    assert translation['api_key'] == 'text-key'


# ---------------------------------------------------------------- assignments


@pytest.mark.parametrize('model_id', [5, True, '', '   '])
def test_an_assignment_that_is_not_a_model_id_is_rejected(admin_client, model_id):
    """A bad body is a 400, not a role silently reverted to the environment."""
    response = admin_client.post('/admin/llm-config/assignments/text',
                                 json={'model_id': model_id})

    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_llm_config'
    assert set(llm_config.load_document()['assignments'].values()) == {None}


def test_an_assignment_to_an_unknown_model_is_not_found(admin_client):
    response = admin_client.post('/admin/llm-config/assignments/text',
                                 json={'model_id': 'm_doesnotexist'})

    assert response.status_code == 404
    assert response.get_json()['code'] == 'llm_model_not_found'
    assert set(llm_config.load_document()['assignments'].values()) == {None}


def test_an_assignment_to_a_deleted_model_reads_as_unassigned(admin_client):
    """The API answers with what the resolver does, so the UI selects 'Environment'."""
    _write_document({'models': [], 'assignments': {'chat': 'm_deleted'}})

    data = admin_client.get('/admin/llm-config').get_json()

    assert data['assignments'].get('chat') is None
    assert data['roles']['chat']['source'] == 'environment'


# ---------------------------------------------------------------- hot apply


def _answer(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                 finish_reason='stop')],
        usage=None,
    )


def _stream_events():
    return iter([
        'data: ' + json.dumps({'delta': 'pong'}) + '\n\n',
        'data: ' + json.dumps({'end_of_stream': True}) + '\n\n',
    ])


def test_an_assignment_reaches_the_chat_gate_without_a_restart(ctx, clean_env, admin_client):
    """The gate and the token row read the assignment made after import."""
    with patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        refused = admin_client.post('/api/inquire/chat', json={'message': 'hi'})
    assert refused.status_code == 503

    model = llm_config.create_model({
        'label': 'Chat model',
        'base_url': 'http://chat.lan/v1',
        'model_name': 'chat-model',
        'api_key': 'chat-key',
    }).model
    llm_config.assign_model('chat', model['id'])

    with patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.call_llm_completion', return_value=_answer('DIRECT')), \
         patch('src.api.inquire.process_streaming_with_thinking',
               return_value=_stream_events()) as streaming:
        response = admin_client.post('/api/inquire/chat', json={'message': 'hi'})
        body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'pong' in body
    assert streaming.call_args.kwargs['model_name'] == 'chat-model'
