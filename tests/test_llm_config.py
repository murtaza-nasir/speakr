"""Effective LLM configuration per role.

The text, chat and translation roles resolve their configuration from a saved
System Setting first, then from the environment. Empty saved fields fall back
to the environment default. Chat and translation fall back to the text model
when no dedicated model is configured. The API key is masked in every public
representation and an empty or masked value keeps the stored secret.
"""

import json
import os
import sys
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import SystemSetting
from src.services import llm_config

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def ctx():
    with app.app_context():
        _clear_saved_configs()
        llm_config.clear_client_cache()
        yield
        db.session.rollback()
        _clear_saved_configs()
        llm_config.clear_client_cache()


def _clear_saved_configs():
    for key in llm_config.SETTING_KEYS.values():
        SystemSetting.query.filter_by(key=key).delete()
    db.session.commit()


def _save(role, payload):
    SystemSetting.set_setting(
        llm_config.SETTING_KEYS[role],
        json.dumps(payload),
        setting_type='string',
    )
    db.session.commit()


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL_NAME', 'TEXT_MODEL_EXTRA_BODY',
        'CHAT_MODEL_API_KEY', 'CHAT_MODEL_BASE_URL', 'CHAT_MODEL_NAME', 'CHAT_MODEL_EXTRA_BODY',
        'TRANSLATION_MODEL_API_KEY', 'TRANSLATION_MODEL_BASE_URL', 'TRANSLATION_MODEL_NAME',
        'TRANSLATION_MODEL_EXTRA_BODY',
        'GPT5_FORCE_MODE', 'GPT5_REASONING_EFFORT', 'GPT5_VERBOSITY',
        'CHAT_GPT5_FORCE_MODE', 'CHAT_GPT5_REASONING_EFFORT', 'CHAT_GPT5_VERBOSITY',
        'TRANSLATION_GPT5_FORCE_MODE',
        'LLM_REQUEST_TIMEOUT', 'LLM_MAX_RETRIES', 'LLM_CONNECT_TIMEOUT', 'LLM_WRITE_TIMEOUT',
        'ENABLE_STREAM_OPTIONS',
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TEXT_MODEL_BASE_URL', 'http://llm.lan/v1')
    return monkeypatch


def _text_env(monkeypatch, *, key='text-key', base='http://llm.lan/v1', model='text-model'):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', key or '')
    monkeypatch.setenv('TEXT_MODEL_BASE_URL', base or '')
    monkeypatch.setenv('TEXT_MODEL_NAME', model or '')


# ---------------------------------------------------------------- precedence

def test_environment_configuration_applies_without_a_saved_value(ctx, clean_env):
    _text_env(clean_env)
    for role in llm_config.ROLES:
        config = llm_config.resolve_config(role, reveal_key=True)
        assert config['source'] == 'environment'
    text = llm_config.resolve_config('text', reveal_key=True)
    assert (text['base_url'], text['model_name'], text['api_key']) == (
        'http://llm.lan/v1', 'text-model', 'text-key')


def test_a_saved_configuration_takes_precedence(ctx, clean_env):
    _text_env(clean_env)
    _save('text', {'base_url': 'http://db.lan/v1', 'model_name': 'db-model', 'api_key': 'db-key'})

    config = llm_config.resolve_config('text', reveal_key=True)
    assert config['source'] == 'database'
    assert (config['base_url'], config['model_name'], config['api_key']) == (
        'http://db.lan/v1', 'db-model', 'db-key')


def test_empty_saved_fields_fall_back_to_the_environment(ctx, clean_env):
    _text_env(clean_env, model='env-model', base='http://env.lan/v1')
    _save('text', {'model_name': 'db-model', 'base_url': '   ', 'api_key': ''})

    config = llm_config.resolve_config('text', reveal_key=True)
    assert config['model_name'] == 'db-model'
    assert config['base_url'] == 'http://env.lan/v1'
    assert config['api_key'] == 'text-key'


def test_a_saved_change_applies_without_a_restart(ctx, clean_env):
    _text_env(clean_env)
    assert llm_config.resolve_config('text', reveal_key=True)['model_name'] == 'text-model'
    _save('text', {'model_name': 'first-model'})
    assert llm_config.resolve_config('text', reveal_key=True)['model_name'] == 'first-model'
    _save('text', {'model_name': 'second-model'})
    assert llm_config.resolve_config('text', reveal_key=True)['model_name'] == 'second-model'


