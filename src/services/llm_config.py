"""Configured LLM models and the effective configuration of each role.

Speakr has three LLM roles: ``text`` (summaries and every other text task),
``chat`` (recording chat and the Inquire/Ambrose assistant) and
``translation``. One document in the System Setting ``llm_config_json`` holds
the configured models ("profiles") and the assignment of each role to a
profile or to the environment.

A profile carries everything needed to talk to one model: base URL, API key,
model name, Extra Body and GPT-5 options. The same profile can serve several
roles. A role with no assignment uses its environment configuration exactly
as earlier releases did, and chat and translation inherit the text role when
they have no dedicated environment configuration, so an empty model list
changes nothing.

The API key is stored in clear text because the OpenAI client needs it. It is
write-only: every representation that leaves this module is masked with
``MASKED_API_KEY``, the administrator's list of models included. An empty or
masked key submitted for an update keeps the stored secret, a new value
replaces it, and an explicit ``clear_api_key`` removes it.

The changes write the document in the caller's transaction and do not commit,
so an admin route commits the change and its audit row together.
"""

import copy
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import OpenAI

from src.utils.system_settings import env_fallback

logger = logging.getLogger(__name__)

ROLES = ('text', 'chat', 'translation')
ROLE_ALIASES = {'ambrose': 'chat', 'inquire': 'chat'}
MASKED_API_KEY = '***'
SETTING_KEY = 'llm_config_json'
CONFIG_VERSION = 1
MAX_MODELS = 12
SETTING_DESCRIPTION = (
    'Configured language models and the role assignment. Managed by the '
    'Language Models section of System Settings.'
)

PROFILE_FIELDS = frozenset({
    'label',
    'base_url',
    'api_key',
    'model_name',
    'extra_body',
    'gpt5_force',
    'gpt5_reasoning_effort',
    'gpt5_verbosity',
})

DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'
DEFAULT_TEXT_MODEL = 'openai/gpt-3.5-turbo'
DEFAULT_GPT5_SETTING = 'medium'

APP_HEADERS = {
    'HTTP-Referer': 'https://github.com/murtaza-nasir/speakr',
    'X-Title': 'Speakr - AI Audio Transcription',
    'User-Agent': 'Speakr/1.0 (https://github.com/murtaza-nasir/speakr)',
}


class LLMConfigError(ValueError):
    """A submitted or stored LLM configuration is unusable.

    ``code`` and ``params`` name the server.apiError message the admin API
    answers with; the exception text is the English form for the log.
    """

    def __init__(self, message, code='invalid_llm_config', **params):
        super().__init__(message)
        self.code = code
        self.params = params if code != 'invalid_llm_config' else {'detail': message, **params}


class LLMModelNotFound(LLMConfigError):
    """No configured model has the given id."""

    def __init__(self, model_id):
        self.model_id = model_id
        super().__init__(f'No configured model with id {model_id!r}',
                         'llm_model_not_found', model=model_id)


class LLMModelLabelTaken(LLMConfigError):
    """Another configured model already uses the label."""

    def __init__(self, label):
        self.label = label
        super().__init__(f'A model labelled {label!r} already exists',
                         'llm_model_label_taken', label=label)


class LLMModelInUse(LLMConfigError):
    """A role is assigned to the model, so it cannot be deleted."""

    def __init__(self, model_id, roles):
        self.model_id = model_id
        self.roles = tuple(sorted(roles))
        super().__init__(
            f'Model {model_id!r} is assigned to {", ".join(self.roles)}; '
            'reassign those roles first.',
            'llm_model_in_use', roles=', '.join(self.roles))


class LLMModelLimitReached(LLMConfigError):
    """The document already holds the maximum number of models."""

    def __init__(self, limit):
        self.limit = limit
        super().__init__(
            f'At most {limit} models can be configured; delete one before '
            'adding another',
            'llm_model_limit_reached', limit=limit)


