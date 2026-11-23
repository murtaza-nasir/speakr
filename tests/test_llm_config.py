"""Configured LLM models and the effective configuration of each role.

One saved document holds the model profiles and the role assignments. A role
assigned to a profile uses it; a role without an assignment uses its
environment configuration, and chat/translation inherit the text role when
they have no dedicated environment configuration of their own. The API key is
write-only: every representation masks it, the model list included, and an
empty or masked key keeps the stored secret.
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

ENV_NAMES = (
    'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL_NAME', 'TEXT_MODEL_EXTRA_BODY',
    'CHAT_MODEL_API_KEY', 'CHAT_MODEL_BASE_URL', 'CHAT_MODEL_NAME', 'CHAT_MODEL_EXTRA_BODY',
    'TRANSLATION_MODEL_API_KEY', 'TRANSLATION_MODEL_BASE_URL', 'TRANSLATION_MODEL_NAME',
    'TRANSLATION_MODEL_EXTRA_BODY',
    'GPT5_FORCE_MODE', 'GPT5_REASONING_EFFORT', 'GPT5_VERBOSITY',
    'CHAT_GPT5_FORCE_MODE', 'CHAT_GPT5_REASONING_EFFORT', 'CHAT_GPT5_VERBOSITY',
    'TRANSLATION_GPT5_FORCE_MODE', 'TRANSLATION_GPT5_REASONING_EFFORT',
    'TRANSLATION_GPT5_VERBOSITY',
    'LLM_REQUEST_TIMEOUT', 'LLM_MAX_RETRIES', 'LLM_CONNECT_TIMEOUT', 'LLM_WRITE_TIMEOUT',
    'ENABLE_STREAM_OPTIONS',
)


@pytest.fixture
def ctx():
    with app.app_context():
        _clear_document()
        llm_config.clear_client_cache()
        yield
        db.session.rollback()
        _clear_document()
        llm_config.clear_client_cache()


def _clear_document():
    SystemSetting.query.filter_by(key=llm_config.SETTING_KEY).delete()
    db.session.commit()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _text_env(monkeypatch, *, key='text-key', base='http://llm.lan/v1', model='text-model'):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', key or '')
    monkeypatch.setenv('TEXT_MODEL_BASE_URL', base or '')
    monkeypatch.setenv('TEXT_MODEL_NAME', model or '')


def _write_document(document):
    SystemSetting.set_setting(llm_config.SETTING_KEY, json.dumps(document))
    db.session.commit()
    llm_config.clear_client_cache()


def _profile(model_id='m_00000001', **overrides):
    profile = {
        'id': model_id,
        'label': f'Model {model_id}',
        'base_url': 'http://profile.lan/v1',
        'model_name': f'model-{model_id}',
        'api_key': 'profile-key',
        'extra_body': {},
        'gpt5_force': False,
        'gpt5_reasoning_effort': 'medium',
        'gpt5_verbosity': 'medium',
    }
    profile.update(overrides)
    return profile


def _document(models=(), assignments=None):
    return {
        'version': 1,
        'models': list(models),
        'assignments': {role: None for role in llm_config.ROLES} | (assignments or {}),
    }


# ---------------------------------------------------------------- environment


def test_with_no_saved_document_each_role_uses_the_environment(ctx, clean_env):
    _text_env(clean_env)

    for role in llm_config.ROLES:
        config = llm_config.resolve_config(role, reveal_key=True)
        assert config['source'] == 'environment'
        assert config['profile'] is None
        assert (config['base_url'], config['model_name'], config['api_key']) == (
            'http://llm.lan/v1', 'text-model', 'text-key')

    assert llm_config.resolve_config('chat')['uses_text_model'] is True
    assert llm_config.resolve_config('translation')['uses_text_model'] is True


def test_a_dedicated_chat_environment_configuration_is_used(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    clean_env.setenv('CHAT_MODEL_BASE_URL', 'http://chat.lan/v1')

    chat = llm_config.resolve_config('chat', reveal_key=True)
    assert (chat['model_name'], chat['base_url']) == ('chat-model', 'http://chat.lan/v1')
    assert chat['uses_text_model'] is False
    assert chat['source'] == 'environment'


def test_a_dedicated_translation_environment_configuration_is_used(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('TRANSLATION_MODEL_NAME', 'translation-model')
    clean_env.setenv('TRANSLATION_MODEL_API_KEY', 'translation-key')

    translation = llm_config.resolve_config('translation', reveal_key=True)
    assert translation['model_name'] == 'translation-model'
    assert translation['api_key'] == 'translation-key'
    assert translation['uses_text_model'] is False


def test_per_role_gpt5_settings_are_kept(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('GPT5_FORCE_MODE', 'true')
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    clean_env.setenv('CHAT_MODEL_API_KEY', 'chat-key')
    clean_env.setenv('CHAT_GPT5_FORCE_MODE', 'false')
    clean_env.setenv('TRANSLATION_GPT5_FORCE_MODE', 'true')
    clean_env.setenv('TRANSLATION_MODEL_NAME', 'translation-model')
    clean_env.setenv('TRANSLATION_MODEL_API_KEY', 'translation-key')

    assert llm_config.resolve_config('text')['gpt5_force'] is True
    assert llm_config.resolve_config('chat')['gpt5_force'] is False
    assert llm_config.resolve_config('translation')['gpt5_force'] is True


# ---------------------------------------------------------------- assigned profiles


def test_an_assigned_profile_is_used(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile(label='Saved model', base_url='http://db.lan/v1',
                       model_name='db-model', api_key='db-key')
    _write_document(_document([profile], {'text': profile['id']}))

    config = llm_config.resolve_config('text', reveal_key=True)
    assert config['source'] == 'model'
    assert config['profile'] == {'id': profile['id'], 'label': 'Saved model'}
    assert (config['base_url'], config['model_name'], config['api_key']) == (
        'http://db.lan/v1', 'db-model', 'db-key')
    assert config['configured'] is True
    assert config['uses_text_model'] is False


def test_the_same_profile_serves_several_roles(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile()
    _write_document(_document([profile], {'text': profile['id'], 'chat': profile['id']}))

    for role in ('text', 'chat'):
        config = llm_config.resolve_config(role)
        assert config['source'] == 'model'
        assert config['profile']['id'] == profile['id']
        assert config['uses_text_model'] is False


def test_an_unassigned_role_keeps_the_environment_when_another_is_assigned(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile()
    _write_document(_document([profile], {'text': profile['id']}))

    chat = llm_config.resolve_config('chat')
    assert chat['source'] == 'model'
    assert chat['uses_text_model'] is True
    assert chat['profile']['id'] == profile['id']

    translation = llm_config.resolve_config('translation')
    assert translation['uses_text_model'] is True


def test_chat_uses_its_environment_even_when_text_is_assigned(ctx, clean_env):
    _text_env(clean_env)
    clean_env.setenv('CHAT_MODEL_NAME', 'chat-model')
    clean_env.setenv('CHAT_MODEL_API_KEY', 'chat-key')
    profile = _profile()
    _write_document(_document([profile], {'text': profile['id']}))

    chat = llm_config.resolve_config('chat', reveal_key=True)
    assert chat['source'] == 'environment'
    assert chat['uses_text_model'] is False
    assert chat['model_name'] == 'chat-model'


def test_ambrose_and_inquire_reuse_the_chat_role(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile()
    _write_document(_document([profile], {'chat': profile['id']}))

    for alias in ('ambrose', 'inquire'):
        config = llm_config.resolve_config(alias)
        assert config['role'] == 'chat'
        assert config['profile']['id'] == profile['id']


# ---------------------------------------------------------------- masking


def test_public_models_mask_the_api_key(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'OpenRouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'model_name': 'openai/gpt-5',
        'api_key': 'secret-key',
    })
    assert created.model['api_key'] == llm_config.MASKED_API_KEY
    assert 'secret-key' not in json.dumps(created.model)
    assert llm_config.list_models()[0]['api_key'] == llm_config.MASKED_API_KEY


def test_no_model_representation_carries_the_stored_key(ctx, clean_env):
    """Keys are write-only (#661); only a connection test reads the stored key."""
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'OpenRouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'model_name': 'openai/gpt-5',
        'api_key': 'secret-key',
    })
    model_id = created.model['id']
    assert created.model['api_key'] == llm_config.MASKED_API_KEY
    assert llm_config.list_models()[0]['api_key'] == llm_config.MASKED_API_KEY
    assert llm_config.get_model(model_id)['api_key'] == llm_config.MASKED_API_KEY
    assert 'secret-key' not in json.dumps([llm_config.list_models(), llm_config.get_model(model_id)])
    assert llm_config.saved_connection(model_id) == ('https://openrouter.ai/api/v1', 'secret-key')


