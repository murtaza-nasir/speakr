"""Configured ASR (transcription) profiles and runtime resolution.

Speakr selects its transcription connector at startup from environment
variables. This module adds a runtime layer on top of that: one versioned
document stored in the ``asr_config_json`` system setting holds the configured
profiles (one profile = one selectable transcription model) and the id of the
explicit default profile. With no profiles configured, every transcription
behaves exactly as the environment-only release did.

A profile carries ``id``, ``label``, ``connector`` (one of the registered
transcription connector names), ``config`` (connector-native configuration
dict) and ``visible`` (whether users may pick it). The connector registry is
the single source of truth: it decides which connectors exist, which fields
they accept and which of those are secret.

The environment is a synthetic profile, never stored. Its id is
``env:<connector>:<model>`` with a lowercase, dash-separated connector and a
lowercase model; its label is the same ``<connector>:<model>`` without the
prefix. It is the default until a profile is explicitly marked.

Secrets are stored in clear text because connectors need them. ``api_key``,
``secret_key`` and every schema property marked ``writeOnly`` are masked in
every public representation. An empty or masked value submitted on update
keeps the stored secret; ``clear_secrets`` removes every secret; changing the
URL without typing a new secret removes the stored one. The environment
profile is masked the same way; a form started from it names
``from_environment`` so the server fills in the environment secrets.

A profile taken from the environment (migrated, imported, or saved from the
environment row without a key of its own) stores no secret. It is marked
``env_credentials`` and gets the environment's secrets when it runs, so a
rotated TRANSCRIPTION_API_KEY, ASR_API_KEY or Tencent secret reaches it.
Typing a key, removing the key, changing the address or the connector ends
the link. The link only holds while the profile's address is the
environment's current address: after the environment moves to another
host, the profile gets no environment secret, so the key is never sent to
a host that is no longer the environment's.

The changes write the document in the caller's transaction and do not commit,
so an admin route commits the change and its audit row together.
"""

import copy
import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SETTING_KEY = 'asr_config_json'
CONFIG_VERSION = 1
MASKED_API_KEY = '***'
MAX_MODELS = 12
MAX_PROFILES = MAX_MODELS
MAX_LABEL_LENGTH = 80
SETTING_DESCRIPTION = (
    'Configured transcription (ASR) profiles and the default profile. '
    'Managed by the Transcription models section of System Settings.'
)

PROFILE_FIELDS = frozenset({'label', 'connector', 'config', 'visible'})
SECRET_FIELDS = frozenset({'api_key', 'secret_key'})
ENV_CREDENTIALS = 'env_credentials'
URL_FIELDS = ('base_url', 'endpoint')

# Connector names as they appear in an environment profile id. A connector
# without an entry falls back to its registry name lowercased with dashes.
ENV_CONNECTOR_NAMES = {
    'openai_whisper': 'openai-whisper',
    'openai_transcribe': 'openai-transcribe',
    'asr_endpoint': 'whisper-asr',
    'azure_openai_transcribe': 'azure-openai-transcribe',
    'mistral': 'mistral',
    'vibevoice': 'vibevoice',
    'funasr': 'funasr',
    'mossland': 'mossland',
    'assemblyai': 'assemblyai',
    'tencent_asr': 'tencent-asr',
    'openasr': 'openasr',
    'audiocpp': 'audiocpp',
}


class ASRConfigError(ValueError):
    """A submitted or stored ASR configuration is unusable."""


class ASRProfileNotFound(ASRConfigError):
    """No configured profile has the given id."""

    def __init__(self, profile_id):
        self.profile_id = profile_id
        super().__init__(f'No configured profile with id {profile_id!r}')


class ASRProfileLabelTaken(ASRConfigError):
    """Another configured profile already uses the label."""

    def __init__(self, label):
        self.label = label
        super().__init__(f'A profile labelled {label!r} already exists')


class ASRProfileLimitReached(ASRConfigError):
    """The document already holds the maximum number of profiles."""

    def __init__(self, limit):
        self.limit = limit
        super().__init__(
            f'At most {limit} profiles can be configured; delete one before '
            'adding another')


class ASRProfileNotVisible(ASRConfigError):
    """A hidden profile cannot become the default or be hidden while default."""

    def __init__(self, profile_id):
        self.profile_id = profile_id
        super().__init__(
            f'Profile {profile_id!r} is hidden; make it visible first')


class ASRProfileUnavailable(ASRConfigError):
    """A chosen profile (by id, or as the default) cannot build its connector.

    The transcription fails with this error instead of running on the
    environment connector, its key and its address.
    """

    def __init__(self, profile_id, label, cause):
        self.profile_id = profile_id
        self.label = label
        self.cause = cause
        super().__init__(
            f'Transcription model "{label}" cannot be used: {cause}. '
            'An administrator can correct it in System settings > Transcription models.')


# Backwards-friendly aliases for callers that still use the model wording.
ASRModelNotFound = ASRProfileNotFound
ASRModelLabelTaken = ASRProfileLabelTaken
ASRModelLimitReached = ASRProfileLimitReached


@dataclass
class ProfileChange:
    """A created or updated profile and the names of the changed fields."""

    profile: dict
    changed_fields: tuple = ()
    # False when nothing was stored (an import that found the same profile).
    created: bool = True

    @property
    def model(self):
        """The profile, under the name existing callers use."""
        return self.profile


@dataclass
class DefaultChange:
    """A change of the default profile."""

    old: Optional[str]
    new: Optional[str]


# ---------------------------------------------------------------- helpers


def _registry():
    from src.services.transcription.registry import get_registry

    return get_registry()