@dataclass(frozen=True)
class ModelChange:
    """A created or updated profile and the names of the changed fields."""

    model: dict
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentChange:
    """A role assignment and the profile it had before."""

    role: str
    old: str | None
    new: str | None


def normalize_role(role):
    """The canonical role name; Inquire and Ambrose reuse chat."""
    canonical = ROLE_ALIASES.get(role, role)
    if canonical not in ROLES:
        raise LLMConfigError(
            f"Unknown LLM role {role!r}; expected one of {', '.join(ROLES)}",
            'llm_unknown_role', role=role, roles=', '.join(ROLES))
    return canonical


# ---------------------------------------------------------------- values


def parse_extra_body(raw, source='Extra Body'):
    """A JSON object from a submitted Extra Body value."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise LLMConfigError(f'{source} is not valid JSON ({error})',
                                 'llm_extra_body_invalid') from error
    else:
        raise LLMConfigError(
            f'{source} must be a JSON object, such as {{"reasoning_effort": "low"}}',
            'llm_extra_body_invalid')
    if not isinstance(value, dict):
        raise LLMConfigError(
            f'{source} must be a JSON object, such as {{"reasoning_effort": "low"}}',
            'llm_extra_body_invalid')
    return value


def merge_api_key(incoming, existing):
    """The key to store: an empty or masked incoming value keeps the old one."""
    if incoming is None:
        return existing or ''
    if not isinstance(incoming, str):
        raise LLMConfigError('api_key must be a string', 'field_must_be_string', field='api_key')
    incoming = incoming.strip()
    if not incoming or incoming == MASKED_API_KEY:
        return existing or ''
    return incoming


def new_key(incoming, stored):
    """A key entered now: empty, masked and echoed values are not new keys."""
    key = merge_api_key(incoming, '')
    return '' if key == stored else key


# The longest values a profile accepts. The label is shown in lists and
# badges; the others only bound what one request can store.
MAX_LENGTHS = {
    'label': 80,
    'model_name': 200,
    'base_url': 500,
    'api_key': 4096,
    'gpt5_reasoning_effort': 40,
    'gpt5_verbosity': 40,
}


def _check_length(name, value):
    limit = MAX_LENGTHS.get(name)
    if limit is not None and len(value) > limit:
        raise LLMConfigError(f'{name} must be at most {limit} characters',
                             'field_too_long', field=name, max=limit)
    return value


def _required_text(values, name):
    value = values.get(name)
    if value is not None and not isinstance(value, str):
        raise LLMConfigError(f'{name} must be a string', 'field_must_be_string', field=name)
    if value is None or not value.strip():
        raise LLMConfigError(f'{name} is required', 'missing_required_field', field=name)
    return _check_length(name, value.strip())


def normalize_base_url(value):
    if value is not None and not isinstance(value, str):
        raise LLMConfigError('base_url must be a string', 'field_must_be_string', field='base_url')
    if value is None or not value.strip():
        raise LLMConfigError('base_url is required', 'missing_required_field', field='base_url')
    value = _check_length('base_url', value.strip().rstrip('/'))
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise LLMConfigError('base_url must start with http:// or https://',
                             'llm_base_url_scheme')
    if parts.username is not None or parts.password is not None:
        # Never echo the URL: it would put the credentials in the answer.
        raise LLMConfigError('base_url must not include credentials',
                             'llm_base_url_credentials')
    return value


def _gpt5_text(values, name):
    value = values.get(name)
    if value is None:
        return DEFAULT_GPT5_SETTING
    if not isinstance(value, str):
        raise LLMConfigError(f'{name} must be a string', 'field_must_be_string', field=name)
    if not value.strip():
        raise LLMConfigError(f'{name} is required', 'missing_required_field', field=name)
    return _check_length(name, value.strip())


def _bool_value(value, name, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise LLMConfigError(f'{name} must be true or false', 'invalid_boolean_flag', key=name)


def _unknown_fields(fields):
    return LLMConfigError(f"Unknown model field(s): {', '.join(fields)}",
                          'llm_unknown_fields', fields=', '.join(fields))


def _label_taken(models, label, exclude_id=None):
    key = label.casefold()
    return any(
        model['id'] != exclude_id and model.get('label', '').casefold() == key
        for model in models)


# ---------------------------------------------------------------- document


def _empty_document():
    return {
        'version': CONFIG_VERSION,
        'models': [],
        'assignments': {role: None for role in ROLES},
    }


def _stored_text(value):
    return value.strip() if isinstance(value, str) else ''


def _stored_extra_body(raw):
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            logger.warning('Ignoring a stored LLM Extra Body that is not valid JSON')
            return {}
        if isinstance(value, dict):
            return value
    if raw not in (None, '', {}):
        logger.warning('Ignoring a stored LLM Extra Body that is not a JSON object')
    return {}


def _stored_model(raw):
    if not isinstance(raw, dict):
        return None
    model_id = raw.get('id')
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    return {
        'id': model_id.strip(),
        'label': _stored_text(raw.get('label')),
        'base_url': _stored_text(raw.get('base_url')).rstrip('/'),
        'model_name': _stored_text(raw.get('model_name')),
        'api_key': _stored_text(raw.get('api_key')),
        'extra_body': _stored_extra_body(raw.get('extra_body')),
        'gpt5_force': raw.get('gpt5_force') if isinstance(raw.get('gpt5_force'), bool) else False,
        'gpt5_reasoning_effort': (
            _stored_text(raw.get('gpt5_reasoning_effort')) or DEFAULT_GPT5_SETTING),
        'gpt5_verbosity': (
            _stored_text(raw.get('gpt5_verbosity')) or DEFAULT_GPT5_SETTING),
    }


def _normalize_document(raw):
    document = _empty_document()
    if not isinstance(raw, Mapping):
        return document

    seen = set()
    for item in raw.get('models') or []:
        model = _stored_model(item)
        if model is None or model['id'] in seen:
            continue
        seen.add(model['id'])
        document['models'].append(model)

    assignments = raw.get('assignments')
    if isinstance(assignments, Mapping):
        for role in ROLES:
            value = assignments.get(role)
            if isinstance(value, str) and value.strip():
                document['assignments'][role] = value.strip()
    return document


# The document read for the current app context, with the System Setting
# cache generation it was read at: one LLM call resolves the configuration
# many times, and a write to system_setting starts a new generation.
_DOCUMENT_CACHE = '_llm_config_document'
_warned_outside_app_context = False


def _setting_generation():
    from src.models import system
    return system._SYSTEM_SETTING_CACHE_GENERATION


def _read_document():
    from src.models import SystemSetting
    try:
        row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
        raw = row.value if row is not None else None
    except Exception as error:  # noqa: BLE001 - an unreadable table: nothing saved
        logger.warning('Could not read the saved LLM configuration (%s); every role uses '
                       'its environment configuration', type(error).__name__)
        return _empty_document()
    if not raw:
        return _empty_document()
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning('Ignoring %s: the saved LLM configuration is not valid JSON', SETTING_KEY)
        return _empty_document()
    return _normalize_document(document)


def load_document():
    """The saved configuration document, normalized, or an empty one.

    The document is read once per app context (a request or a job) and again
    after any write to system_setting. Outside an app context nothing saved
    can be read: every role then uses its environment configuration, and a
    warning says so once per process.
    """
    global _warned_outside_app_context
    from flask import g, has_app_context

    if not has_app_context():
        if not _warned_outside_app_context:
            _warned_outside_app_context = True
            logger.warning('The LLM configuration was read outside an app context; the saved '
                           'models and assignments are not used there, only the environment')
        return _empty_document()
    generation = _setting_generation()
    cached = g.get(_DOCUMENT_CACHE)
    if cached is None or cached[0] != generation:
        cached = (generation, _read_document())
        setattr(g, _DOCUMENT_CACHE, cached)
    return copy.deepcopy(cached[1])


def _save_document(document):
    """Write the document in the current transaction; the caller commits."""
    from flask import g
    from src.database import db
    from src.models import SystemSetting, system

    value = json.dumps(document, sort_keys=True)
    row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None:
        db.session.add(SystemSetting(key=SETTING_KEY, value=value,
                                     description=SETTING_DESCRIPTION, setting_type='string'))
    else:
        row.value = value
        row.description = SETTING_DESCRIPTION
        row.updated_at = datetime.utcnow()
    db.session.flush()
    system._SYSTEM_SETTING_CACHE_GENERATION += 1
    g.pop(_DOCUMENT_CACHE, None)
    clear_client_cache()


def _find_model(document, model_id):
    for index, model in enumerate(document['models']):
        if model['id'] == model_id:
            return index, model
    raise LLMModelNotFound(model_id)


def _public_profile(profile):
    """A profile as it may leave this module: the API key is always masked."""
    result = dict(profile)
    result['extra_body'] = dict(profile.get('extra_body') or {})
    result['api_key'] = MASKED_API_KEY if profile.get('api_key') else ''
    return result


# ---------------------------------------------------------------- CRUD


def list_models():
    """The configured models, with their API keys masked."""
    return [_public_profile(model) for model in load_document()['models']]


def get_model(model_id):
    """One configured model, with its API key masked."""
    _, model = _find_model(load_document(), model_id)
    return _public_profile(model)


def saved_connection(model_id):
    """(base_url, api_key) of a saved model, for a connection test only.

    The only way to the stored key outside a client; the key goes to the
    provider and never into an answer or a log line.
    """
    _, model = _find_model(load_document(), model_id)
    return model['base_url'], model['api_key']


def create_model(values):
    """Validate and append a profile. Returns its public form."""
    if not isinstance(values, Mapping):
        raise LLMConfigError('The model configuration must be a JSON object',
                             'body_must_be_object')
    unknown = sorted(set(values) - PROFILE_FIELDS)
    if unknown:
        raise _unknown_fields(unknown)

    label = _required_text(values, 'label')
    model_name = _required_text(values, 'model_name')
    base_url = normalize_base_url(values.get('base_url'))
    api_key = _check_length('api_key', merge_api_key(values.get('api_key'), ''))

    document = load_document()
    if len(document['models']) >= MAX_MODELS:
        raise LLMModelLimitReached(MAX_MODELS)
    if _label_taken(document['models'], label):
        raise LLMModelLabelTaken(label)

    profile = {
        'id': f'm_{uuid.uuid4().hex[:8]}',
        'label': label,
        'base_url': base_url,
        'model_name': model_name,
        'api_key': api_key,
        'extra_body': parse_extra_body(values.get('extra_body')),
        'gpt5_force': _bool_value(values.get('gpt5_force'), 'gpt5_force', False),
        'gpt5_reasoning_effort': _gpt5_text(values, 'gpt5_reasoning_effort'),
        'gpt5_verbosity': _gpt5_text(values, 'gpt5_verbosity'),
    }
    document['models'].append(profile)
    _save_document(document)
    return ModelChange(_public_profile(profile), tuple(sorted(set(values) & PROFILE_FIELDS)))


def update_model(model_id, values):
    """Validate and change a profile. Omitted fields keep their value."""
    if not isinstance(values, Mapping):
        raise LLMConfigError('The model configuration must be a JSON object',
                             'body_must_be_object')
    unknown = sorted(set(values) - PROFILE_FIELDS - {'clear_api_key'})
    if unknown:
        raise _unknown_fields(unknown)
    clear_key = _bool_value(values.get('clear_api_key'), 'clear_api_key', False)

    document = load_document()
    index, existing = _find_model(document, model_id)
    updated = dict(existing)

    if 'label' in values:
        label = _required_text(values, 'label')
        if _label_taken(document['models'], label, exclude_id=model_id):
            raise LLMModelLabelTaken(label)
        updated['label'] = label
    if 'base_url' in values:
        updated['base_url'] = normalize_base_url(values.get('base_url'))
    if 'model_name' in values:
        updated['model_name'] = _required_text(values, 'model_name')
    base_url_changed = updated['base_url'] != existing['base_url']
    if clear_key:
        updated['api_key'] = ''
    elif base_url_changed:
        # A stored key belongs to the URL it was saved with: a new URL keeps it
        # only when the request enters a different key.
        updated['api_key'] = new_key(values.get('api_key'), existing['api_key'])
    elif 'api_key' in values:
        updated['api_key'] = merge_api_key(values.get('api_key'), existing['api_key'])
    _check_length('api_key', updated['api_key'])
    if 'extra_body' in values:
        updated['extra_body'] = parse_extra_body(values.get('extra_body'))
    if 'gpt5_force' in values:
        updated['gpt5_force'] = _bool_value(values.get('gpt5_force'), 'gpt5_force',
                                            existing['gpt5_force'])
    if 'gpt5_reasoning_effort' in values:
        updated['gpt5_reasoning_effort'] = _gpt5_text(values, 'gpt5_reasoning_effort')
    if 'gpt5_verbosity' in values:
        updated['gpt5_verbosity'] = _gpt5_text(values, 'gpt5_verbosity')

    changed = tuple(sorted(
        name for name in PROFILE_FIELDS if existing.get(name) != updated.get(name)))
    if changed:
        document['models'][index] = updated
        _save_document(document)
    return ModelChange(_public_profile(updated), changed)


def delete_model(model_id):
    """Delete an unassigned profile and return its public form."""
    document = load_document()
    index, existing = _find_model(document, model_id)
    roles = [role for role in ROLES if document['assignments'].get(role) == model_id]
    if roles:
        raise LLMModelInUse(model_id, roles)
    document['models'].pop(index)
    _save_document(document)
    return _public_profile(existing)


def assign_model(role, model_id):
    """Assign a profile to a role, or ``None`` to return it to the environment."""
    role = normalize_role(role)
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise LLMConfigError(
                'model_id must be the id of a configured model, or null to '
                'use the environment', 'llm_invalid_model_id')
        model_id = model_id.strip()

    document = load_document()
    if model_id is not None:
        _find_model(document, model_id)
    old = document['assignments'].get(role)
    if old != model_id:
        document['assignments'][role] = model_id
        _save_document(document)
    return AssignmentChange(role, old, model_id)


def clear_assignment(role):
    """Return a role to its environment configuration."""
    return assign_model(role, None)


# ---------------------------------------------------------------- resolution


def _env(name, default=None):
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _env_url(name, default=None):
    """A base URL from the environment, without a trailing ``#`` comment.

    Earlier releases allowed ``TEXT_MODEL_BASE_URL=http://host/v1#note`` and
    stripped everything from the ``#``; every other URL setting still does.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.split('#', 1)[0].strip()
    return value or default


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ('true', '1', 'yes')