def test_resolve_masks_the_key_unless_it_is_revealed(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile(api_key='secret-key')
    _write_document(_document([profile], {'text': profile['id']}))

    assert llm_config.resolve_config('text')['api_key'] == llm_config.MASKED_API_KEY
    assert llm_config.resolve_config('text', reveal_key=True)['api_key'] == 'secret-key'


def test_a_profile_without_a_key_reports_an_empty_public_key(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile(api_key='')
    _write_document(_document([profile], {'text': profile['id']}))

    assert llm_config.resolve_config('text')['api_key'] == ''
    assert llm_config.is_configured('text') is True


@pytest.mark.parametrize('incoming', ['', '   ', llm_config.MASKED_API_KEY, None])
def test_an_empty_or_masked_key_keeps_the_stored_secret(ctx, clean_env, incoming):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'stored-secret',
    })
    values = {} if incoming is None else {'api_key': incoming}
    llm_config.update_model(created.model['id'], values)

    assert llm_config.list_models()[0]['api_key'] == llm_config.MASKED_API_KEY
    assert llm_config._find_model(llm_config.load_document(), created.model['id'])[1]['api_key'] == 'stored-secret'


def test_a_new_key_replaces_the_stored_secret(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'old-secret',
    })
    llm_config.update_model(created.model['id'], {'api_key': 'new-secret'})

    assert llm_config._find_model(llm_config.load_document(), created.model['id'])[1]['api_key'] == 'new-secret'