def _connector_names():
    try:
        return tuple(item['name'] for item in _registry().list_connectors())
    except Exception:  # noqa: BLE001
        return ()


def _is_connector(name):
    if not isinstance(name, str) or not name.strip():
        return False
    try:
        _registry().get_connector_class(name.strip())
        return True
    except Exception:  # noqa: BLE001
        return False


def _connector_class(connector_name):
    return _registry().get_connector_class(connector_name)


def env_connector_name(connector):
    """The connector part of an environment profile id."""
    if not isinstance(connector, str) or not connector.strip():
        return ''
    name = connector.strip().lower()
    if name in ENV_CONNECTOR_NAMES:
        return ENV_CONNECTOR_NAMES[name]
    return name.replace('_', '-')


def connector_from_env_name(name):
    """Reverse :func:`env_connector_name`; the registry name or None."""
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if _is_connector(name):
        return name
    lowered = name.lower()
    for connector, env_name in ENV_CONNECTOR_NAMES.items():
        if env_name == lowered:
            return connector
    for connector in _connector_names():
        if env_connector_name(connector) == lowered:
            return connector
    return None


def default_label(connector, model):
    """``<connector>:<model>`` in environment-id form."""
    env_name = env_connector_name(connector)
    model_slug = '' if model is None else str(model).strip().lower()
    return f'{env_name}:{model_slug}'


def secret_fields(connector_name, config=None):
    """Every secret field the connector declares (or a stored config uses)."""
    try:
        schema = _connector_class(connector_name).get_config_schema() or {}
    except Exception:  # noqa: BLE001
        schema = {}
    properties = schema.get('properties') or {}
    fields = set()
    for name in SECRET_FIELDS:
        if name in properties or (config is not None and name in config):
            fields.add(name)
    for name, spec in properties.items():
        if isinstance(spec, Mapping) and spec.get('writeOnly'):
            fields.add(name)
    return fields


def _required_text(values, name):
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ASRConfigError(f'{name} is required')
    return value.strip()


def normalize_base_url(value):
    """Validate and normalize a base URL."""
    if not isinstance(value, str) or not value.strip():
        raise ASRConfigError('base_url is required')
    value = value.strip().rstrip('/')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise ASRConfigError('base_url must start with http:// or https://')
    if parts.username is not None or parts.password is not None:
        raise ASRConfigError('base_url must not include credentials')
    return value


def merge_secret(incoming, existing):
    """The secret to store: empty or masked incoming keeps the old value."""
    if incoming is None:
        return existing or ''
    if not isinstance(incoming, str):
        raise ASRConfigError('secret values must be strings')
    incoming = incoming.strip()
    if not incoming or incoming == MASKED_API_KEY:
        return existing or ''
    return incoming


def new_secret(incoming, existing):
    """The secret entered now: empty, masked and echoed values are not new."""
    value = merge_secret(incoming, '')
    return '' if value == (existing or '') else value


# Backwards-friendly name for callers that only deal with api_key.
merge_api_key = merge_secret
new_key = new_secret