def _environment_extra_body(env_name):
    raw = _env(env_name)
    if raw is None:
        return None, None
    try:
        return parse_extra_body(raw, env_name), None
    except LLMConfigError as error:
        logger.error('%s; requests with this configuration will fail until it is corrected.', error)
        return {}, str(error)


def _environment_text():
    extra_body, extra_body_error = _environment_extra_body('TEXT_MODEL_EXTRA_BODY')
    return {
        'source': 'environment',
        'profile': None,
        'api_key': _env('TEXT_MODEL_API_KEY'),
        'base_url': _env_url('TEXT_MODEL_BASE_URL', DEFAULT_BASE_URL),
        'base_url_explicit': bool(_env_url('TEXT_MODEL_BASE_URL')),
        'model_name': _env('TEXT_MODEL_NAME', DEFAULT_TEXT_MODEL),
        'extra_body': extra_body if extra_body is not None else {},
        'extra_body_error': extra_body_error,
        'gpt5_force': _env_bool('GPT5_FORCE_MODE'),
        'gpt5_reasoning_effort': _env('GPT5_REASONING_EFFORT', DEFAULT_GPT5_SETTING),
        'gpt5_verbosity': _env('GPT5_VERBOSITY', DEFAULT_GPT5_SETTING),
        'uses_text_model': False,
        'configured': bool(_env('TEXT_MODEL_API_KEY') or _env_url('TEXT_MODEL_BASE_URL')),
    }