def test_clear_api_key_removes_the_secret(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'stored-secret',
    })
    llm_config.update_model(created.model['id'], {'clear_api_key': True})

    assert llm_config.list_models()[0]['api_key'] == ''
    assert llm_config.resolve_config('text')['configured'] is True


def test_a_new_base_url_clears_the_stored_key_when_no_new_key_is_sent(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'stored-secret',
    })
    change = llm_config.update_model(created.model['id'], {'base_url': 'http://other.lan/v1'})

    stored = llm_config._find_model(llm_config.load_document(), created.model['id'])[1]
    assert (stored['base_url'], stored['api_key']) == ('http://other.lan/v1', '')
    assert 'api_key' in change.changed_fields


def test_a_new_base_url_clears_the_key_the_form_echoes(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'stored-secret',
    })
    llm_config.update_model(created.model['id'], {
        'base_url': 'http://other.lan/v1', 'api_key': 'stored-secret'})

    stored = llm_config._find_model(llm_config.load_document(), created.model['id'])[1]
    assert (stored['base_url'], stored['api_key']) == ('http://other.lan/v1', '')


def test_new_key_is_empty_for_empty_masked_or_echoed_values(ctx, clean_env):
    assert llm_config.new_key('', 'stored-secret') == ''
    assert llm_config.new_key(None, 'stored-secret') == ''
    assert llm_config.new_key(llm_config.MASKED_API_KEY, 'stored-secret') == ''
    assert llm_config.new_key(' stored-secret ', 'stored-secret') == ''
    assert llm_config.new_key('other-secret', 'stored-secret') == 'other-secret'
    assert llm_config.new_key('other-secret', '') == 'other-secret'


def test_a_new_base_url_with_a_new_key_replaces_the_stored_key(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'stored-secret',
    })
    llm_config.update_model(created.model['id'], {
        'base_url': 'http://other.lan/v1', 'api_key': 'other-secret'})

    stored = llm_config._find_model(llm_config.load_document(), created.model['id'])[1]
    assert (stored['base_url'], stored['api_key']) == (
        'http://other.lan/v1', 'other-secret')


def test_a_new_base_url_can_clear_the_stored_key(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'Saved', 'base_url': 'http://profile.lan/v1',
        'model_name': 'saved-model', 'api_key': 'stored-secret',
    })
    llm_config.update_model(created.model['id'], {
        'base_url': 'http://other.lan/v1', 'clear_api_key': True})

    stored = llm_config._find_model(llm_config.load_document(), created.model['id'])[1]
    assert (stored['base_url'], stored['api_key']) == ('http://other.lan/v1', '')


