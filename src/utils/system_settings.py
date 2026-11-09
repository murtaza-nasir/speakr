"""Helpers for hot-applied System Settings backed by environment fallback."""

import os
from typing import Any, Callable, Dict, Tuple


def _env_bool(env: str, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


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
