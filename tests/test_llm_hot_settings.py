"""Hot-applied LLM runtime settings.

Timeouts, retries, streaming compatibility and the prompt-cache flags fall back
to their environment variables, can be saved in System Settings, and apply
without a restart: a connection setting rebuilds the client on the next
request, and the prompt-cache flags are read when a prompt is built instead of
being captured at import.
"""

import os
import sys
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.init_db import initialize_database
from src.models import SystemSetting
from src.services import llm_config
from src.tasks import processing
from src.utils.system_settings import ENV_BACKED_SETTINGS

app.config["WTF_CSRF_ENABLED"] = False

SETTINGS = (
    ("llm_request_timeout", "LLM_REQUEST_TIMEOUT", 600, "integer"),
    ("llm_max_retries", "LLM_MAX_RETRIES", 2, "integer"),
    ("llm_connect_timeout", "LLM_CONNECT_TIMEOUT", 30.0, "float"),
    ("llm_write_timeout", "LLM_WRITE_TIMEOUT", 30.0, "float"),
    ("enable_stream_options", "ENABLE_STREAM_OPTIONS", True, "boolean"),
    ("prefix_cache_optimized_prompts", "PREFIX_CACHE_OPTIMIZED_PROMPTS", False, "boolean"),
    ("text_model_cache_breakpoints", "TEXT_MODEL_CACHE_BREAKPOINTS", False, "boolean"),
)
KEYS = tuple(entry[0] for entry in SETTINGS)


@pytest.fixture
def ctx(monkeypatch):
    for _key, env_name, _default, _kind in SETTINGS:
        monkeypatch.delenv(env_name, raising=False)
    with app.app_context():
        yield monkeypatch
        db.session.rollback()
        _clear_rows()
        llm_config.clear_client_cache()


def _clear_rows():
    for key in KEYS:
        SystemSetting.set_setting(key, None, ENV_BACKED_SETTINGS[key][3],
                                  ENV_BACKED_SETTINGS[key][2])
    db.session.commit()


def _save(key, value):
    SystemSetting.set_setting(key, value, ENV_BACKED_SETTINGS[key][3],
                              ENV_BACKED_SETTINGS[key][2])
    db.session.commit()


# ---------------------------------------------------------------- registration and seeding


@pytest.mark.parametrize("key,env_name,default,kind", SETTINGS)
def test_each_runtime_setting_is_registered_with_its_environment_fallback(
        key, env_name, default, kind):
    registered = ENV_BACKED_SETTINGS[key]
    assert registered[0] == env_name
    assert registered[1] == default
    assert registered[2] == kind
    assert registered[3].strip()


def test_initialize_database_seeds_the_runtime_settings(ctx):
    SystemSetting.query.filter(SystemSetting.key.in_(KEYS)).delete(synchronize_session=False)
    db.session.commit()

    initialize_database(app)

    for key, _env_name, _default, kind in SETTINGS:
        row = SystemSetting.query.filter_by(key=key).one()
        assert row.value is None, key
        assert row.setting_type == kind, key
        assert row.description == ENV_BACKED_SETTINGS[key][3], key


def test_system_settings_lists_them_with_their_type_and_environment_source(ctx):
    _clear_rows()
    rows = {row["key"]: row for row in _settings_list()}
    for key, _env_name, _default, kind in SETTINGS:
        row = rows[key]
        assert row["setting_type"] == kind, key
        assert row["env_backed"] is True, key
        assert row["source"] == "env", key
        assert row["description"] == ENV_BACKED_SETTINGS[key][3], key


def _settings_list():
    from src.models import User

    user = User(username="hot_llm_admin", email="hot_llm_admin@local.test",
                password="x", is_admin=True)
    db.session.add(user)
    db.session.commit()
    try:
        from flask import g
        from flask.testing import FlaskClient

        class _Client(FlaskClient):
            def open(self, *args, **kwargs):
                g.pop("_login_user", None)
                return super().open(*args, **kwargs)

        client = _Client(app, app.response_class)
        with client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
        response = client.get("/admin/settings")
        assert response.status_code == 200
        return response.get_json()
    finally:
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


# ---------------------------------------------------------------- values in effect


def test_the_environment_applies_while_no_value_is_saved(ctx):
    ctx.setenv("LLM_REQUEST_TIMEOUT", "120")
    ctx.setenv("LLM_MAX_RETRIES", "5")
    ctx.setenv("LLM_CONNECT_TIMEOUT", "4.5")
    ctx.setenv("LLM_WRITE_TIMEOUT", "6.5")
    ctx.setenv("ENABLE_STREAM_OPTIONS", "false")

    settings = llm_config.runtime_settings()

    assert settings.request_timeout == 120
    assert settings.max_retries == 5
    assert settings.connect_timeout == 4.5
    assert settings.write_timeout == 6.5
    assert settings.enable_stream_options is False