def _environment_role(role, text_environment):
    """The role's own environment configuration, or None when it inherits text."""
    prefix = role.upper()
    base_url = _env_url(f'{prefix}_MODEL_BASE_URL')
    api_key = _env(f'{prefix}_MODEL_API_KEY')
    model_name = _env(f'{prefix}_MODEL_NAME')
    if not (model_name and (api_key or base_url)):
        return None

    extra_body, extra_body_error = _environment_extra_body(f'{prefix}_MODEL_EXTRA_BODY')
    effective_base_url = base_url or text_environment['base_url']
    return {
        'source': 'environment',
        'profile': None,
        # The text key goes only to the text model's server, exactly as the
        # per-role configuration did before profiles existed.
        'api_key': api_key or (
            text_environment['api_key']
            if effective_base_url == text_environment['base_url'] else None),
        'base_url': effective_base_url,
        'base_url_explicit': bool(base_url),
        'model_name': model_name,
        # A dedicated role does not inherit the text model's Extra Body: the
        # parameters may not suit a different model or server. Chat and
        # translation inherit the whole text configuration only when they
        # have no dedicated environment configuration at all.
        'extra_body': extra_body if extra_body is not None else {},
        'extra_body_error': extra_body_error,
        'gpt5_force': _env_bool(f'{prefix}_GPT5_FORCE_MODE'),
        'gpt5_reasoning_effort': (
            _env(f'{prefix}_GPT5_REASONING_EFFORT')
            or text_environment['gpt5_reasoning_effort']),
        'gpt5_verbosity': (
            _env(f'{prefix}_GPT5_VERBOSITY')
            or text_environment['gpt5_verbosity']),
        'uses_text_model': False,
        'configured': True,
    }


