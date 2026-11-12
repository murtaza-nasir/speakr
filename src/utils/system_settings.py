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
    'enable_incognito_mode': ('ENABLE_INCOGNITO_MODE', False, 'boolean', 'Whether incognito recording is enabled.'),
    'incognito_mode_default': ('INCOGNITO_MODE_DEFAULT', False, 'boolean', 'Default incognito state for new recordings.'),
    'enable_auto_deletion': ('ENABLE_AUTO_DELETION', False, 'boolean', 'Master switch for automatic deletion of old recordings.'),
    'global_retention_days': ('GLOBAL_RETENTION_DAYS', 0, 'integer', 'Number of days to retain recordings globally. 0 disables auto-deletion.'),
    'deletion_mode': ('DELETION_MODE', 'full_recording', 'string', 'Deletion mode: audio_only or full_recording.'),
    'recording_tombstone_days': ('RECORDING_TOMBSTONE_DAYS', 90, 'integer', 'Days to keep changes-feed tombstones.'),
    'video_retention': ('VIDEO_RETENTION', False, 'boolean', 'Whether to keep the original video stream.'),
    'video_passthrough_asr': ('VIDEO_PASSTHROUGH_ASR', False, 'boolean', 'Whether to send original video to ASR.'),
    'help_center_url': ('HELP_CENTER_URL', '', 'string', 'Optional help center URL.'),
    'help_center_label': ('HELP_CENTER_LABEL', '', 'string', 'Optional help center label.'),
    'default_ui_theme': ('DEFAULT_UI_THEME', 'cards', 'string', 'UI theme for new accounts, and for the sign-in pages in a browser with no saved theme.'),
    'default_color_scheme': ('DEFAULT_COLOR_SCHEME', 'emerald', 'string', 'Color scheme for new accounts, and for the sign-in pages in a browser with no saved color scheme.'),
    'max_concurrent_uploads': ('MAX_CONCURRENT_UPLOADS', 3, 'integer', 'Maximum number of concurrent uploads.'),
    'enable_email_verification': ('ENABLE_EMAIL_VERIFICATION', False, 'boolean', 'Whether email verification is enabled.'),
    'require_email_verification': ('REQUIRE_EMAIL_VERIFICATION', False, 'boolean', 'Whether verified email is required.'),
}


def env_backed_display_value(key: str) -> str:
    """The .env value of an env-backed setting, as System Settings stores it."""
    env_var, default, _type, _desc = ENV_BACKED_SETTINGS[key]
    value = env_fallback(env_var, default)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)
