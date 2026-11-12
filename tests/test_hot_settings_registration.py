"""Every module with hot-applied System Settings registers them through
apply_hot_settings, and each mapped name resolves on every read.

A module lists its settings in ``_SYSTEM_SETTING_MAP`` and registers them with
``_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)``.
``_self.NAME`` then reads System Settings inside an app context and the
``.env`` value otherwise, with no value cached in the module.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models import SystemSetting
from src.utils.system_settings import apply_hot_settings, env_fallback

REGISTRATION = "_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)"


def _modules_with_a_setting_map():
    names = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if "_SYSTEM_SETTING_MAP = {" in path.read_text(encoding="utf-8"):
            names.append(".".join(path.relative_to(ROOT).with_suffix("").parts))
    return names


MODULES = _modules_with_a_setting_map()


def _env_value_unlike(default):
    """An .env string whose parsed value differs from the default."""
    if isinstance(default, bool):
        return "false" if default else "true"
    if isinstance(default, int):
        return str(default + 7)
    return "hot-setting-test-value"


def test_the_setting_maps_are_found():
    assert len(MODULES) >= 20
    assert "src.app" in MODULES and "src.tasks.processing" in MODULES


@pytest.mark.parametrize("module_name", MODULES)
def test_the_module_registers_its_map_with_the_helper(module_name):
    path = ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
    source = path.read_text(encoding="utf-8")
    assert source.count(REGISTRATION) == 1
    assert "make_module_getattr" not in source


@pytest.mark.parametrize("module_name", MODULES)
def test_each_mapped_name_resolves_from_env_on_every_read(module_name, monkeypatch):
    module = importlib.import_module(module_name)
    assert module._self is module
    assert module.__getattr__.__qualname__ == "make_module_getattr.<locals>.__getattr__"

    for name, (_key, env_var, default) in module._SYSTEM_SETTING_MAP.items():
        # A real module attribute of the same name would hide the setting.
        assert name not in vars(module), name

        monkeypatch.delenv(env_var, raising=False)
        assert getattr(module._self, name) == default, name

        raw = _env_value_unlike(default)
        monkeypatch.setenv(env_var, raw)
        expected = env_fallback(env_var, default)
        assert expected != default, name
        assert getattr(module._self, name) == expected, name

    with pytest.raises(AttributeError):
        module.__getattr__("NOT_A_HOT_SETTING")


@pytest.mark.parametrize("module_name", MODULES)
def test_each_mapped_name_reads_system_settings_in_an_app_context(module_name, monkeypatch):
    module = importlib.import_module(module_name)
    calls = []

    def fake_get_setting(key, default_value=None):
        calls.append((key, default_value))
        return ("from-system-settings", key)

    monkeypatch.setattr(SystemSetting, "get_setting", staticmethod(fake_get_setting))
    with Flask("hot-settings-test").app_context():
        for name, (key, env_var, default) in module._SYSTEM_SETTING_MAP.items():
            monkeypatch.delenv(env_var, raising=False)
            assert getattr(module._self, name) == ("from-system-settings", key), name
            # The .env value is passed as the fallback for an unset row.
            assert calls[-1] == (key, default), name


def test_the_helper_returns_the_module_and_a_getattr():
    mapping = {"HOT_TEST_FLAG": ("hot_test_flag", "SPEAKR_HOT_TEST_FLAG", False)}
    module, getattr_fn = apply_hot_settings(__name__, mapping)
    assert module is sys.modules[__name__]
    os.environ.pop("SPEAKR_HOT_TEST_FLAG", None)
    assert getattr_fn("HOT_TEST_FLAG") is False