def test_a_saved_value_wins_over_the_environment(ctx):
    ctx.setenv("LLM_REQUEST_TIMEOUT", "120")
    _save("llm_request_timeout", "900")

    assert llm_config.runtime_settings().request_timeout == 900


def test_a_changed_runtime_setting_applies_to_the_next_read(ctx):
    assert llm_config.runtime_settings().request_timeout == 600

    _save("llm_request_timeout", "300")
    assert llm_config.runtime_settings().request_timeout == 300

    _save("llm_request_timeout", None)
    assert llm_config.runtime_settings().request_timeout == 600


def test_an_invalid_saved_value_falls_back_to_the_default(ctx):
    _save("llm_request_timeout", "not-a-number")
    assert llm_config.runtime_settings().request_timeout == 600


# ---------------------------------------------------------------- clients


class _FakeOpenAI:
    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeOpenAI.instances.append(self)


@pytest.fixture
def fake_openai(ctx):
    _FakeOpenAI.instances = []
    ctx.setattr(llm_config, "OpenAI", _FakeOpenAI)
    ctx.setenv("TEXT_MODEL_API_KEY", "key")
    ctx.setenv("TEXT_MODEL_BASE_URL", "http://env.lan/v1")
    llm_config.clear_client_cache()
    return _FakeOpenAI


@pytest.mark.parametrize("key,value", [
    ("llm_request_timeout", "60"),
    ("llm_max_retries", "7"),
    ("llm_connect_timeout", "3.5"),
    ("llm_write_timeout", "4.5"),
    ("enable_stream_options", "false"),
])
def test_a_changed_connection_setting_rebuilds_the_client(fake_openai, key, value):
    first = llm_config.get_client("text")
    assert llm_config.get_client("text") is first

    _save(key, value)
    second = llm_config.get_client("text")

    assert second is not first
    assert len(fake_openai.instances) == 2


def test_an_unchanged_connection_setting_reuses_the_client(fake_openai):
    first = llm_config.get_client("text")
    _save("llm_request_timeout", "600")
    assert llm_config.get_client("text") is first
    assert len(fake_openai.instances) == 1


# ---------------------------------------------------------------- prompt cache


def test_the_prompt_cache_flags_follow_the_environment(monkeypatch):
    # Outside an application context the .env value is the only source, which
    # is what a fresh process starts from.
    monkeypatch.delenv("PREFIX_CACHE_OPTIMIZED_PROMPTS", raising=False)
    monkeypatch.delenv("TEXT_MODEL_CACHE_BREAKPOINTS", raising=False)
    assert processing.prefix_cache_flags() == (False, False)

    monkeypatch.setenv("PREFIX_CACHE_OPTIMIZED_PROMPTS", "true")
    assert processing.prefix_cache_flags() == (True, False)

    monkeypatch.setenv("TEXT_MODEL_CACHE_BREAKPOINTS", "true")
    assert processing.prefix_cache_flags() == (True, True)


def test_the_prompt_cache_flags_follow_a_saved_value(ctx):
    ctx.setenv("PREFIX_CACHE_OPTIMIZED_PROMPTS", "true")
    _save("prefix_cache_optimized_prompts", "false")
    _save("text_model_cache_breakpoints", "true")

    assert processing.prefix_cache_flags() == (False, True)


def test_a_prompt_builder_reads_the_flags_when_it_builds_the_prompt(ctx):
    seen = []
    ctx.setattr(processing, "resolve_title_instructions",
                lambda recording: ("- Keep it short", "test"))
    ctx.setattr(processing, "_end_transaction_before_external_call", lambda *instances: None)
    ctx.setattr(processing, "get_temperature", lambda kind: 0.7)
    ctx.setattr(processing, "build_title_prompt",
                lambda *args: seen.append(args[3]) or ("system", "prompt"))
    ctx.setattr(processing, "build_user_content", lambda *args: "content")
    ctx.setattr(processing, "call_llm_completion", lambda *a, **k: _completion())
    recording = SimpleNamespace(id=1, transcription="A short transcript.",
                                owner=None, user_id=1)

    _save("prefix_cache_optimized_prompts", "false")
    assert processing._generate_ai_title(recording) == "ok"
    _save("prefix_cache_optimized_prompts", "true")
    processing._generate_ai_title(recording)

    assert seen == [False, True]


def test_a_patched_module_attribute_still_wins(ctx):
    ctx.setattr(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", True)
    ctx.setattr(processing, "TEXT_MODEL_CACHE_BREAKPOINTS", True)
    _save("prefix_cache_optimized_prompts", "false")
    _save("text_model_cache_breakpoints", "false")

    assert processing.prefix_cache_flags() == (True, True)


def _completion():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=None,
    )