def test_at_most_twelve_models_can_be_configured(ctx, clean_env):
    _text_env(clean_env)
    for index in range(llm_config.MAX_MODELS):
        llm_config.create_model({
            'label': f'Model {index}', 'base_url': f'http://m{index}.lan/v1',
            'model_name': f'model-{index}'})

    with pytest.raises(llm_config.LLMModelLimitReached) as error:
        llm_config.create_model({
            'label': 'Too many', 'base_url': 'http://extra.lan/v1',
            'model_name': 'extra'})
    assert error.value.limit == llm_config.MAX_MODELS
    assert len(llm_config.list_models()) == llm_config.MAX_MODELS

    first = llm_config.list_models()[0]
    llm_config.delete_model(first['id'])
    created = llm_config.create_model({
        'label': 'Replacement', 'base_url': 'http://extra.lan/v1',
        'model_name': 'extra'})
    assert created.model['label'] == 'Replacement'


@pytest.mark.parametrize('incoming', [{}, ['key'], 42, True])
def test_an_api_key_that_is_not_a_string_is_rejected(ctx, clean_env, incoming):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMConfigError, match='api_key'):
        llm_config.create_model({
            'label': 'x', 'base_url': 'http://a.lan/v1',
            'model_name': 'm', 'api_key': incoming})
    assert llm_config.load_document()['models'] == []


def test_a_base_url_with_credentials_is_rejected_without_echoing_them(ctx, clean_env):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMConfigError) as error:
        llm_config.create_model({
            'label': 'x', 'base_url': 'http://user:topsecret@a.lan/v1',
            'model_name': 'm'})

    assert 'topsecret' not in str(error.value)
    assert llm_config.load_document()['models'] == []


# ---------------------------------------------------------------- validation


def test_a_profile_requires_a_label_a_model_name_and_a_base_url(ctx, clean_env):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({'base_url': 'http://x.lan/v1', 'model_name': 'm'})
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({'label': 'x', 'base_url': 'http://x.lan/v1'})
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({'label': 'x', 'model_name': 'm'})
    assert llm_config.load_document()['models'] == []


@pytest.mark.parametrize('base_url', ['file:///etc/passwd', 'ftp://x.lan/v1', 'x.lan/v1', ''])
def test_the_base_url_must_be_http_or_https(ctx, clean_env, base_url):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({'label': 'x', 'model_name': 'm', 'base_url': base_url})


def test_labels_are_unique_without_case(ctx, clean_env):
    _text_env(clean_env)
    llm_config.create_model({'label': 'OpenRouter', 'base_url': 'http://a.lan/v1', 'model_name': 'a'})
    with pytest.raises(llm_config.LLMModelLabelTaken):
        llm_config.create_model({'label': 'openrouter', 'base_url': 'http://b.lan/v1', 'model_name': 'b'})
    assert len(llm_config.load_document()['models']) == 1


def test_a_label_can_be_kept_on_update(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'OpenRouter', 'base_url': 'http://a.lan/v1', 'model_name': 'a'})
    updated = llm_config.update_model(created.model['id'], {'label': 'OpenRouter', 'model_name': 'b'})
    assert updated.model['label'] == 'OpenRouter'
    assert updated.model['model_name'] == 'b'