def _bool_value(value, name, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false', '1', '0', 'yes', 'no'):
        return value.strip().lower() in ('true', '1', 'yes')
    raise ASRConfigError(f'{name} must be true or false')


def _label_taken(models, label, exclude_id=None):
    key = label.casefold()
    return any(
        model['id'] != exclude_id and str(model.get('label', '')).casefold() == key
        for model in models)


def _normalize_config(config, connector_name):
    if not isinstance(config, Mapping):
        raise ASRConfigError('config must be a JSON object')
    config = dict(config)
    for field in URL_FIELDS:
        raw = config.get(field)
        if isinstance(raw, str) and raw.strip():
            try:
                config[field] = normalize_base_url(raw)
            except ASRConfigError:
                # Keep the raw value if it is not a URL we can validate; the
                # connector rejects it later.
                config[field] = raw.strip().rstrip('/')
    for field in secret_fields(connector_name):
        if field in config and config[field] is not None and not isinstance(config[field], str):
            config[field] = str(config[field])
    return config


def _drop_undeclared_secrets(connector_name, config):
    """Remove an api_key or secret_key the connector's schema does not declare.

    The environment config of the WhisperX connector holds ASR_API_KEY, which
    that connector never sends; it is not a field of the profile.
    """
    try:
        properties = (_connector_class(connector_name).get_config_schema() or {}).get('properties') or {}
    except Exception:  # noqa: BLE001
        return config
    for field in SECRET_FIELDS:
        if field in config and field not in properties:
            config.pop(field)
    return config


def _validate_config_for_connector(connector_name, config):
    """Reject a connector the registry does not know and unknown config keys.

    Required-field enforcement stays lenient: a profile may legitimately omit
    a secret for a local/keyless endpoint, and the connector validates at use
    time.
    """
    try:
        connector_class = _connector_class(connector_name)
    except Exception as error:  # noqa: BLE001
        raise ASRConfigError(str(error)) from error
    schema = connector_class.get_config_schema() or {}
    properties = schema.get('properties') or {}
    unknown = sorted(set(config) - set(properties))
    if unknown:
        raise ASRConfigError(
            f"Unknown config field(s) for {connector_name}: {', '.join(unknown)}")
    return None


def _stored_profile(raw):
    if not isinstance(raw, Mapping):
        return None
    pid = raw.get('id')
    if not isinstance(pid, str) or not pid.strip():
        return None
    connector = raw.get('connector')
    if not isinstance(connector, str) or not connector.strip():
        return None
    connector = connector.strip()
    if not _is_connector(connector):
        return None
    label = raw.get('label')
    if not isinstance(label, str) or not label.strip():
        label = pid.strip()
    else:
        label = label.strip()
    config = raw.get('config')
    if not isinstance(config, Mapping):
        config = {}
    config = _drop_undeclared_secrets(connector, _normalize_config(config, connector))
    visible = raw.get('visible')
    if not isinstance(visible, bool):
        visible = True
    profile = {
        'id': pid.strip(),
        'label': label,
        'connector': connector,
        'config': config,
        'visible': visible,
    }
    if raw.get(ENV_CREDENTIALS) is True:
        profile[ENV_CREDENTIALS] = True
    return profile


def _empty_document():
    return {
        'version': CONFIG_VERSION,
        'models': [],
        'default': None,
    }


def _normalize_document(raw):
    document = _empty_document()
    if not isinstance(raw, Mapping):
        return document

    seen = set()
    for item in raw.get('models') or []:
        profile = _stored_profile(item)
        if profile is None or profile['id'] in seen:
            continue
        seen.add(profile['id'])
        document['models'].append(profile)

    default = raw.get('default')
    if isinstance(default, str) and default.strip():
        document['default'] = default.strip()
    return document


# The document read for the current app context, with the System Setting
# cache generation it was read at: one transcription resolves the
# configuration several times, and a write to system_setting starts a new
# generation.
_DOCUMENT_CACHE = '_asr_config_document'


def _setting_generation():
    from src.models import system

    return system._SYSTEM_SETTING_CACHE_GENERATION


def _read_document():
    try:
        from src.models import SystemSetting

        row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
        raw = row.value if row is not None else None
    except Exception:  # noqa: BLE001
        return _empty_document()
    if not raw:
        return _empty_document()
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning('Ignoring %s: the saved ASR configuration is not valid JSON', SETTING_KEY)
        return _empty_document()
    return _normalize_document(document)


def load_document():
    """The saved configuration document, normalized, or an empty one.

    The document is read once per app context (a request or a job) and again
    after any write to system_setting.
    """
    from flask import g, has_app_context

    if not has_app_context():
        return _read_document()
    generation = _setting_generation()
    cached = g.get(_DOCUMENT_CACHE)
    if cached is None or cached[0] != generation:
        cached = (generation, _read_document())
        setattr(g, _DOCUMENT_CACHE, cached)
    return copy.deepcopy(cached[1])


def save_document(document):
    """Write the document in the current transaction; the caller commits."""
    from flask import g, has_app_context
    from src.database import db
    from src.models import SystemSetting, system

    document = _normalize_document(document)
    value = json.dumps(document, sort_keys=True)
    row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None:
        db.session.add(SystemSetting(key=SETTING_KEY, value=value,
                                     description=SETTING_DESCRIPTION,
                                     setting_type='string'))
    else:
        row.value = value
        row.description = SETTING_DESCRIPTION
        row.updated_at = datetime.utcnow()
    db.session.flush()
    system._SYSTEM_SETTING_CACHE_GENERATION += 1
    if has_app_context():
        g.pop(_DOCUMENT_CACHE, None)
    return document


_save_document = save_document


def _find_profile(document, profile_id):
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ASRConfigError('profile_id must be a non-empty string')
    profile_id = profile_id.strip()
    for index, profile in enumerate(document['models']):
        if profile['id'] == profile_id:
            return index, profile
    raise ASRProfileNotFound(profile_id)


# ---------------------------------------------------------------- capabilities


CAPABILITY_FLAGS = (
    ('diarization', 'DIARIZATION'),
    ('speaker_count_control', 'SPEAKER_COUNT_CONTROL'),
    ('exact_speaker_count', 'EXACT_SPEAKER_COUNT'),
    ('hotwords', 'HOTWORDS'),
    ('initial_prompt', 'INITIAL_PROMPT'),
    ('timestamps', 'TIMESTAMPS'),
    ('language_selection', 'LANGUAGE_SELECTION'),
    ('language_detection', 'LANGUAGE_DETECTION'),
    ('chunking', 'CHUNKING'),
    ('speaker_embeddings', 'SPEAKER_EMBEDDINGS'),
)


def capabilities_for_connector(connector_name, config):
    """The read-only capability map of a connector instance, or {}."""
    try:
        connector = _registry().create_connector(connector_name, dict(config or {}))
        current = getattr(connector, 'CAPABILITIES', set())
        names = {capability.name for capability in current}
    except Exception:  # noqa: BLE001
        return {}
    return {flag: code in names for flag, code in CAPABILITY_FLAGS}


def profile_capabilities(profile):
    return capabilities_for_connector(profile.get('connector'), runtime_config(profile))


def uses_env_credentials(profile):
    """Whether the profile takes its secrets from the environment."""
    return isinstance(profile, Mapping) and profile.get(ENV_CREDENTIALS) is True


def _without_secrets(connector_name, config):
    config = dict(config or {})
    for field in secret_fields(connector_name, config):
        if field in config:
            config[field] = ''
    return config


def _same_address(config, other):
    """Whether both configs name the same base URL and endpoint."""
    for field in URL_FIELDS:
        left = str((config or {}).get(field) or '').strip().rstrip('/')
        right = str((other or {}).get(field) or '').strip().rstrip('/')
        if left != right:
            return False
    return True


def runtime_config(profile):
    """The config a connector is built with.

    The stored config, except that a profile marked ``env_credentials`` gets
    the current environment secrets of its connector. It gets none when the
    environment uses another connector or another address, so the
    environment key only goes to the environment's own host.
    """
    config = dict(profile.get('config') or {})
    if uses_env_credentials(profile):
        connector = profile.get('connector')
        base = _environment_config_for(connector) or {}
        if base and not _same_address(config, base):
            base = {}
        for field in secret_fields(connector, config) | secret_fields(connector, base):
            config[field] = base.get(field) or ''
    return config


def _public_profile(profile, reveal_key=False):
    result = dict(profile)
    config = dict(profile.get('config') or {})
    connector = profile.get('connector')
    runtime = runtime_config(profile) if uses_env_credentials(profile) else config
    fields = secret_fields(connector, config) | secret_fields(connector, runtime)
    for field in fields:
        if field not in config and field not in runtime:
            continue
        if reveal_key:
            config[field] = runtime.get(field) or ''
        else:
            config[field] = MASKED_API_KEY if runtime.get(field) else ''
    result['config'] = config
    result['capabilities'] = profile_capabilities(profile)
    return result


# ---------------------------------------------------------------- CRUD


def list_models(reveal_key=False):
    """The configured profiles, with secrets masked unless revealed."""
    return [_public_profile(p, reveal_key) for p in load_document()['models']]


def get_model(profile_id, reveal_key=False):
    """One configured profile, with secrets masked unless revealed."""
    _, profile = _find_profile(load_document(), profile_id)
    return _public_profile(profile, reveal_key)


def visible_models():
    """Profiles users may pick: id, label, connector, model and capabilities only."""
    return [
        {
            'id': profile['id'],
            'label': profile['label'],
            'connector': profile['connector'],
            'model': _profile_model(profile),
            'capabilities': profile_capabilities(profile),
        }
        for profile in load_document()['models']
        if profile.get('visible', True)
    ]


def is_selectable(value):
    """Whether a user may choose ``value`` as the transcription model.

    True for a visible profile id, the environment id, and a plain model name
    equal to the model of either (legacy tag and folder defaults). With no
    profiles configured nothing is curated, so every value is accepted.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    profiles = load_document()['models']
    if not profiles:
        return True
    visible = [profile for profile in profiles if profile.get('visible', True)]
    if any(profile['id'] == value for profile in visible):
        return True
    environment = environment_profile()
    if environment is not None and value.casefold() == environment['id'].casefold():
        return True
    key = value.casefold()
    models = {_profile_model(profile).casefold() for profile in visible}
    if environment is not None:
        models.add(_profile_model(environment).casefold())
    models.discard('')
    return key in models


def schema_defaults(connector_name):
    """The default value of every config field the connector's schema gives one."""
    try:
        schema = _connector_class(connector_name).get_config_schema() or {}
    except Exception:  # noqa: BLE001
        return {}
    defaults = {}
    for name, spec in (schema.get('properties') or {}).items():
        if isinstance(spec, Mapping) and 'default' in spec:
            defaults[name] = copy.deepcopy(spec['default'])
    return defaults


def _config_for_new_connector(connector, incoming_config):
    """The config of a profile moved to another connector.

    It starts from the new connector's defaults plus the submitted fields;
    nothing of the previous connector's config is kept, and a secret is kept
    only when it was typed for the new connector.
    """
    config = schema_defaults(connector)
    secrets = secret_fields(connector, incoming_config)
    for field, value in (incoming_config or {}).items():
        if field in secrets:
            continue
        config[field] = value
    for field in secrets:
        typed = merge_secret(incoming_config.get(field), '')
        if typed or field in config:
            config[field] = typed
    return config


def _url_changed(existing_config, incoming_config):
    for field in URL_FIELDS:
        if field in incoming_config:
            old_url = str(existing_config.get(field) or '').strip().rstrip('/')
            new_url = str(incoming_config.get(field) or '').strip().rstrip('/')
            if old_url != new_url:
                return True
    return False


def _typed_secret(connector, incoming_config, existing_config):
    """Whether the incoming config names a secret other than the stored one."""
    return any(
        new_secret(incoming_config.get(field), existing_config.get(field, ''))
        for field in secret_fields(connector, incoming_config)
        if field in incoming_config)


def environment_option():
    """The environment as the tag and folder forms show a legacy value: id, label, model."""
    environment = environment_profile()
    if environment is None:
        return None
    return {
        'value': environment['id'],
        'label': environment['label'],
        'model': _profile_model(environment),
    }


def create_profile(values, env_credentials=False):
    """Store a new profile.

    ``env_credentials`` (internal, for the migration and the import) stores
    the profile without secrets, linked to the environment's.
    """
    if not isinstance(values, Mapping):
        raise ASRConfigError('The profile configuration must be a JSON object')

    unknown = sorted(set(values) - PROFILE_FIELDS
                     - {'clear_secrets', 'clear_api_key', 'from_environment'})
    if unknown:
        raise ASRConfigError(f"Unknown profile field(s): {', '.join(unknown)}")
    from_environment = _bool_value(values.get('from_environment'), 'from_environment', False)

    label = _required_text(values, 'label')
    if len(label) > MAX_LABEL_LENGTH:
        raise ASRConfigError(f'label must be at most {MAX_LABEL_LENGTH} characters')
    connector = _required_text(values, 'connector')
    if not _is_connector(connector):
        raise ASRConfigError(
            f'Unknown connector: {connector}. Available: {", ".join(_connector_names())}')

    config = _normalize_config(values.get('config', {}), connector)
    base = _environment_config_for(connector) if from_environment else None
    if base is not None:
        # A form started from the environment row: the browser only holds
        # masked secrets. Without a typed key, a removed key or a changed
        # address, the profile keeps using the environment's secrets.
        clear = bool(values.get('clear_secrets') or values.get('clear_api_key'))
        if not (clear or _url_changed(base, config) or _typed_secret(connector, config, base)):
            env_credentials = True
        config = _merge_config_for_update({'config': base}, config, connector, clear)
    if env_credentials:
        config = _without_secrets(connector, config)
    _validate_config_for_connector(connector, config)
    for field in secret_fields(connector):
        if field in config:
            config[field] = merge_secret(config.get(field), '')

    visible = _bool_value(values.get('visible'), 'visible', True) if 'visible' in values else True

    document = load_document()
    if len(document['models']) >= MAX_MODELS:
        raise ASRProfileLimitReached(MAX_MODELS)
    if _label_taken(document['models'], label):
        raise ASRProfileLabelTaken(label)

    profile = {
        'id': f'm_{uuid.uuid4().hex[:8]}',
        'label': label,
        'connector': connector,
        'config': config,
        'visible': bool(visible),
    }
    if env_credentials:
        profile[ENV_CREDENTIALS] = True
    document['models'].append(profile)
    save_document(document)
    return ProfileChange(_public_profile(profile), tuple(sorted(set(values) & PROFILE_FIELDS)))


def _merge_config_for_update(existing, incoming_config, connector, clear_secrets):
    """Merge an incoming config into the stored one, honouring secret rules."""
    existing_config = dict(existing.get('config') or {})
    incoming_config = dict(incoming_config or {})

    url_changed = _url_changed(existing_config, incoming_config)

    merged = dict(existing_config)
    merged.update(incoming_config)

    fields = secret_fields(connector, merged)
    if clear_secrets:
        for field in fields:
            merged[field] = ''
    elif url_changed:
        for field in fields:
            merged[field] = new_secret(incoming_config.get(field), existing_config.get(field, ''))
    else:
        for field in fields:
            if field in incoming_config or field in existing_config:
                merged[field] = merge_secret(incoming_config.get(field), existing_config.get(field, ''))
    return merged


def update_profile(profile_id, values):
    if not isinstance(values, Mapping):
        raise ASRConfigError('The profile configuration must be a JSON object')

    allowed = PROFILE_FIELDS | {'clear_secrets', 'clear_api_key'}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ASRConfigError(f"Unknown profile field(s): {', '.join(unknown)}")

    clear_secrets = bool(values.get('clear_secrets') or values.get('clear_api_key'))

    document = load_document()
    index, existing = _find_profile(document, profile_id)
    updated = dict(existing)
    updated['config'] = dict(existing.get('config') or {})

    if 'label' in values:
        label = _required_text(values, 'label')
        if len(label) > MAX_LABEL_LENGTH:
            raise ASRConfigError(f'label must be at most {MAX_LABEL_LENGTH} characters')
        if _label_taken(document['models'], label, exclude_id=profile_id):
            raise ASRProfileLabelTaken(label)
        updated['label'] = label

    connector = updated['connector']
    if 'connector' in values:
        connector = _required_text(values, 'connector')
        if not _is_connector(connector):
            raise ASRConfigError(
                f'Unknown connector: {connector}. Available: {", ".join(_connector_names())}')
        updated['connector'] = connector

    if 'visible' in values:
        visible = _bool_value(values.get('visible'), 'visible', existing.get('visible', True))
        if visible is False and document.get('default') == existing['id']:
            raise ASRProfileNotVisible(existing['id'])
        updated['visible'] = visible

    if clear_secrets:
        updated.pop(ENV_CREDENTIALS, None)
    connector_changed = connector != existing['connector']
    if connector_changed:
        # Nothing of the previous connector is carried over: no field, no
        # secret, no link to the environment's secrets.
        updated.pop(ENV_CREDENTIALS, None)

    if connector_changed:
        incoming_config = values.get('config', {})
        if incoming_config is None:
            incoming_config = {}
        if not isinstance(incoming_config, Mapping):
            raise ASRConfigError('config must be a JSON object')
        incoming_config = _normalize_config(incoming_config, connector)
        merged = _config_for_new_connector(connector, incoming_config)
    elif 'config' in values:
        incoming_config = values.get('config')
        if not isinstance(incoming_config, Mapping):
            raise ASRConfigError('config must be a JSON object')
        incoming_config = _normalize_config(incoming_config, connector)
        stored_config = dict(existing.get('config') or {})
        if (_url_changed(stored_config, incoming_config)
                or _typed_secret(connector, incoming_config, stored_config)):
            # A key of its own or another address ends the link to the
            # environment's secrets.
            updated.pop(ENV_CREDENTIALS, None)
        merged = _merge_config_for_update(existing, incoming_config, connector, clear_secrets)
    elif clear_secrets:
        merged = dict(existing.get('config') or {})
        for field in secret_fields(connector):
            merged[field] = ''
    else:
        merged = dict(updated.get('config') or {})

    # Revalidate whenever the connector changed, so a config for the previous
    # connector cannot be stored against the new one.
    if 'connector' in values or 'config' in values or clear_secrets:
        _validate_config_for_connector(connector, merged)
    updated['config'] = merged

    changed = set(
        name for name in PROFILE_FIELDS
        if existing.get(name) != updated.get(name))
    if uses_env_credentials(existing) != uses_env_credentials(updated):
        changed.add('config')
    changed = tuple(sorted(changed))

    if changed or clear_secrets:
        document['models'][index] = updated
        save_document(document)
    return ProfileChange(_public_profile(updated), changed)


def delete_profile(profile_id):
    document = load_document()
    index, existing = _find_profile(document, profile_id)
    document['models'].pop(index)
    if document.get('default') == existing['id']:
        document['default'] = None
    save_document(document)
    return _public_profile(existing)


# ---------------------------------------------------------------- default


def set_default(profile_id):
    document = load_document()
    old = document.get('default')
    new = None
    if profile_id is not None:
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ASRConfigError('profile_id must be a non-empty string or null')
        new = profile_id.strip()
        _, profile = _find_profile(document, new)
        if not profile.get('visible', True):
            raise ASRProfileNotVisible(new)
    if old != new:
        document['default'] = new
        save_document(document)
        # Re-probe the voice embedding backend so speaker profiles are
        # invalidated when the backend changes (#380). Best effort only.
        try:
            from flask import current_app, has_app_context
            if has_app_context():
                from src.services import voice_embedding_check
                voice_embedding_check.check_voice_embeddings(current_app._get_current_object(), force=True)
        except Exception:  # noqa: BLE001
            pass
    return DefaultChange(old, new)


def clear_default():
    return set_default(None)


def default_profile_id():
    """The explicitly marked profile, or None (the environment)."""
    document = load_document()
    default = document.get('default')
    if not isinstance(default, str) or not default.strip():
        return None
    default = default.strip()
    if any(model['id'] == default for model in document['models']):
        return default
    return None


# ---------------------------------------------------------------- environment


def _env_connector():
    reg = _registry()
    try:
        name = reg.get_active_connector_name()
    except Exception:  # noqa: BLE001
        name = ''
    if not name:
        try:
            reg.initialize_from_env()
            name = reg.get_active_connector_name()
        except Exception:  # noqa: BLE001
            name = ''
    return name


def environment_profile(reveal_key=False):
    """The synthetic profile built from the environment, or None.

    Its secrets are masked like a stored profile's unless ``reveal_key`` is
    true; only the server-side snapshot and probe paths need the real values.
    """
    try:
        reg = _registry()
        name = _env_connector()
        if not name:
            return None
        config = reg._build_config_from_env(name)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(config, Mapping):
        return None
    config = _drop_undeclared_secrets(name, _normalize_config(config, name))
    model = config.get('model')
    if not isinstance(model, str):
        model = '' if model is None else str(model)
    model = model.strip()
    env_name = env_connector_name(name)
    model_slug = model.lower()
    profile = {
        'id': f'env:{env_name}:{model_slug}',
        # Without a model the label is the connector alone, not "name:".
        'label': f'{env_name}:{model_slug}' if model_slug else env_name,
        'connector': name,
        'config': config,
        'visible': True,
        'environment': True,
    }
    profile['capabilities'] = profile_capabilities(profile)
    if not reveal_key:
        masked = dict(config)
        for field in secret_fields(name, masked):
            if field in masked:
                masked[field] = MASKED_API_KEY if masked.get(field) else ''
        profile['config'] = masked
    return profile


def _environment_config_for(connector_name):
    """The environment config when it belongs to ``connector_name``, else None."""
    environment = environment_profile(reveal_key=True)
    if environment is None or environment['connector'] != connector_name:
        return None
    return dict(environment['config'])


def import_from_environment():
    """Snapshot the environment connector into a profile.

    Returns the existing profile when an identical one (same connector and
    config) is already stored, so a second import does not duplicate it.
    """
    profile = environment_profile(reveal_key=True)
    if profile is None:
        raise ASRConfigError('No environment-configured ASR provider is available')
    connector = profile['connector']
    document = load_document()
    wanted = _without_secrets(connector, profile['config'])
    for item in document['models']:
        if (item.get('connector') == connector
                and _without_secrets(connector, item.get('config')) == wanted
                and runtime_config(item) == profile['config']):
            return ProfileChange(_public_profile(item), (), created=False)
    return create_profile({
        'label': profile['label'],
        'connector': connector,
        'config': profile['config'],
        'visible': True,
    }, env_credentials=True)


# ---------------------------------------------------------------- migration


def _legacy_visible_entries():
    try:
        from src.models import SystemSetting

        raw = SystemSetting.get_setting('transcription_models_visible_json', None)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning('Ignoring transcription_models_visible_json: not valid JSON')
        return []
    if not isinstance(parsed, list):
        return []
    entries = []
    for item in parsed:
        if isinstance(item, str):
            value = item.strip()
            label = ''
        elif isinstance(item, Mapping):
            value = item.get('value')
            label = item.get('label') or ''
            if not isinstance(value, str):
                continue
            value = value.strip()
            label = label.strip() if isinstance(label, str) else ''
        else:
            continue
        if value:
            entries.append((value, label))
    return entries


def _legacy_env_entries():
    try:
        from src.config.app_config import TRANSCRIPTION_MODEL_OPTIONS
    except Exception:  # noqa: BLE001
        return []
    entries = []
    for item in TRANSCRIPTION_MODEL_OPTIONS or []:
        if isinstance(item, Mapping):
            value = item.get('value')
            label = item.get('label') or ''
        elif isinstance(item, str):
            value, label = item, ''
        else:
            continue
        if isinstance(value, str) and value.strip():
            entries.append((value.strip(), str(label or '').strip()))
    return entries


def _legacy_default_model():
    try:
        from src.models import SystemSetting

        raw = SystemSetting.get_setting('transcription_default_model', None)
    except Exception:  # noqa: BLE001
        raw = None
    return str(raw).strip() if raw else ''


def _unique_label(label, used, fallback):
    base = (label or fallback or 'Model').strip()
    candidate = base
    number = 2
    while candidate.casefold() in used:
        candidate = f'{base} ({number})'
        number += 1
    used.add(candidate.casefold())
    return candidate


def ensure_migrated():
    """Create the document once from the legacy settings. Idempotent.

    Nothing is stored when the legacy models cannot be migrated because the
    environment provider cannot be read, so the next start tries again. When
    another process (a second gunicorn worker) stores the document first, its
    document is kept and this call returns False without an error.
    """
    from src.database import db

    try:
        from src.models import SystemSetting

        if SystemSetting.query.filter_by(key=SETTING_KEY).first() is not None:
            return False
    except Exception as error:  # noqa: BLE001
        logger.debug('ASR configuration migration skipped: %s', error)
        return False

    try:
        document = _migrated_document()
    except Exception as error:  # noqa: BLE001
        db.session.rollback()
        logger.warning('ASR configuration migration failed and is retried at the next start: %s', error)
        return False
    if document is None:
        return False
    return _insert_document(document)


def _insert_document(document):
    """Insert the first document; False when another process inserted one."""
    from flask import g, has_app_context
    from sqlalchemy.exc import IntegrityError
    from src.database import db
    from src.models import SystemSetting, system

    value = json.dumps(_normalize_document(document), sort_keys=True)
    try:
        db.session.add(SystemSetting(key=SETTING_KEY, value=value,
                                     description=SETTING_DESCRIPTION,
                                     setting_type='string'))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.info('ASR configuration already migrated by another process')
        return False
    system._SYSTEM_SETTING_CACHE_GENERATION += 1
    if has_app_context():
        g.pop(_DOCUMENT_CACHE, None)
    logger.info('ASR configuration migrated: %d profile(s)', len(document['models']))
    return True


def _migrated_document():
    """The document built from the legacy settings, or None to retry later."""
    env = environment_profile(reveal_key=True)
    env_model = ''
    if env is not None:
        env_model = str((env.get('config') or {}).get('model') or '').strip()

    # Sources in order, deduplicated by model value; the first label wins.
    ordered = []
    seen = set()
    for value, label in _legacy_visible_entries() + _legacy_env_entries():
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append((value, label))
    legacy_default = _legacy_default_model()
    default_is_env = bool(env_model) and legacy_default.casefold() == env_model.casefold()
    # Transcriptions without a choice ran on the legacy default, listed or
    # not, so an unlisted default (other than the environment model) still
    # becomes a profile and stays the default.
    if legacy_default and not default_is_env and not any(
            value.casefold() == legacy_default.casefold() for value, _ in ordered):
        ordered.append((legacy_default, ''))
    # The environment model needs no profile while the environment is the
    # default ("use the default" reaches it); with another default it must
    # stay selectable, so it becomes a profile like the other listed models.
    skip_env_model = not legacy_default or default_is_env

    # Keep the legacy default inside the first MAX_MODELS entries.
    if legacy_default:
        index = next((i for i, (value, _) in enumerate(ordered)
                      if value.casefold() == legacy_default.casefold()), None)
        if index is not None and index >= MAX_MODELS:
            entry = ordered.pop(index)
            ordered.insert(MAX_MODELS - 1, entry)
    dropped = ordered[MAX_MODELS:]
    if dropped:
        logger.warning(
            'ASR configuration migration keeps %d models and drops: %s',
            MAX_MODELS, ', '.join(value for value, _ in dropped))
    ordered = ordered[:MAX_MODELS]

    if env is None and (ordered or legacy_default):
        logger.warning(
            'ASR configuration migration postponed: the environment transcription '
            'provider could not be read, so %d legacy model(s) wait for the next start',
            len(ordered))
        return None

    document = _empty_document()
    used_labels = set()
    if env is not None:
        connector = env['connector']
        base_config = dict(env.get('config') or {})
        display_name = connector
        try:
            display_name = _connector_class(connector).DISPLAY_NAME or connector
        except Exception:  # noqa: BLE001
            pass
        for value, label in ordered:
            if skip_env_model and env_model and value.casefold() == env_model.casefold():
                continue
            # No copy of the environment secrets: the profile reads them
            # when it runs, so a rotated key reaches it.
            config = _without_secrets(connector, base_config)
            config['model'] = value
            fallback = f'{display_name}: {value}'
            unique = _unique_label(label, used_labels, fallback)
            document['models'].append({
                'id': f'm_{uuid.uuid4().hex[:8]}',
                'label': unique,
                'connector': connector,
                'config': config,
                'visible': True,
                ENV_CREDENTIALS: True,
            })

    if legacy_default:
        if env_model and legacy_default.casefold() == env_model.casefold():
            document['default'] = None
        else:
            match = next(
                (profile for profile in document['models']
                 if str((profile.get('config') or {}).get('model') or '').casefold()
                 == legacy_default.casefold()),
                None)
            document['default'] = match['id'] if match else None
    return document


# ---------------------------------------------------------------- resolution


def _profile_model(profile):
    config = profile.get('config') or {}
    model = config.get('model')
    if model is None:
        model = ''
    if not isinstance(model, str):
        model = str(model)
    return model.strip()


def _create_connector(connector_name, config):
    return _registry().create_connector(connector_name, dict(config or {}))


def _environment_connector():
    reg = _registry()
    name = _env_connector()
    config = {}
    try:
        config = reg._build_config_from_env(name)
    except Exception:  # noqa: BLE001
        config = {}
    try:
        connector = reg.create_connector(name, config)
    except Exception:  # noqa: BLE001
        connector = reg.get_active_connector()
    model = config.get('model', '')
    if not isinstance(model, str):
        model = '' if model is None else str(model)
    return connector, str(model).strip()


_PROFILE_ID = re.compile(r'm_[0-9a-f]{8}')


def _legacy_connector(model_value):
    """The environment connector with a legacy model name as the model."""
    reg = _registry()
    name = _env_connector()
    try:
        config = reg._build_config_from_env(name)
    except Exception:  # noqa: BLE001
        config = {}
    config = dict(config or {})
    config['model'] = model_value
    try:
        connector = reg.create_connector(name, config)
    except Exception:  # noqa: BLE001
        connector = reg.get_active_connector()
    return connector, model_value


def resolve_asr_connector(value=None):
    """Resolve a profile id / environment id / legacy model to (connector, model).

    ``None`` is the explicit default profile, else the environment. An unknown
    string keeps the historical meaning: the environment connector with that
    model, never a profile's credentials.

    A profile chosen by id or as the default whose connector cannot be built
    raises ASRProfileUnavailable; only a transcription with no choice and no
    default runs on the environment.
    """
    document = load_document()
    profiles = document['models']

    if isinstance(value, str) and value.strip():
        value = value.strip()

        for profile in profiles:
            if profile['id'] == value:
                return _profile_connector(profile), _profile_model(profile)

        if value.startswith('env:'):
            parts = value.split(':', 2)
            if len(parts) == 3:
                connector_name = connector_from_env_name(parts[1])
                model = parts[2].strip()
                if connector_name:
                    try:
                        config = _registry()._build_config_from_env(connector_name)
                    except Exception:  # noqa: BLE001
                        config = {}
                    config = dict(config or {})
                    if model:
                        config['model'] = model
                    try:
                        connector = _create_connector(connector_name, config)
                    except Exception:  # noqa: BLE001
                        logger.warning('Failed to create connector for env profile %s', value)
                        return _environment_connector()
                    return connector, model

        if _PROFILE_ID.fullmatch(value):
            # A deleted profile still named by a tag, a folder or a queued
            # job: its id is no model name, so the default applies.
            logger.warning('ASR profile %r no longer exists; the default model is used', value)
            return resolve_asr_connector(None)

        logger.warning('ASR profile %r not found; falling back to the environment', value)
        return _legacy_connector(value)

    pid = default_profile_id()
    if pid is not None:
        for profile in profiles:
            if profile['id'] == pid:
                return _profile_connector(profile), _profile_model(profile)
    return _environment_connector()


def _profile_connector(profile):
    """The connector of a chosen profile, or ASRProfileUnavailable."""
    try:
        return _create_connector(profile['connector'], runtime_config(profile))
    except Exception as error:  # noqa: BLE001
        cause = ' '.join(str(error).split()) or type(error).__name__
        logger.error('Transcription model %s (%s, connector %s) cannot be used: %s',
                     profile['id'], profile.get('label'), profile['connector'], cause)
        raise ASRProfileUnavailable(profile['id'], profile.get('label') or profile['id'],
                                    cause) from error


def get_connector(profile_id=None):
    connector, _ = resolve_asr_connector(profile_id)
    return connector


# ---------------------------------------------------------------- probe


def _profile_values(values, default_connector=None):
    """The (connector, config) an unsaved test/discover request describes."""
    if not isinstance(values, Mapping):
        raise ASRConfigError('The profile configuration must be a JSON object')
    connector = values.get('connector') or default_connector
    if not isinstance(connector, str) or not connector.strip():
        raise ASRConfigError('connector is required')
    connector = connector.strip()
    if not _is_connector(connector):
        raise ASRConfigError(f'Unknown connector: {connector}')
    config = values.get('config') or {}
    config = _normalize_config(config, connector)
    return connector, config


def _connector_for_probe(profile_id=None, values=None, from_environment=False):
    connector_name = None
    config = None
    if profile_id is None and from_environment and values is not None:
        # The environment row or a form started from it: its secrets reach
        # the browser masked, so they are filled in from the environment
        # here, and dropped when the address was changed.
        incoming_name, incoming_config = _profile_values(values)
        base = _environment_config_for(incoming_name)
        if base is not None:
            connector_name = incoming_name
            config = _merge_config_for_update(
                {'config': base}, incoming_config, connector_name, False)
            values = None
    if profile_id is not None:
        _, stored = _find_profile(load_document(), profile_id)
        connector_name = stored['connector']
        config = runtime_config(stored)
    if values is not None:
        incoming_name, incoming_config = _profile_values(values, connector_name)
        if connector_name is None or incoming_name != connector_name:
            # A different connector starts from its own submitted config; the
            # stored fields belong to the previous schema.
            connector_name = incoming_name
            config = incoming_config
        else:
            config = _merge_config_for_update(
                {'config': config}, incoming_config, connector_name, False)
    if connector_name is None:
        environment = environment_profile(reveal_key=True)
        if environment is None:
            raise ASRConfigError('No environment-configured ASR provider is available')
        connector_name = environment['connector']
        config = dict(environment['config'])
    try:
        return _create_connector(connector_name, config or {})
    except ASRConfigError:
        raise
    except Exception as error:  # noqa: BLE001
        raise ASRConfigError(str(error)) from error


def probe_profile(profile_id=None, values=None, from_environment=False):
    """Probe a stored profile, a submitted one, or the environment."""
    return _connector_for_probe(profile_id, values, from_environment).probe()


def discover_models(profile_id=None, values=None, from_environment=False):
    """Probe and keep the audio models the connector's discovery offers."""
    connector = _connector_for_probe(profile_id, values, from_environment)
    result = connector.probe()
    filters = getattr(connector, 'MODEL_FILTERS', ())
    from src.services.transcription.base import filter_probe_models
    result.models = filter_probe_models(result.models, filters)
    return result
