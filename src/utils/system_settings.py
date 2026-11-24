"""Helpers for hot-applied System Settings backed by environment fallback."""

import os
import sys
from types import ModuleType
from typing import Any, Callable, Dict, Tuple


def _env_bool(env: str, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        return default
    # Only "true" (any case) counts as on, as everywhere else in Speakr.
    return raw.strip().lower() == "true"


def _env_int(env: str, default: int) -> int:
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return default


def _env_str(env: str, default: str) -> str:
    raw = os.environ.get(env)
    if raw is None:
        return default
    return raw.strip()


def env_fallback(env: str, default: Any) -> Any:
    if isinstance(default, bool):
        return _env_bool(env, default)
    if isinstance(default, int) and not isinstance(default, bool):
        return _env_int(env, default)
    return _env_str(env, default)


def make_module_getattr(mapping: Dict[str, Tuple[str, str, Any]]) -> Callable[[str], Any]:
    """Return a module ``__getattr__`` that resolves hot-applied settings.

    ``mapping`` maps a module attribute name to ``(setting_key, env_var, default)``.
    """

    def __getattr__(name: str) -> Any:
        if name not in mapping:
            raise AttributeError(f"module has no attribute {name!r}")
        setting_key, env_var, default = mapping[name]
        fallback = env_fallback(env_var, default)
        try:
            from flask import has_app_context

            if has_app_context():
                from src.models import SystemSetting

                return SystemSetting.get_setting(setting_key, fallback)
        except Exception:
            pass
        return fallback

    return __getattr__


def apply_hot_settings(module_name: str, mapping: Dict[str, Tuple[str, str, Any]]
                       ) -> Tuple[ModuleType, Callable[[str], Any]]:
    """Return ``(module, __getattr__)`` for a module with hot-applied settings.

    A module registers its settings with one line after its map::

        _self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)

    Python calls a module-level ``__getattr__`` for any name the module does not
    define (PEP 562), so every ``_self.NAME`` read looks the setting up again,
    from System Settings inside an app context and from ``.env`` otherwise.
    """
    return sys.modules[module_name], make_module_getattr(mapping)


# Settings that can be changed in System Settings and that fall back to an
# environment variable. Their rows are created with no value, so the value
# from .env applies until an admin saves one in System Settings.
# key: (env var, default, setting_type, description)
ENV_BACKED_SETTINGS: Dict[str, Tuple[str, Any, str, str]] = {
    'allow_registration': ('ALLOW_REGISTRATION', True, 'boolean', 'Whether self-service registration is open.'),
    'registration_allowed_domains': ('REGISTRATION_ALLOWED_DOMAINS', '', 'string', 'Comma/whitespace-separated domains allowed to register. Empty means no restriction.'),
    'users_can_delete': ('USERS_CAN_DELETE', True, 'boolean', 'Whether non-admin users can delete their recordings.'),
    'enable_public_sharing': ('ENABLE_PUBLIC_SHARING', True, 'boolean', 'Whether public share links are allowed.'),
    'enable_internal_sharing': ('ENABLE_INTERNAL_SHARING', False, 'boolean', 'Whether internal sharing between users and groups is allowed.'),
    'show_usernames_in_ui': ('SHOW_USERNAMES_IN_UI', False, 'boolean', 'Whether usernames are visible in the UI.'),
    'user_search_scope': ('USER_SEARCH_SCOPE', '', 'string', 'Who a user can find by part of a username when sharing: everyone, groups or exact. Unset: everyone when usernames are shown, else exact.'),
    'enable_incognito_mode': ('ENABLE_INCOGNITO_MODE', False, 'boolean', 'Whether incognito recording is enabled.'),
    'incognito_temp_max_age_hours': ('INCOGNITO_TEMP_MAX_AGE_HOURS', 6, 'integer', 'Hours after which a leftover temporary file of an incognito request is removed. Longer than the longest incognito request.'),
    'incognito_mode_default': ('INCOGNITO_MODE_DEFAULT', False, 'boolean', 'Default incognito state for new recordings.'),
    'enable_auto_deletion': ('ENABLE_AUTO_DELETION', False, 'boolean', 'Master switch for automatic deletion of old recordings.'),
    'global_retention_days': ('GLOBAL_RETENTION_DAYS', 0, 'integer', 'Number of days to retain recordings globally. 0 disables auto-deletion.'),
    'deletion_mode': ('DELETION_MODE', 'full_recording', 'string', 'Deletion mode: audio_only or full_recording.'),
    'recording_tombstone_days': ('RECORDING_TOMBSTONE_DAYS', 90, 'integer', 'Days to keep changes-feed tombstones.'),
    'webhook_delivery_retention_days': ('WEBHOOK_DELIVERY_RETENTION_DAYS', 30, 'integer', 'Days to keep webhook delivery records (with their payloads) after they were created. 0 keeps them without limit.'),
    'webhook_omit_titles': ('WEBHOOK_OMIT_TITLES', False, 'boolean', 'Whether the recording title and the original filename are left out of webhook payloads.'),
    'auto_export_delete_with_recording': ('AUTO_EXPORT_DELETE_WITH_RECORDING', False, 'boolean', 'Whether the automatically exported file of a recording is deleted with the recording. Off: the file is renamed with the [deleted]_ prefix and its content is kept.'),
    'video_retention': ('VIDEO_RETENTION', False, 'boolean', 'Whether to keep the original video stream.'),
    'video_passthrough_asr': ('VIDEO_PASSTHROUGH_ASR', False, 'boolean', 'Whether to send original video to ASR.'),
    'asr_async_jobs': ('ASR_ASYNC_JOBS', True, 'boolean', 'Whether the asynchronous jobs API of the WhisperX ASR service is used when the service advertises it. Off: every transcription is sent to the synchronous /asr endpoint.'),
    'help_center_url': ('HELP_CENTER_URL', '', 'string', 'Optional help center URL.'),
    'help_center_label': ('HELP_CENTER_LABEL', '', 'string', 'Optional help center label.'),
    'default_ui_theme': ('DEFAULT_UI_THEME', 'cards', 'string', 'UI theme for new accounts, and for the sign-in pages in a browser with no saved theme.'),
    'default_color_scheme': ('DEFAULT_COLOR_SCHEME', 'emerald', 'string', 'Color scheme for new accounts, and for the sign-in pages in a browser with no saved color scheme.'),
    'default_audio_player_position': ('DEFAULT_AUDIO_PLAYER_POSITION', 'top', 'string', 'Audio player position on the recording page for new accounts: top or bottom.'),
    'default_ui_language': ('DEFAULT_UI_LANGUAGE', 'en', 'string', 'Interface language for new accounts: the code of a shipped language, such as en or de.'),
    'default_transcription_language': ('DEFAULT_TRANSCRIPTION_LANGUAGE', 'auto', 'string', 'Transcription language for new accounts: auto (the service detects it) or a language code such as en.'),
    'default_output_language': ('DEFAULT_OUTPUT_LANGUAGE', 'auto', 'string', 'Language of summaries, chat and translations for new accounts: auto (the model decides) or an English language name such as German.'),
    'default_timezone_mode': ('DEFAULT_TIMEZONE_MODE', 'auto', 'string', 'Time zone of new accounts: auto (the zone of the browser) or fixed (the default time zone).'),
    'default_auto_summarization': ('DEFAULT_AUTO_SUMMARIZATION', True, 'boolean', 'Whether new accounts have automatic summaries on.'),
    'default_auto_speaker_labelling': ('DEFAULT_AUTO_SPEAKER_LABELLING', False, 'boolean', 'Whether new accounts have automatic speaker labelling on.'),
    'default_extract_events': ('DEFAULT_EXTRACT_EVENTS', False, 'boolean', 'Whether new accounts have event extraction on.'),
    'max_concurrent_uploads': ('MAX_CONCURRENT_UPLOADS', 3, 'integer', 'Maximum number of concurrent uploads.'),
    'enable_email_verification': ('ENABLE_EMAIL_VERIFICATION', False, 'boolean', 'Whether email verification is enabled.'),
    'require_email_verification': ('REQUIRE_EMAIL_VERIFICATION', False, 'boolean', 'Whether verified email is required.'),
    'session_idle_timeout_minutes': ('SESSION_IDLE_TIMEOUT_MINUTES', 0, 'integer', 'Minutes without a request after which a browser session must sign in again. 0 means no idle timeout. API tokens are not affected.'),
    'api_query_tokens_enabled': ('API_QUERY_TOKENS_ENABLED', False, 'boolean', 'Whether an API token is accepted in the deprecated ?token= query parameter (GET and HEAD only). Off (the default): a token is accepted in a header only.'),
    'sso_api_tokens_enabled': ('SSO_API_TOKENS_ENABLED', False, 'boolean', 'Whether SSO access tokens from allowed apps are accepted on the API and can be exchanged for API tokens.'),
    'sso_api_allowed_clients': ('SSO_API_ALLOWED_CLIENTS', '', 'string', 'Comma-separated OIDC client IDs whose access tokens are accepted (matched against aud or azp). Empty means none.'),
    'sso_api_exchange_clients': ('SSO_API_EXCHANGE_CLIENTS', '', 'string', 'Comma-separated OIDC client IDs whose access tokens may be exchanged for API tokens. Each must also be in sso_api_allowed_clients. Empty means no exchange.'),
    'sso_api_scopes': ('SSO_API_SCOPES', 'read', 'string', 'Comma-separated scopes a request with an SSO access token has, and the most an exchanged token may have.'),
    'sso_api_client_scopes': ('SSO_API_CLIENT_SCOPES', '', 'string', 'Per-app scope limits, as client_id=scope1 scope2 pairs separated by commas or new lines. A listed app gets those scopes within sso_api_scopes; an app not listed gets sso_api_scopes.'),
    'sso_api_client_names': ('SSO_API_CLIENT_NAMES', '', 'string', 'Display names for OIDC client IDs, as client_id=Name pairs separated by commas or new lines. Shown on API tokens created through the SSO token exchange.'),
    'audit_log_enabled': ('AUDIT_LOG_ENABLED', True, 'boolean', 'Whether sign-ins, access to recordings, changes, shares and administrative actions are recorded in the audit log. Changes of the audit log settings are recorded also when this is off.'),
    'audit_log_retention_days': ('AUDIT_LOG_RETENTION_DAYS', 365, 'integer', 'Days after which an audit log row is removed. 0 keeps the rows without limit.'),
    'audit_log_store_ip': ('AUDIT_LOG_STORE_IP', 'full', 'string', 'How the client address is stored in the audit log: full, truncated (the /24 network of an IPv4 address, the /48 network of an IPv6 address) or off.'),
    'audit_log_record_views': ('AUDIT_LOG_RECORD_VIEWS', True, 'boolean', 'Whether opening and playing a recording in the web interface is recorded in the audit log. Downloads, exports, API reads and public link events are recorded in either case.'),
    'llm_request_timeout': ('LLM_REQUEST_TIMEOUT', 600, 'integer', 'Seconds to wait for a language model answer (the read timeout). Raise it for slow reasoning models.'),
    'llm_max_retries': ('LLM_MAX_RETRIES', 2, 'integer', 'How often a failed language model request is retried.'),
    'llm_connect_timeout': ('LLM_CONNECT_TIMEOUT', 30.0, 'float', 'Seconds to wait for a connection to the language model server.'),
    'llm_write_timeout': ('LLM_WRITE_TIMEOUT', 30.0, 'float', 'Seconds to wait while sending a request to the language model server.'),
    'enable_stream_options': ('ENABLE_STREAM_OPTIONS', True, 'boolean', 'Whether streaming requests ask for token usage in the final chunk. Turn this off for servers that reject the OpenAI stream_options parameter.'),
    'prefix_cache_optimized_prompts': ('PREFIX_CACHE_OPTIMIZED_PROMPTS', False, 'boolean', 'Whether prompts are laid out so the provider can reuse the transcript prefix from its prompt cache.'),
    'text_model_cache_breakpoints': ('TEXT_MODEL_CACHE_BREAKPOINTS', False, 'boolean', 'Whether the shared prompt prefix is sent as its own part with a cache breakpoint, for providers that need an explicit one. Used together with prompt cache optimization.'),
}


def env_backed_display_value(key: str) -> str:
    """The .env value of an env-backed setting, as System Settings stores it."""
    env_var, default, _type, _desc = ENV_BACKED_SETTINGS[key]
    value = env_fallback(env_var, default)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)
