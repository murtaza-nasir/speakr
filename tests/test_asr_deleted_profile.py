"""A profile id that no longer exists resolves to the default model.

Tags, folders and queued jobs keep the id of the profile chosen for them. After
the profile is deleted, the id must not reach the environment connector as a
model name.
"""
from src.services import asr_config


def _setup(monkeypatch, document):
    calls = []
    monkeypatch.setattr(asr_config, "load_document", lambda: document)
    monkeypatch.setattr(asr_config, "_environment_connector", lambda: ("env", None))
    monkeypatch.setattr(asr_config, "_legacy_connector", lambda value: calls.append(value) or ("legacy", value))
    monkeypatch.setattr(asr_config, "_profile_connector", lambda profile: "profile:" + profile["id"])
    monkeypatch.setattr(asr_config, "_profile_model", lambda profile: profile["config"].get("model"))
    monkeypatch.setattr(asr_config, "default_profile_id", lambda: document.get("default"))
    return calls


def test_deleted_profile_id_uses_the_environment_without_a_default(monkeypatch):
    calls = _setup(monkeypatch, {"models": [], "default": None})
    assert asr_config.resolve_asr_connector("m_deadbeef") == ("env", None)
    assert calls == []


def test_deleted_profile_id_uses_the_default_profile(monkeypatch):
    document = {"models": [{"id": "m_0123abcd", "config": {"model": "small"}}], "default": "m_0123abcd"}
    calls = _setup(monkeypatch, document)
    assert asr_config.resolve_asr_connector("m_deadbeef") == ("profile:m_0123abcd", "small")
    assert calls == []


def test_a_legacy_model_name_still_reaches_the_environment(monkeypatch):
    calls = _setup(monkeypatch, {"models": [], "default": None})
    assert asr_config.resolve_asr_connector("large-v3") == ("legacy", "large-v3")
    assert calls == ["large-v3"]