@pytest.mark.parametrize('raw', ['{not json}', '["low"]', '"low"', 42, True])
def test_extra_body_must_be_a_json_object(ctx, clean_env, raw):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({
            'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'extra_body': raw})
    assert llm_config.load_document()['models'] == []


def test_extra_body_is_saved_as_an_object_and_used(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm',
        'extra_body': '{"reasoning_effort": "low"}',
    })
    assert created.model['extra_body'] == {'reasoning_effort': 'low'}
    llm_config.assign_model('text', created.model['id'])
    assert llm_config.resolve_config('text')['extra_body'] == {'reasoning_effort': 'low'}


def test_an_unknown_field_is_rejected(ctx, clean_env):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({
            'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'modle_name': 'typo'})
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.create_model({'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm',
                                 'id': 'm_mine'})


def test_gpt5_force_accepts_booleans_and_true_false_strings(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'gpt5_force': 'true'})
    assert created.model['gpt5_force'] is True
    updated = llm_config.update_model(created.model['id'], {'gpt5_force': 'false'})
    assert updated.model['gpt5_force'] is False
    with pytest.raises(llm_config.LLMConfigError):
        llm_config.update_model(created.model['id'], {'gpt5_force': 'sometimes'})


def test_ids_are_generated_and_do_not_change(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm'})
    model_id = created.model['id']
    assert model_id.startswith('m_')
    assert len(model_id) == 10
    updated = llm_config.update_model(model_id, {'model_name': 'other'})
    assert updated.model['id'] == model_id


# ---------------------------------------------------------------- assignments and deletion


def test_assigning_and_reverting_a_profile(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm'})
    change = llm_config.assign_model('text', created.model['id'])
    assert (change.role, change.new) == ('text', created.model['id'])
    assert llm_config.resolve_config('text')['source'] == 'model'

    reverted = llm_config.clear_assignment('text')
    assert reverted.old == created.model['id']
    assert llm_config.resolve_config('text')['source'] == 'environment'


def test_assigning_an_unknown_model_is_rejected(ctx, clean_env):
    _text_env(clean_env)
    with pytest.raises(llm_config.LLMModelNotFound):
        llm_config.assign_model('text', 'm_missing')
    assert llm_config.load_document()['assignments']['text'] is None


def test_a_profile_in_use_cannot_be_deleted(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm'})
    llm_config.assign_model('text', created.model['id'])
    llm_config.assign_model('chat', created.model['id'])

    with pytest.raises(llm_config.LLMModelInUse) as error:
        llm_config.delete_model(created.model['id'])
    assert error.value.roles == ('chat', 'text')
    assert len(llm_config.load_document()['models']) == 1


def test_an_unassigned_profile_can_be_deleted(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm'})
    removed = llm_config.delete_model(created.model['id'])
    assert removed['id'] == created.model['id']
    assert llm_config.load_document()['models'] == []
    with pytest.raises(llm_config.LLMModelNotFound):
        llm_config.delete_model(created.model['id'])


def test_reassigning_frees_the_old_profile_for_deletion(ctx, clean_env):
    _text_env(clean_env)
    first = llm_config.create_model({'label': 'a', 'base_url': 'http://a.lan/v1', 'model_name': 'a'})
    second = llm_config.create_model({'label': 'b', 'base_url': 'http://b.lan/v1', 'model_name': 'b'})
    llm_config.assign_model('text', first.model['id'])
    llm_config.assign_model('text', second.model['id'])
    llm_config.delete_model(first.model['id'])
    assert [model['id'] for model in llm_config.list_models()] == [second.model['id']]


# ---------------------------------------------------------------- stored state


def test_a_dangling_assignment_is_ignored(ctx, clean_env):
    _text_env(clean_env)
    _write_document(_document([], {'text': 'm_missing'}))

    config = llm_config.resolve_config('text', reveal_key=True)
    assert config['source'] == 'environment'
    assert config['api_key'] == 'text-key'


def test_a_malformed_document_falls_back_to_the_environment(ctx, clean_env):
    _text_env(clean_env)
    SystemSetting.set_setting(llm_config.SETTING_KEY, '{not json')
    db.session.commit()

    config = llm_config.resolve_config('text', reveal_key=True)
    assert config['source'] == 'environment'
    assert config['model_name'] == 'text-model'


def test_an_invalid_stored_extra_body_is_ignored(ctx, clean_env):
    _text_env(clean_env)
    profile = _profile(extra_body=['not', 'an', 'object'])
    _write_document(_document([profile], {'text': profile['id']}))

    assert llm_config.resolve_config('text')['extra_body'] == {}


def test_a_profile_change_applies_without_a_restart(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'first'})
    llm_config.assign_model('text', created.model['id'])
    assert llm_config.resolve_config('text')['model_name'] == 'first'

    llm_config.update_model(created.model['id'], {'model_name': 'second'})
    assert llm_config.resolve_config('text')['model_name'] == 'second'


def test_the_document_holds_exactly_one_configuration_key(ctx, clean_env):
    assert llm_config.SETTING_KEY == 'llm_config_json'
    assert not hasattr(llm_config, 'SETTING_KEYS')


# ---------------------------------------------------------------- changes


def test_create_reports_the_fields_it_stored(ctx, clean_env):
    _text_env(clean_env)
    change = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'api_key': 'k'})
    assert set(change.changed_fields) == {'label', 'base_url', 'model_name', 'api_key'}


def test_update_reports_only_the_changed_fields(ctx, clean_env):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm'})
    change = llm_config.update_model(created.model['id'], {
        'model_name': 'other', 'base_url': 'http://a.lan/v1'})
    assert change.changed_fields == ('model_name',)


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


def test_a_profile_change_rebuilds_the_client(ctx, clean_env, fake_openai):
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'api_key': 'k'})
    llm_config.assign_model('text', created.model['id'])
    first = llm_config.get_client('text')

    llm_config.update_model(created.model['id'],
                            {'base_url': 'http://b.lan/v1', 'api_key': 'k2'})
    second = llm_config.get_client('text')
    assert first is not second


def test_an_unconfigured_role_has_no_client(ctx, clean_env, fake_openai):
    clean_env.setenv('TEXT_MODEL_API_KEY', '')
    clean_env.setenv('TEXT_MODEL_BASE_URL', '')
    assert llm_config.get_client('text') is None
    assert fake_openai.instances == []


def test_text_is_configured_by_a_key_or_an_explicit_base_url(ctx, clean_env):
    clean_env.setenv('TEXT_MODEL_API_KEY', '')
    clean_env.setenv('TEXT_MODEL_BASE_URL', '')
    assert llm_config.is_configured('text') is False

    clean_env.setenv('TEXT_MODEL_BASE_URL', 'http://local.lan/v1')
    assert llm_config.is_configured('text') is True

    clean_env.setenv('TEXT_MODEL_BASE_URL', '')
    clean_env.setenv('TEXT_MODEL_API_KEY', 'key')
    assert llm_config.is_configured('text') is True


# ---------------------------------------------------------------- per-request cache


def _setting_reads():
    """The SELECT statements on system_setting run while the block is open."""
    from sqlalchemy import event

    statements = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'system_setting' in statement.lower():
            statements.append(statement)

    class _Reads:
        def __enter__(self):
            event.listen(db.engine, 'before_cursor_execute', listener)
            return statements

        def __exit__(self, *exc):
            event.remove(db.engine, 'before_cursor_execute', listener)

    return _Reads()


def test_one_request_reads_the_configuration_once(ctx, clean_env, fake_openai):
    """Resolving the roles again and again in one app context does not query
    the database again (#661)."""
    _text_env(clean_env)
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm', 'api_key': 'k'})
    llm_config.assign_model('text', created.model['id'])
    db.session.commit()
    from flask import g
    g.pop('_system_setting_cache', None)
    g.pop('_llm_config_document', None)

    with _setting_reads() as reads:
        for _ in range(5):
            for role in llm_config.ROLES:
                llm_config.resolve_config(role)
            llm_config.get_client('text')
            llm_config.runtime_settings()

    # The document once, and each runtime setting once.
    assert len(reads) == 1 + len(llm_config.RUNTIME_SETTINGS), reads


def test_a_change_in_the_same_request_is_read_at_once(ctx, clean_env):
    _text_env(clean_env)
    assert llm_config.resolve_config('text')['model_name'] == 'text-model'
    created = llm_config.create_model({
        'label': 'x', 'base_url': 'http://a.lan/v1', 'model_name': 'm'})
    llm_config.assign_model('text', created.model['id'])
    assert llm_config.resolve_config('text')['model_name'] == 'm'


def test_the_document_is_a_copy(ctx, clean_env):
    """A caller that changes the returned document does not change the cache."""
    llm_config.load_document()['models'].append({'id': 'm_x'})
    assert llm_config.load_document()['models'] == []


def test_outside_an_app_context_the_environment_applies_with_a_warning(clean_env, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(llm_config, '_warned_outside_app_context', False)
    with caplog.at_level(logging.WARNING, logger=llm_config.logger.name):
        assert llm_config.load_document() == llm_config._empty_document()
        llm_config.load_document()
    warnings = [r for r in caplog.records if 'outside an app context' in r.getMessage()]
    assert len(warnings) == 1