def _profile_config(role, profile):
    return {
        'source': 'model',
        'profile': {'id': profile['id'], 'label': profile['label']},
        'api_key': profile['api_key'] or None,
        'base_url': profile['base_url'],
        'base_url_explicit': bool(profile['base_url']),
        'model_name': profile['model_name'],
        'extra_body': dict(profile['extra_body']),
        'extra_body_error': None,
        'gpt5_force': profile['gpt5_force'],
        'gpt5_reasoning_effort': profile['gpt5_reasoning_effort'],
        'gpt5_verbosity': profile['gpt5_verbosity'],
        'uses_text_model': False,
        'configured': bool(profile['model_name'] and profile['base_url']),
    }


def _assigned_profile(document, role):
    model_id = document['assignments'].get(role)
    if not model_id:
        return None
    for model in document['models']:
        if model['id'] == model_id:
            return model
    logger.warning('Role %s is assigned to %s, which no longer exists', role, model_id)
    return None


def _text_config(document):
    profile = _assigned_profile(document, 'text')
    if profile is not None:
        return _profile_config('text', profile)
    return _environment_text()


def _resolve(role):
    role = normalize_role(role)
    document = load_document()

    profile = _assigned_profile(document, role)
    if profile is not None:
        config = _profile_config(role, profile)
    elif role == 'text':
        config = _environment_text()
    else:
        text_environment = _environment_text()
        config = _environment_role(role, text_environment)
        if config is None:
            config = dict(_text_config(document))
            config['uses_text_model'] = True

    config['role'] = role
    return config


