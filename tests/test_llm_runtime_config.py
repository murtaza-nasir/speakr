"""Request paths use the model and settings in effect, not import-time values.

A role assigned to a configured model sends that model, its Extra Body and its
GPT-5 options. An unassigned role keeps the environment configuration. A
change made through the configuration API applies to the next call without a
restart, and token tracking records the model that was actually used.
"""

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.config.app_config import initialize_config
from src.models import SystemSetting
from src.services import llm, llm_config
from src.services.token_tracking import token_tracker

app.config["WTF_CSRF_ENABLED"] = False

ENV_NAMES = (
    'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL_NAME', 'TEXT_MODEL_EXTRA_BODY',
    'CHAT_MODEL_API_KEY', 'CHAT_MODEL_BASE_URL', 'CHAT_MODEL_NAME', 'CHAT_MODEL_EXTRA_BODY',
    'TRANSLATION_MODEL_API_KEY', 'TRANSLATION_MODEL_BASE_URL', 'TRANSLATION_MODEL_NAME',
    'TRANSLATION_MODEL_EXTRA_BODY',
    'GPT5_FORCE_MODE', 'GPT5_REASONING_EFFORT', 'GPT5_VERBOSITY',
    'CHAT_GPT5_FORCE_MODE', 'CHAT_GPT5_REASONING_EFFORT', 'CHAT_GPT5_VERBOSITY',
    'TRANSLATION_GPT5_FORCE_MODE', 'TRANSLATION_GPT5_REASONING_EFFORT',
    'TRANSLATION_GPT5_VERBOSITY',
    'ENABLE_STREAM_OPTIONS',
)


class _FakeClient:
    def __init__(self, response=None):
        self.create = MagicMock(return_value=response or _completion())
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))


def _completion(content='ok', usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason='stop')],
        usage=usage,
    )


@pytest.fixture
def ctx(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'env-key')
    monkeypatch.setenv('TEXT_MODEL_BASE_URL', 'http://env.lan/v1')
    monkeypatch.setenv('TEXT_MODEL_NAME', 'env-text')
    with app.app_context():
        _clear()
        yield monkeypatch
        db.session.rollback()
        _clear()
        SystemSetting.query.filter_by(key='enable_stream_options').delete()
        db.session.commit()


def _clear():
    SystemSetting.query.filter_by(key=llm_config.SETTING_KEY).delete()
    db.session.commit()
    llm_config.clear_client_cache()


def _profile(role, **overrides):
    values = {
        'label': f'{role} profile',
        'base_url': f'http://{role}.lan/v1',
        'model_name': f'{role}-model',
        'api_key': f'{role}-key',
        'extra_body': {'reasoning_effort': 'low'},
    }
    values.update(overrides)
    model = llm_config.create_model(values).model
    llm_config.assign_model(role, model['id'])
    return model


def _messages():
    return [{"role": "user", "content": "q"}]


# ---------------------------------------------------------------- text


def test_an_assigned_profile_is_sent_by_call_llm_completion(ctx):
    _profile('text', model_name='db-model', extra_body={'reasoning': {'effort': 'low'}},
             gpt5_force=True, gpt5_reasoning_effort='high', gpt5_verbosity='low')
    client = _FakeClient()
    ctx.setattr(llm, 'client', client)

    llm.call_llm_completion(_messages(), max_tokens=100)

    kwargs = client.create.call_args.kwargs
    assert kwargs['model'] == 'db-model'
    assert kwargs['extra_body'] == {'reasoning': {'effort': 'low'}}
    assert kwargs['reasoning_effort'] == 'high'
    assert kwargs['verbosity'] == 'low'
    assert kwargs['max_completion_tokens'] == 100
    assert 'temperature' not in kwargs


def test_a_profile_gpt5_force_false_beats_the_environment(ctx):
    ctx.setenv('GPT5_FORCE_MODE', 'true')
    _profile('text', model_name='profile-model', extra_body={})
    client = _FakeClient()
    ctx.setattr(llm, 'client', client)

    llm.call_llm_completion(_messages(), max_tokens=100)

    kwargs = client.create.call_args.kwargs
    assert kwargs['temperature'] == 0.7
    assert 'reasoning_effort' not in kwargs
    assert 'verbosity' not in kwargs
    assert 'max_completion_tokens' not in kwargs


def test_a_profile_change_applies_to_the_next_call(ctx):
    model = _profile('text', model_name='first-model')
    client = _FakeClient()
    ctx.setattr(llm, 'client', client)

    llm.call_llm_completion(_messages())
    assert client.create.call_args.kwargs['model'] == 'first-model'

    llm_config.update_model(model['id'], {'model_name': 'second-model'})
    llm.call_llm_completion(_messages())
    assert client.create.call_args.kwargs['model'] == 'second-model'


def test_an_unassigned_text_role_keeps_the_environment(ctx):
    client = _FakeClient()
    ctx.setattr(llm, 'client', client)
    llm.call_llm_completion(_messages())
    assert client.create.call_args.kwargs['model'] == 'env-text'


def test_reverting_the_assignment_returns_to_the_environment(ctx):
    _profile('text', model_name='db-model')
    client = _FakeClient()
    ctx.setattr(llm, 'client', client)

    llm_config.clear_assignment('text')
    llm.call_llm_completion(_messages())
    assert client.create.call_args.kwargs['model'] == 'env-text'