# ---------------------------------------------------------------- masking

def test_public_configuration_masks_the_api_key(ctx, clean_env):
    _text_env(clean_env, key='secret-key')
    public = llm_config.public_config('text')
    assert public['api_key'] == llm_config.MASKED_API_KEY
    assert public['api_key'] != 'secret-key'
    assert llm_config.resolve_config('text')['api_key'] == llm_config.MASKED_API_KEY


@pytest.mark.parametrize('incoming', ['', '   ', llm_config.MASKED_API_KEY])
def test_an_empty_or_masked_key_keeps_the_stored_secret(ctx, clean_env, incoming):
    _text_env(clean_env)
    _save('text', {'model_name': 'db-model', 'api_key': 'stored-secret'})

    llm_config.save_config('text', {'model_name': 'db-model', 'api_key': incoming})

    assert llm_config.resolve_config('text', reveal_key=True)['api_key'] == 'stored-secret'
    assert llm_config.public_config('text')['api_key'] == llm_config.MASKED_API_KEY


def test_a_new_key_replaces_the_stored_secret(ctx, clean_env):
    _text_env(clean_env)
    _save('text', {'api_key': 'old-secret'})
    llm_config.save_config('text', {'api_key': 'new-secret'})
    assert llm_config.resolve_config('text', reveal_key=True)['api_key'] == 'new-secret'


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize('raw', ['{not json}', '["low"]', '"low"', 42])
def test_extra_body_must_be_a_json_object(ctx, clean_env, raw):
    with pytest.raises(llm_config.LLMConfigError) as error:
        llm_config.save_config('text', {'extra_body': raw})
    assert 'JSON object' in str(error.value) or 'valid JSON' in str(error.value)
    assert SystemSetting.query.filter_by(key=llm_config.SETTING_KEYS['text']).first() is None


def test_extra_body_is_saved_as_an_object_and_used(ctx, clean_env):
    _text_env(clean_env)
    llm_config.save_config('text', {'extra_body': '{"reasoning_effort": "low"}'})
    assert llm_config.resolve_config('text')['extra_body'] == {'reasoning_effort': 'low'}


def test_unknown_fields_are_rejected(ctx, clean_env):
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.save_config('text', {'modle_name': 'typo'})


# ---------------------------------------------------------------- clients

class _FakeOpenAI:
    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeOpenAI.instances.append(self)


@pytest.fixture
def fake_openai(monkeypatch):
    _FakeOpenAI.instances = []
    monkeypatch.setattr(llm_config, 'OpenAI', _FakeOpenAI)
    return _FakeOpenAI


def test_the_client_is_reused_while_its_signature_is_unchanged(ctx, clean_env, fake_openai):
    _text_env(clean_env)
    first = llm_config.get_client('text')
    second = llm_config.get_client('text')
    assert first is second
    assert len(fake_openai.instances) == 1


@pytest.mark.parametrize('name,value', [
    ('TEXT_MODEL_BASE_URL', 'http://other.lan/v1'),
    ('TEXT_MODEL_API_KEY', 'another-key'),
    ('LLM_REQUEST_TIMEOUT', '120'),
    ('LLM_MAX_RETRIES', '5'),
    ('LLM_CONNECT_TIMEOUT', '5'),
    ('LLM_WRITE_TIMEOUT', '5'),
    ('ENABLE_STREAM_OPTIONS', 'false'),
])
def test_a_changed_connection_setting_rebuilds_the_client(ctx, clean_env, fake_openai, name, value):
    _text_env(clean_env)
    first = llm_config.get_client('text')
    clean_env.setenv(name, value)
    second = llm_config.get_client('text')
    assert first is not second
    assert len(fake_openai.instances) == 2


def test_an_unconfigured_role_has_no_client(ctx, clean_env, fake_openai):
    clean_env.setenv('TEXT_MODEL_API_KEY', '')
    clean_env.setenv('TEXT_MODEL_BASE_URL', '')
    assert llm_config.get_client('text') is None
    assert fake_openai.instances == []


