"""The effective configuration of an LLM role.

Speakr uses three LLM roles: ``text`` (summaries and every other text task),
``chat`` (recording chat and the Inquire/Ambrose assistant) and
``translation``. A configuration saved in System Settings takes precedence
over the environment. An empty saved field falls back to the environment
default. Chat and translation fall back to the text model when no dedicated
model is configured.

The API key is stored in clear text because the OpenAI client needs it. Every
representation that leaves this module is masked with ``MASKED_API_KEY``; an
empty or masked key submitted for saving keeps the stored secret.

Clients are cached per role by a signature that covers every connection
setting, so a configuration change rebuilds the client on the next request.
Nothing is captured at import time: every read resolves the current values.
"""

import json
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from openai import OpenAI

from src.utils.system_settings import env_fallback

logger = logging.getLogger(__name__)

ROLES = ('text', 'chat', 'translation')
ROLE_ALIASES = {'ambrose': 'chat', 'inquire': 'chat'}
MASKED_API_KEY = '***'

SETTING_KEYS = {role: f'llm_{role}_config_json' for role in ROLES}
SETTING_DESCRIPTION = (
    'Saved LLM configuration for {role} requests. Managed by the LLM '
    'configuration section of System Settings.'
)

CONFIG_FIELDS = frozenset({
    'api_key',
    'base_url',
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
    """A submitted or stored LLM configuration is unusable."""


def normalize_role(role):
    """The canonical role name; Inquire and Ambrose reuse chat."""
    canonical = ROLE_ALIASES.get(role, role)
    if canonical not in ROLES:
        raise LLMConfigError(
            f"Unknown LLM role {role!r}; expected one of {', '.join(ROLES)}")
    return canonical


# ---------------------------------------------------------------- environment

def _env(name, default=None):
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ('true', '1', 'yes')


def parse_extra_body(raw, source='Extra Body'):
    """A JSON object from a submitted or stored Extra Body value."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError as e:
            raise LLMConfigError(f"{source} is not valid JSON ({e})") from e
    else:
        raise LLMConfigError(
            f'{source} must be a JSON object, such as {{"reasoning_effort": "low"}}')
    if not isinstance(value, dict):
        raise LLMConfigError(
            f'{source} must be a JSON object, such as {{"reasoning_effort": "low"}}')
    return value


def _environment_extra_body(env_name):
    raw = _env(env_name)
    if raw is None:
        return {}, None
    try:
        return parse_extra_body(raw, env_name), None
    except LLMConfigError as e:
        logger.error(f'{e}; requests with this configuration will fail until it is corrected.')
        return {}, str(e)


def _text_environment():
    base_url = _env('TEXT_MODEL_BASE_URL', DEFAULT_BASE_URL)
    extra_body, extra_body_error = _environment_extra_body('TEXT_MODEL_EXTRA_BODY')
    return {
        'api_key': _env('TEXT_MODEL_API_KEY'),
        'base_url': base_url,
        'base_url_explicit': bool(_env('TEXT_MODEL_BASE_URL')),
        'model_name': _env('TEXT_MODEL_NAME', DEFAULT_TEXT_MODEL),
        'extra_body': extra_body,
        'extra_body_error': extra_body_error,
        'gpt5_force': _env_bool('GPT5_FORCE_MODE'),
        'gpt5_reasoning_effort': _env('GPT5_REASONING_EFFORT', DEFAULT_GPT5_SETTING),
        'gpt5_verbosity': _env('GPT5_VERBOSITY', DEFAULT_GPT5_SETTING),
    }


def _role_environment(role, text_environment):
    """The role's own environment values, without the text fallback."""
    prefix = role.upper()
    extra_body, extra_body_error = _environment_extra_body(f'{prefix}_MODEL_EXTRA_BODY')
    return {
        'api_key': _env(f'{prefix}_MODEL_API_KEY'),
        'base_url': _env(f'{prefix}_MODEL_BASE_URL'),
        'base_url_explicit': bool(_env(f'{prefix}_MODEL_BASE_URL')),
        'model_name': _env(f'{prefix}_MODEL_NAME'),
        'extra_body': extra_body or text_environment['extra_body'],
        'extra_body_error': extra_body_error or text_environment['extra_body_error'],
        'gpt5_force': _env_bool(f'{prefix}_GPT5_FORCE_MODE'),
        'gpt5_reasoning_effort': (
            _env(f'{prefix}_GPT5_REASONING_EFFORT')
            or text_environment['gpt5_reasoning_effort']),
        'gpt5_verbosity': (
            _env(f'{prefix}_GPT5_VERBOSITY')
            or text_environment['gpt5_verbosity']),
    }


# ---------------------------------------------------------------- saved values

def _stored(role):
    """The saved configuration of a role, or an empty mapping."""
    try:
        from src.models import SystemSetting
        row = SystemSetting.query.filter_by(key=SETTING_KEYS[role]).first()
        raw = row.value if row is not None else None
    except Exception:  # noqa: BLE001 - a missing app context or database means "use the environment"
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            'Ignoring %s: the saved LLM configuration is not valid JSON',
            SETTING_KEYS[role])
        return {}
    return value if isinstance(value, dict) else {}


def _saved_text(stored, name):
    value = stored.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _saved_extra_body(stored):
    if 'extra_body' not in stored:
        return None, None
    try:
        return parse_extra_body(stored['extra_body']), None
    except LLMConfigError as e:
        logger.warning('Ignoring saved Extra Body for an LLM role: %s', e)
        return {}, str(e)


def _saved_bool(stored, name):
    value = stored.get(name)
    return value if isinstance(value, bool) else None


def _overlay_text(environment, stored):
    config = dict(environment)
    config.update(
        api_key=_saved_text(stored, 'api_key') or environment['api_key'],
        base_url=_saved_text(stored, 'base_url') or environment['base_url'],
        model_name=_saved_text(stored, 'model_name') or environment['model_name'],
        gpt5_reasoning_effort=(
            _saved_text(stored, 'gpt5_reasoning_effort')
            or environment['gpt5_reasoning_effort']),
        gpt5_verbosity=(
            _saved_text(stored, 'gpt5_verbosity')
            or environment['gpt5_verbosity']),
    )
    config['base_url_explicit'] = (
        bool(_saved_text(stored, 'base_url')) or environment['base_url_explicit'])
    saved_force = _saved_bool(stored, 'gpt5_force')
    if saved_force is not None:
        config['gpt5_force'] = saved_force
    extra_body, extra_body_error = _saved_extra_body(stored)
    if extra_body is not None:
        config['extra_body'] = extra_body
        config['extra_body_error'] = extra_body_error
    config['source'] = 'database' if stored else 'environment'
    return config


def _overlay_role(role, role_environment, stored):
    """Merge saved role values, then decide whether the role is dedicated."""
    own_base = _saved_text(stored, 'base_url') or role_environment['base_url']
    own_key = _saved_text(stored, 'api_key') or role_environment['api_key']
    model_name = _saved_text(stored, 'model_name') or role_environment['model_name']

    extra_body, extra_body_error = _saved_extra_body(stored)
    if extra_body is None:
        extra_body = role_environment['extra_body']
        extra_body_error = role_environment['extra_body_error']

    gpt5_force = _saved_bool(stored, 'gpt5_force')
    if gpt5_force is None:
        gpt5_force = role_environment['gpt5_force']

    return {
        'api_key': own_key,
        'base_url': own_base,
        'base_url_explicit': bool(own_base),
        'model_name': model_name,
        'extra_body': extra_body,
        'extra_body_error': extra_body_error,
        'gpt5_force': gpt5_force,
        'gpt5_reasoning_effort': (
            _saved_text(stored, 'gpt5_reasoning_effort')
            or role_environment['gpt5_reasoning_effort']),
        'gpt5_verbosity': (
            _saved_text(stored, 'gpt5_verbosity')
            or role_environment['gpt5_verbosity']),
        'dedicated': bool(model_name) and bool(own_base or own_key),
        'source': 'database' if stored else 'environment',
    }


def _text_configured(config):
    return bool(config['api_key']) or bool(config['base_url_explicit'])


def _resolve_text(environment):
    stored = _stored('text')
    config = _overlay_text(environment, stored)
    config['role'] = 'text'
    config['configured'] = _text_configured(config)
    config['uses_text_model'] = False
    return config


def _resolve(role):
    role = normalize_role(role)
    text_environment = _text_environment()
    if role == 'text':
        return _resolve_text(text_environment)

    text_config = _resolve_text(text_environment)
    stored = _stored(role)
    role_config = _overlay_role(role, _role_environment(role, text_environment), stored)

    if not role_config['dedicated']:
        config = dict(text_config)
        config['role'] = role
        config['source'] = 'database' if stored else 'environment'
        config['uses_text_model'] = True
        return config

    base_url = role_config['base_url'] or text_config['base_url']
    api_key = role_config['api_key']
    if not api_key and base_url == text_config['base_url']:
        # The text key goes only to the text model's server.
        api_key = text_config['api_key']
    config = dict(role_config)
    config.update(
        role=role,
        api_key=api_key,
        base_url=base_url,
        configured=True,
        uses_text_model=False,
    )
    return config


# ---------------------------------------------------------------- public API

def resolve_config(role, reveal_key=False):
    """The effective configuration of a role.

    The API key is masked unless ``reveal_key`` is true; only the request
    path that builds a client needs the real value.
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


def merge_api_key(incoming, existing):
    """The key to store: an empty or masked incoming value keeps the old one."""
    if incoming is None:
        return existing
    incoming = incoming.strip() if isinstance(incoming, str) else incoming
    if not incoming or incoming == MASKED_API_KEY:
        return existing
    return incoming


def _normalize_saved_values(role, values):
    unknown = sorted(set(values) - CONFIG_FIELDS)
    if unknown:
        raise LLMConfigError(
            f"Unknown LLM configuration field(s) for {role}: {', '.join(unknown)}")

    normalized = {}
    for name, raw in values.items():
        if name == 'extra_body':
            normalized['extra_body'] = parse_extra_body(raw)
        elif name == 'gpt5_force':
            if isinstance(raw, bool):
                normalized[name] = raw
            elif isinstance(raw, str) and raw.strip().lower() in ('true', 'false'):
                normalized[name] = raw.strip().lower() == 'true'
            else:
                raise LLMConfigError('gpt5_force must be true or false')
        elif raw is None:
            continue
        elif isinstance(raw, str):
            if raw.strip():
                normalized[name] = raw.strip()
        else:
            normalized[name] = raw
    return normalized


def save_config(role, values):
    """Validate and save a role configuration; returns its public form.

    A rejected value raises LLMConfigError and changes nothing. An empty or
    masked API key keeps the stored one. A configuration with no fields left
    is removed, so the environment applies again.
    """
    role = normalize_role(role)
    if not isinstance(values, Mapping):
        raise LLMConfigError('The LLM configuration must be a JSON object')

    normalized = _normalize_saved_values(role, values)
    existing = _stored(role)
    kept_key = merge_api_key(normalized.get('api_key'), existing.get('api_key'))
    normalized.pop('api_key', None)
    if kept_key:
        normalized['api_key'] = kept_key

    if not normalized:
        clear_config(role)
        return public_config(role)

    from src.models import SystemSetting

    SystemSetting.set_setting(
        SETTING_KEYS[role],
        json.dumps(normalized, sort_keys=True),
        description=SETTING_DESCRIPTION.format(role=role),
        setting_type='string',
    )
    clear_client_cache(role)
    return public_config(role)


def clear_config(role):
    """Remove the saved configuration, so the environment applies again."""
    role = normalize_role(role)
    from src.database import db
    from src.models import SystemSetting

    SystemSetting.query.filter_by(key=SETTING_KEYS[role]).delete()
    db.session.commit()
    clear_client_cache(role)


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
            row = SystemSetting.query.filter_by(key=setting_key).first()
            if row is not None and row.value not in (None, ''):
                return row.value
    except Exception as e:  # noqa: BLE001 - fall back to the environment on any read failure
        logger.debug('Could not read the runtime setting %s: %s', setting_key, e)
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
    values = {}
    for name, (setting_key, env_name, default) in RUNTIME_SETTINGS.items():
        values[name] = _runtime_raw(setting_key, env_name, default)

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
_http_client = httpx.Client(verify=True, headers=APP_HEADERS)


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
    with _clients_lock:
        cached = _clients.get(role)
        if cached is not None and cached.signature == signature:
            return cached.client
        client = OpenAI(
            api_key=config['api_key'] or 'not-needed',
            base_url=config['base_url'],
            http_client=_http_client,
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