def resolve_config(role, reveal_key=False):
    """The effective configuration of a role.

    The API key is masked unless ``reveal_key`` is true; only the request path
    that builds a client needs the real value.
    """
    config = _resolve(role)
    if not reveal_key:
        config['api_key'] = MASKED_API_KEY if config.get('api_key') else ''
    return config


def public_config(role):
    """The configuration as it may be shown to an administrator."""
    return resolve_config(role)


def is_configured(role):
    return bool(resolve_config(role)['configured'])


# ---------------------------------------------------------------- runtime settings

RUNTIME_SETTINGS = {
    'request_timeout': ('llm_request_timeout', 'LLM_REQUEST_TIMEOUT', 600),
    'max_retries': ('llm_max_retries', 'LLM_MAX_RETRIES', 2),
    'connect_timeout': ('llm_connect_timeout', 'LLM_CONNECT_TIMEOUT', 30.0),
    'write_timeout': ('llm_write_timeout', 'LLM_WRITE_TIMEOUT', 30.0),
    'enable_stream_options': ('enable_stream_options', 'ENABLE_STREAM_OPTIONS', True),
}


@dataclass(frozen=True)
class RuntimeSettings:
    request_timeout: int
    max_retries: int
    connect_timeout: float
    write_timeout: float
    enable_stream_options: bool