# ---------------------------------------------------------------- chat and translation


def test_chat_uses_the_profile_assigned_to_it(ctx):
    _profile('chat', model_name='db-chat', extra_body={'chat_template_kwargs': {'enable_thinking': False}})
    client = _FakeClient()
    ctx.setattr(llm, 'chat_client', client)

    llm.call_chat_completion(_messages())
    kwargs = client.create.call_args.kwargs
    assert kwargs['model'] == 'db-chat'
    assert kwargs['extra_body'] == {'chat_template_kwargs': {'enable_thinking': False}}


def test_chat_without_an_assignment_inherits_the_text_profile(ctx):
    _profile('text', model_name='shared-text-model')
    client = _FakeClient()
    ctx.setattr(llm, 'chat_client', client)

    llm.call_chat_completion(_messages())
    assert client.create.call_args.kwargs['model'] == 'shared-text-model'


def test_chat_inheriting_the_text_model_names_the_text_extra_body(ctx):
    assert llm.get_chat_config()['extra_body_variable'] == 'TEXT_MODEL_EXTRA_BODY'


def test_a_dedicated_chat_environment_names_its_own_extra_body(ctx):
    ctx.setenv('CHAT_MODEL_NAME', 'chat-model')
    ctx.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1')
    assert llm.get_chat_config()['extra_body_variable'] == 'CHAT_MODEL_EXTRA_BODY'


def test_translation_inheriting_the_text_model_names_the_text_extra_body(ctx):
    assert llm.get_translation_config()['extra_body_variable'] == 'TEXT_MODEL_EXTRA_BODY'


def test_a_dedicated_translation_environment_names_its_own_extra_body(ctx):
    ctx.setenv('TRANSLATION_MODEL_NAME', 'translation-model')
    ctx.setenv('TRANSLATION_MODEL_BASE_URL', 'http://translation.lan/v1')
    assert llm.get_translation_config()['extra_body_variable'] == 'TRANSLATION_MODEL_EXTRA_BODY'


def test_translation_uses_the_profile_assigned_to_it(ctx):
    _profile('translation', model_name='db-translation', gpt5_force=True,
             gpt5_reasoning_effort='medium', gpt5_verbosity='medium')
    client = _FakeClient()
    ctx.setattr(llm, 'translation_client', client)

    llm.call_translation_completion(_messages())
    kwargs = client.create.call_args.kwargs
    assert kwargs['model'] == 'db-translation'
    assert kwargs['reasoning_effort'] == 'medium'
    assert 'temperature' not in kwargs


# ---------------------------------------------------------------- settings and tracking


def test_a_hot_streaming_setting_applies_to_the_next_call(ctx):
    client = _FakeClient(iter([]))
    ctx.setattr(llm, 'client', client)
    SystemSetting.set_setting('enable_stream_options', 'false')

    llm.call_llm_completion(_messages(), stream=True)
    assert 'stream_options' not in client.create.call_args.kwargs

    SystemSetting.set_setting('enable_stream_options', 'true')
    llm.call_llm_completion(_messages(), stream=True)
    assert client.create.call_args.kwargs['stream_options'] == {'include_usage': True}


def test_token_tracking_records_the_resolved_model(ctx):
    _profile('text', model_name='tracked-model')
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=None)
    client = _FakeClient(_completion(usage=usage))
    ctx.setattr(llm, 'client', client)

    with patch.object(token_tracker, 'check_budget', return_value=(True, 0, None)), \
         patch.object(token_tracker, 'record_usage') as record:
        llm.call_llm_completion(_messages(), user_id=1, operation_type='summarization')

    assert record.call_args.kwargs['model_name'] == 'tracked-model'


def test_the_dynamic_configured_flag_follows_the_assignment(ctx):
    assert llm.TEXT_MODEL_CONFIGURED is True  # the environment is configured

    _clear()
    SystemSetting.query.filter_by(key=llm_config.SETTING_KEY).delete()
    db.session.commit()
    ctx.delenv('TEXT_MODEL_API_KEY')
    ctx.delenv('TEXT_MODEL_BASE_URL')
    assert llm.TEXT_MODEL_CONFIGURED is False

    _profile('text', model_name='db-model', api_key='db-key')
    assert llm.TEXT_MODEL_CONFIGURED is True


# ---------------------------------------------------------------- startup


def test_startup_reads_the_saved_configuration_without_a_request(monkeypatch, caplog):
    """initialize_config runs at import time, outside an app context."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    with app.app_context():
        _clear()
        model = llm_config.create_model({
            'label': 'startup profile',
            'base_url': 'http://startup.lan/v1',
            'model_name': 'startup-model',
            'api_key': 'startup-key',
        }).model
        llm_config.assign_model('text', model['id'])
        db.session.commit()

    try:
        with caplog.at_level(logging.INFO):
            initialize_config(app)
    finally:
        with app.app_context():
            _clear()

    messages = [record.getMessage() for record in caplog.records]
    assert 'LLM configured: http://startup.lan/v1 / startup-model' in messages
    assert not any(message.startswith('Failed to read the LLM configuration')
                   for message in messages)
