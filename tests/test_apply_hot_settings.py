"""Unit tests for the centralized hot-settings registration helper.

The helper owns the wiring that turns a module settings map into a module
whose constants resolve dynamically through ``make_module_getattr``. These
tests exercise that seam directly, without importing every consuming module.
"""

import sys
import types
from pathlib import Path

import pytest

import src.app  # noqa: F401  (ensures the Flask app and models are initialized)
from src.app import app, db
from src.models import SystemSetting
from src.utils import system_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"


@pytest.fixture
def synthetic_module():
    """Register a throwaway module in ``sys.modules`` and clean it up."""
    created = []

    def make(name):
        module = types.ModuleType(name)
        sys.modules[name] = module
        created.append(name)
        return module

    yield make

    for name in created:
        sys.modules.pop(name, None)


@pytest.fixture
def synthetic_setting():
    """Create a SystemSetting row for a synthetic key and remove it after."""
    key = "_test_apply_hot_settings"

    def save(value, setting_type):
        with app.app_context():
            SystemSetting.set_setting(key, value, None, setting_type)

    yield key, save

    with app.app_context():
        row = SystemSetting.query.filter_by(key=key).first()
        if row is not None:
            db.session.delete(row)
            db.session.commit()


def test_all_published_aliases_are_the_same_function():
    assert (
        system_settings.register_hot_settings
        is system_settings.install_hot_settings
        is system_settings.hot_apply_settings
        is system_settings._apply_hot_settings
        is system_settings.apply_hot_settings
    )


def test_explicit_module_is_registered_and_unrelated_module_is_untouched(
    synthetic_module,
):
    module = synthetic_module("_test_hot_explicit")
    unrelated = synthetic_module("_test_hot_unrelated")

    system_settings.apply_hot_settings(
        module, {"FEATURE_FLAG": ("feature_flag", "FEATURE_FLAG_ENV", False)}
    )

    assert module._self is module
    assert callable(module.__getattr__)
    assert "_self" not in unrelated.__dict__
    assert "__getattr__" not in unrelated.__dict__
    with pytest.raises(AttributeError):
        unrelated.FEATURE_FLAG


def test_keyword_forms_are_accepted(synthetic_module):
    module_a = synthetic_module("_test_hot_kwargs_mapping")
    module_b = synthetic_module("_test_hot_kwargs_settings_map")
    module_c = synthetic_module("_test_hot_positional_mapping")

    system_settings.apply_hot_settings(
        module=module_a,
        mapping={"ALPHA": ("alpha_key", "ALPHA_ENV", 1)},
    )
    system_settings.apply_hot_settings(
        module=module_b,
        settings_map={"BETA": ("beta_key", "BETA_ENV", 2)},
    )
    system_settings.apply_hot_settings(
        {"THETA": ("theta_key", "THETA_ENV", 3)},
        module=module_c,
    )

    assert module_a._self is module_a
    assert module_b._self is module_b
    assert module_c._self is module_c
    assert module_a.ALPHA == 1
    assert module_b.BETA == 2
    assert module_c.THETA == 3


def test_mapping_only_call_registers_the_calling_module(synthetic_module):
    module = synthetic_module("_test_hot_inferred")
    name = module.__name__

    code = (
        "from src.utils.system_settings import apply_hot_settings\n"
        "apply_hot_settings({'GAMMA': ('gamma_key', 'GAMMA_ENV', 'default')})\n"
    )
    exec(compile(code, name, "exec"), module.__dict__)

    assert module._self is module
    assert module.GAMMA == "default"


def test_env_fallback_is_resolved(monkeypatch, synthetic_module):
    module = synthetic_module("_test_hot_env")
    system_settings.apply_hot_settings(
        module, {"ENV_FLAG": ("env_flag", "ENV_FLAG_VALUE", False)}
    )

    monkeypatch.setenv("ENV_FLAG_VALUE", "true")
    assert module.ENV_FLAG is True
    assert module._self.ENV_FLAG is True

    monkeypatch.setenv("ENV_FLAG_VALUE", "false")
    assert module.ENV_FLAG is False


def test_saved_setting_overrides_env(
    monkeypatch, synthetic_module, synthetic_setting
):
    key, save = synthetic_setting
    module = synthetic_module("_test_hot_saved")
    system_settings.apply_hot_settings(
        module, {"SAVED_FLAG": (key, "SAVED_FLAG_ENV", True)}
    )

    monkeypatch.setenv("SAVED_FLAG_ENV", "true")
    with app.app_context():
        assert module.SAVED_FLAG is True

    save("false", "boolean")
    with app.app_context():
        assert module.SAVED_FLAG is False


def test_registration_is_idempotent(monkeypatch, synthetic_module):
    module = synthetic_module("_test_hot_idempotent")
    mapping = {"IDEMPOTENT_FLAG": ("idempotent_flag", "IDEMPOTENT_ENV", False)}

    system_settings.apply_hot_settings(module, mapping)
    first_getattr = module.__getattr__
    monkeypatch.setenv("IDEMPOTENT_ENV", "true")

    system_settings.apply_hot_settings(module, mapping)

    assert module._self is module
    assert callable(module.__getattr__)
    assert module.IDEMPOTENT_FLAG is True
    assert first_getattr("IDEMPOTENT_FLAG") is True


def test_non_dict_mapping_raises_type_error(synthetic_module):
    module = synthetic_module("_test_hot_type_error")

    with pytest.raises(TypeError):
        system_settings.apply_hot_settings("not-a-mapping")
    with pytest.raises(TypeError):
        system_settings.apply_hot_settings(module, "not-a-mapping")
    with pytest.raises(TypeError):
        system_settings.apply_hot_settings(module=module, mapping="not-a-mapping")


def test_unresolvable_inferred_module_raises():
    code = (
        "from src.utils.system_settings import apply_hot_settings\n"
        "apply_hot_settings({'DELTA': ('delta_key', 'DELTA_ENV', 4)})\n"
    )
    with pytest.raises(RuntimeError):
        exec(
            compile(code, "_test_hot_missing_module", "exec"),
            {"__name__": "_test_hot_missing_module"},
        )


def test_every_module_with_a_settings_map_uses_the_helper():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "_SYSTEM_SETTING_MAP =" not in text:
            continue
        relative = path.relative_to(PROJECT_ROOT)
        if text.count("apply_hot_settings(_SYSTEM_SETTING_MAP)") != 1:
            offenders.append(f"{relative}: missing single helper call")
        if "_make_getattr(_SYSTEM_SETTING_MAP)" in text:
            offenders.append(f"{relative}: still uses make_module_getattr directly")
        if "sys.modules[__name__]" in text:
            offenders.append(f"{relative}: still captures _self manually")

    assert not offenders, "\n".join(offenders)