def _runtime_raw(setting_key, env_name, default):
    try:
        from flask import has_app_context
        if has_app_context():
            from src.models import SystemSetting
            # Read through the per-request System Setting cache.
            value = SystemSetting.get_setting(setting_key)
            if value not in (None, ''):
                return value
    except Exception as error:  # noqa: BLE001 - fall back to the environment on any read failure
        logger.debug('Could not read the runtime setting %s: %s', setting_key, error)
    return env_fallback(env_name, default)


def _as_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_bool(value, fallback):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in ('true', '1', 'yes')
    return fallback


def runtime_settings():
    """Timeouts, retries and streaming behavior in effect right now."""
    values = {
        name: _runtime_raw(setting_key, env_name, default)
        for name, (setting_key, env_name, default) in RUNTIME_SETTINGS.items()
    }
    return RuntimeSettings(
        request_timeout=_as_int(values['request_timeout'], 600),
        max_retries=_as_int(values['max_retries'], 2),
        connect_timeout=_as_float(values['connect_timeout'], 30.0),
        write_timeout=_as_float(values['write_timeout'], 30.0),
        enable_stream_options=_as_bool(values['enable_stream_options'], True),
    )


# ---------------------------------------------------------------- clients

@dataclass
class _CachedClient:
    signature: tuple
    client: Any


_clients = {}
_clients_lock = threading.Lock()
# The one HTTP client of the LLM clients, also used for embeddings and the
# transcription services (src.services.llm.http_client_no_proxy).
HTTP_CLIENT = httpx.Client(verify=True, headers=APP_HEADERS)


def client_signature(config, settings=None):
    """Every value that requires a new client when it changes."""
    settings = settings or runtime_settings()
    return (
        config.get('base_url'),
        config.get('api_key') or '',
        settings.request_timeout,
        settings.max_retries,
        settings.connect_timeout,
        settings.write_timeout,
        settings.enable_stream_options,
    )


def clear_client_cache(role=None):
    with _clients_lock:
        if role is None:
            _clients.clear()
        else:
            _clients.pop(normalize_role(role), None)


def get_client(role):
    """The OpenAI client for a role, rebuilt when its signature changes."""
    role = normalize_role(role)
    config = resolve_config(role, reveal_key=True)
    if not config['configured']:
        return None

    settings = runtime_settings()
    signature = client_signature(config, settings)
    if role != 'text':
        text_config = resolve_config('text', reveal_key=True)
        if text_config['configured'] and client_signature(text_config, settings) == signature:
            # Same connection: reuse the text client instead of a second one.
            return get_client('text')
    with _clients_lock:
        cached = _clients.get(role)
        if cached is not None and cached.signature == signature:
            return cached.client
        client = OpenAI(
            api_key=config['api_key'] or 'not-needed',
            base_url=config['base_url'],
            http_client=HTTP_CLIENT,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=float(settings.request_timeout),
                write=settings.write_timeout,
                pool=30.0,
            ),
            max_retries=settings.max_retries,
        )
        _clients[role] = _CachedClient(signature, client)
        return client