# ---------------------------------------------------------------- configured state

def test_text_is_configured_by_a_key_or_an_explicit_base_url(ctx, clean_env):
    clean_env.setenv('TEXT_MODEL_BASE_URL', '')
    clean_env.setenv('TEXT_MODEL_API_KEY', '')
    assert llm_config.is_configured('text') is False

    clean_env.setenv('TEXT_MODEL_BASE_URL', 'http://local.lan/v1')
    assert llm_config.is_configured('text') is True

    clean_env.setenv('TEXT_MODEL_BASE_URL', '')
    clean_env.setenv('TEXT_MODEL_API_KEY', 'key')
    assert llm_config.is_configured('text') is True


def test_a_dedicated_chat_model_needs_a_name_and_a_key_or_base_url(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    assert llm_config.resolve_config('chat', reveal_key=True)['model_name'] == 'text-model'

    clean_env.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1')
    chat = llm_config.resolve_config('chat', reveal_key=True)
    assert chat['model_name'] == 'chat-model'
    assert chat['base_url'] == 'http://chat.lan/v1'
    assert chat['api_key'] is None


def test_chat_and_translation_fall_back_to_the_saved_text_model(ctx, clean_env):
    _text_env(clean_env)
    _save('text', {'model_name': 'db-text', 'api_key': 'db-key', 'base_url': 'http://db.lan/v1'})

    for role in ('chat', 'translation'):
        config = llm_config.resolve_config(role, reveal_key=True)
        assert config['model_name'] == 'db-text'
        assert config['uses_text_model'] is True


def test_a_saved_dedicated_chat_configuration_is_used(ctx, clean_env):
    _text_env(clean_env)
    _save('chat', {'model_name': 'db-chat', 'api_key': 'chat-key', 'base_url': 'http://chat.lan/v1'})

    chat = llm_config.resolve_config('chat', reveal_key=True)
    assert (chat['model_name'], chat['base_url'], chat['api_key']) == (
        'db-chat', 'http://chat.lan/v1', 'chat-key')
    assert chat['source'] == 'database'
    assert chat['uses_text_model'] is False


def test_ambrose_and_inquire_reuse_the_chat_role(ctx, clean_env):
    _text_env(clean_env)
    _save('chat', {'model_name': 'db-chat', 'api_key': 'chat-key'})
    for alias in ('ambrose', 'inquire'):
        config = llm_config.resolve_config(alias, reveal_key=True)
        assert config['role'] == 'chat'
        assert config['model_name'] == 'db-chat'


# ---------------------------------------------------------------- gpt-5

def test_each_role_keeps_its_own_gpt5_force_setting(ctx, clean_env):
    _text_env(clean_env)
    _save('chat', {'model_name': 'db-chat', 'api_key': 'chat-key'})
    _save('translation', {'model_name': 'db-translation', 'api_key': 'translation-key'})
    clean_env.setenv('GPT5_FORCE_MODE', 'true')
    clean_env.setenv('CHAT_GPT5_FORCE_MODE', 'false')
    clean_env.setenv('TRANSLATION_GPT5_FORCE_MODE', 'true')

    assert llm_config.resolve_config('text')['gpt5_force'] is True
    assert llm_config.resolve_config('chat')['gpt5_force'] is False
    assert llm_config.resolve_config('translation')['gpt5_force'] is True


def test_a_saved_gpt5_setting_overrides_the_environment(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('GPT5_FORCE_MODE', 'true')
    _save('text', {'gpt5_force': False, 'gpt5_reasoning_effort': 'high'})

    text = llm_config.resolve_config('text')
    assert text['gpt5_force'] is False
    assert text['gpt5_reasoning_effort'] == 'high'


# ---------------------------------------------------------------- revert

def test_revert_returns_the_role_to_its_environment_configuration(ctx, clean_env):
    _text_env(clean_env)
    _save('text', {'model_name': 'db-model'})
    llm_config.clear_config('text')
    config = llm_config.resolve_config('text', reveal_key=True)
    assert config['source'] == 'environment'
    assert config['model_name'] == 'text-model'
    assert SystemSetting.query.filter_by(key=llm_config.SETTING_KEYS['text']).first() is None
