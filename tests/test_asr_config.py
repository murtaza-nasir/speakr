"""Configured ASR profiles: document, secrets, default, visible, migration."""

import json
import re

import pytest

from src.app import app, db
from src.models import SystemSetting
from src.services import asr_config as ac
from src.services.transcription.registry import get_registry


ENV_NAMES = (
    "TRANSCRIPTION_CONNECTOR", "TRANSCRIPTION_BASE_URL", "TRANSCRIPTION_API_KEY",
    "TRANSCRIPTION_MODEL", "ASR_BASE_URL", "ASR_API_KEY", "ASR_DIARIZE",
    "USE_ASR_ENDPOINT", "WHISPER_MODEL",
)
LEGACY_KEYS = ("transcription_models_visible_json", "transcription_default_model")


@pytest.fixture
def ctx():
    with app.app_context():
        _clear_settings()
        yield
        _clear_settings()


def _clear_settings():
    SystemSetting.query.filter(
        SystemSetting.key.in_((ac.SETTING_KEY,) + LEGACY_KEYS)).delete(
            synchronize_session=False)
    db.session.commit()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSCRIPTION_CONNECTOR", "openai_whisper")
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "env-key")
    monkeypatch.setenv("TRANSCRIPTION_BASE_URL", "http://env.lan/v1")
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "whisper-1")
    # The registry caches the active connector; drop it so the environment of
    # this test is the one every helper reads.
    registry = get_registry()
    registry._active_connector = None
    registry._connector_name = ""
    return monkeypatch


@pytest.fixture
def tencent_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSCRIPTION_CONNECTOR", "tencent_asr")
    monkeypatch.setenv("TENCENT_SECRET_ID", "env-id")
    monkeypatch.setenv("TENCENT_SECRET_KEY", "env-secret")
    monkeypatch.setenv("TENCENT_ASR_MODEL", "16k_zh_en_meeting")
    registry = get_registry()
    registry._active_connector = None
    registry._connector_name = ""
    return monkeypatch


def _profile(**overrides):
    profile = {
        "label": "Profile A",
        "connector": "openai_whisper",
        "config": {"api_key": "profile-key", "base_url": "http://profile.lan/v1", "model": "whisper-1"},
        "visible": True,
    }
    profile.update(overrides)
    return profile


def _tencent_profile(**overrides):
    profile = {
        "label": "Tencent A",
        "connector": "tencent_asr",
        "config": {
            "secret_id": "profile-id",
            "secret_key": "profile-secret",
            "model": "16k_zh_en_meeting",
        },
        "visible": True,
    }
    profile.update(overrides)
    return profile


def _stored(profile_id):
    _, profile = ac._find_profile(ac.load_document(), profile_id)
    return profile


# ---------------------------------------------------------------- document


def test_load_document_defaults_when_missing(ctx, clean_env):
    document = ac.load_document()
    assert document == {"version": 1, "models": [], "default": None}


def test_save_document_round_trip_and_version(ctx, clean_env):
    created = ac.create_profile(_profile())
    document = ac.load_document()
    assert document["version"] == 1
    assert document["models"][0]["id"] == created.profile["id"]
    assert document["models"][0]["label"] == "Profile A"


def test_create_profile_id_is_m_uuid8(ctx, clean_env):
    created = ac.create_profile(_profile())
    assert re.fullmatch(r"m_[0-9a-f]{8}", created.profile["id"])
    assert not created.profile["id"].startswith("env:")


def test_create_profile_rejects_empty_label(ctx, clean_env):
    with pytest.raises(ac.ASRConfigError):
        ac.create_profile(_profile(label="  "))


def test_create_profile_rejects_long_label(ctx, clean_env):
    with pytest.raises(ac.ASRConfigError):
        ac.create_profile(_profile(label="x" * 81))


def test_create_profile_rejects_taken_label_case_insensitive(ctx, clean_env):
    ac.create_profile(_profile(label="Same"))
    with pytest.raises(ac.ASRProfileLabelTaken):
        ac.create_profile(_profile(label="sAME"))


def test_create_profile_rejects_unknown_connector(ctx, clean_env):
    with pytest.raises(ac.ASRConfigError):
        ac.create_profile(_profile(connector="not_a_connector"))


def test_create_profile_rejects_unknown_field(ctx, clean_env):
    with pytest.raises(ac.ASRConfigError):
        ac.create_profile({**_profile(), "unexpected": True})


def test_create_profile_rejects_unknown_config_field(ctx, clean_env):
    with pytest.raises(ac.ASRConfigError):
        ac.create_profile(_profile(config={"api_key": "k", "nope": 1}))


def test_create_profile_over_limit(ctx, clean_env):
    for index in range(ac.MAX_MODELS):
        ac.create_profile(_profile(
            label=f"P{index}",
            config={"api_key": "k", "base_url": f"http://x{index}.lan/v1", "model": "whisper-1"}))
    with pytest.raises(ac.ASRProfileLimitReached):
        ac.create_profile(_profile(
            label="extra",
            config={"api_key": "k", "base_url": "http://extra.lan/v1", "model": "whisper-1"}))


def test_update_profile_merges_partial_config(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {"config": {"model": "whisper-1"}})
    stored = _stored(created.profile["id"])
    assert stored["config"]["api_key"] == "profile-key"
    assert stored["config"]["base_url"] == "http://profile.lan/v1"


def test_update_profile_connector_change_starts_from_the_new_defaults(ctx, clean_env):
    """A Whisper profile moved to Tencent keeps nothing of the Whisper config
    (it was a 400 "Unknown config field(s)" before)."""
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {
        "connector": "tencent_asr",
        "config": {"secret_id": "id", "secret_key": "tencent-secret", "model": "16k_zh"}})
    stored = _stored(created.profile["id"])
    assert stored["connector"] == "tencent_asr"
    assert "base_url" not in stored["config"] and "api_key" not in stored["config"]
    assert stored["config"]["secret_key"] == "tencent-secret"
    assert stored["config"]["model"] == "16k_zh"
    for field, value in ac.schema_defaults("tencent_asr").items():
        if field not in ("secret_id", "secret_key", "model"):
            assert stored["config"][field] == value


def test_update_profile_connector_change_never_carries_a_secret(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {
        "connector": "mistral",
        "config": {"api_key": ac.MASKED_API_KEY, "model": "voxtral-mini-latest"}})
    stored = _stored(created.profile["id"])
    assert stored["connector"] == "mistral"
    assert stored["config"].get("api_key", "") == ""
    assert "profile-key" not in json.dumps(ac.load_document())


def test_update_profile_connector_change_without_config(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {"connector": "mistral"})
    stored = _stored(created.profile["id"])
    assert stored["connector"] == "mistral"
    assert stored["config"].get("api_key", "") == ""
    assert "base_url" not in stored["config"] or stored["config"]["base_url"] != "http://profile.lan/v1"


def test_update_profile_same_connector_keeps_the_secret_rules(ctx, clean_env):
    created = ac.create_profile(_profile())
    pid = created.profile["id"]
    ac.update_profile(pid, {"connector": "openai_whisper",
                            "config": {"api_key": "", "model": "whisper-1"}})
    assert _stored(pid)["config"]["api_key"] == "profile-key"
    ac.update_profile(pid, {"connector": "openai_whisper",
                            "config": {"base_url": "http://other.lan/v1"}})
    assert _stored(pid)["config"]["api_key"] == ""


def test_update_missing_profile_raises(ctx, clean_env):
    with pytest.raises(ac.ASRProfileNotFound):
        ac.update_profile("m_deadbeef", {"label": "x"})


def test_delete_profile_clears_default(ctx, clean_env):
    created = ac.create_profile(_profile())
    pid = created.profile["id"]
    ac.set_default(pid)
    ac.delete_profile(pid)
    assert ac.default_profile_id() is None


def test_delete_missing_profile_raises(ctx, clean_env):
    with pytest.raises(ac.ASRProfileNotFound):
        ac.delete_profile("m_deadbeef")


# ---------------------------------------------------------------- default


def test_default_profile_id_returns_explicit_default(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.set_default(created.profile["id"])
    assert ac.default_profile_id() == created.profile["id"]


def test_default_profile_id_returns_none_without_default(ctx, clean_env):
    ac.create_profile(_profile())
    assert ac.default_profile_id() is None


def test_set_default_rejects_hidden_profile(ctx, clean_env):
    created = ac.create_profile(_profile(visible=False))
    with pytest.raises(ac.ASRProfileNotVisible):
        ac.set_default(created.profile["id"])
    assert ac.default_profile_id() is None


def test_hiding_the_default_profile_is_rejected(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.set_default(created.profile["id"])
    with pytest.raises(ac.ASRProfileNotVisible):
        ac.update_profile(created.profile["id"], {"visible": False})
    assert ac.get_model(created.profile["id"])["visible"] is True


def test_set_default_none_means_environment(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.set_default(created.profile["id"])
    ac.set_default(None)
    assert ac.default_profile_id() is None


def test_delete_default_reverts_to_environment(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.set_default(created.profile["id"])
    ac.delete_profile(created.profile["id"])
    assert ac.default_profile_id() is None


# ---------------------------------------------------------------- visible


def test_visible_models_returns_only_visible(ctx, clean_env):
    visible = ac.create_profile(_profile(label="Visible"))
    ac.create_profile(_profile(label="Hidden", visible=False))
    ids = [item["id"] for item in ac.visible_models()]
    assert ids == [visible.profile["id"]]


def test_visible_models_have_no_config_or_secrets(ctx, clean_env):
    ac.create_profile(_profile())
    item = ac.visible_models()[0]
    assert set(item) == {"id", "label", "connector", "model", "capabilities"}
    assert "config" not in item
    assert "api_key" not in item


def test_visible_models_include_capabilities(ctx, clean_env):
    ac.create_profile(_profile())
    capabilities = ac.visible_models()[0]["capabilities"]
    assert capabilities["hotwords"] is True
    assert capabilities["diarization"] is False
    assert set(capabilities) == {flag for flag, _ in ac.CAPABILITY_FLAGS}


# ---------------------------------------------------------------- secrets


def test_list_models_masks_api_key(ctx, clean_env):
    ac.create_profile(_profile())
    assert ac.list_models()[0]["config"]["api_key"] == ac.MASKED_API_KEY
    assert ac.list_models(reveal_key=True)[0]["config"]["api_key"] == "profile-key"


def test_list_models_masks_secret_key(ctx, tencent_env):
    ac.create_profile(_tencent_profile())
    assert ac.list_models()[0]["config"]["secret_key"] == ac.MASKED_API_KEY
    assert ac.list_models()[0]["config"]["secret_id"] == "profile-id"


def test_list_models_masks_every_write_only_field(ctx, clean_env):
    from src.services.transcription.registry import ConnectorRegistry
    for connector in ConnectorRegistry().list_connectors():
        for name, spec in connector["config_schema"].get("properties", {}).items():
            if spec.get("writeOnly"):
                assert name in ac.secret_fields(connector["name"])


def test_reveal_key_returns_real_values(ctx, clean_env):
    created = ac.create_profile(_profile())
    assert ac.get_model(created.profile["id"], reveal_key=True)["config"]["api_key"] == "profile-key"


def test_update_keeps_key_when_empty_or_masked(ctx, clean_env):
    created = ac.create_profile(_profile())
    pid = created.profile["id"]
    ac.update_profile(pid, {"config": {"api_key": ""}})
    assert _stored(pid)["config"]["api_key"] == "profile-key"
    ac.update_profile(pid, {"config": {"api_key": ac.MASKED_API_KEY}})
    assert _stored(pid)["config"]["api_key"] == "profile-key"


def test_update_replaces_key_when_new_value(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {"config": {"api_key": "new-key"}})
    assert _stored(created.profile["id"])["config"]["api_key"] == "new-key"


def test_clear_secrets_clears_api_key_and_secret_key(ctx, tencent_env):
    created = ac.create_profile(_tencent_profile())
    ac.update_profile(created.profile["id"], {"clear_secrets": True})
    stored = _stored(created.profile["id"])
    assert stored["config"]["secret_key"] == ""
    assert stored["config"]["secret_id"] == "profile-id"


def test_url_change_drops_key_without_new_value(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {"config": {"base_url": "http://other.lan/v1"}})
    assert _stored(created.profile["id"])["config"]["api_key"] == ""


def test_url_change_keeps_new_key(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.update_profile(created.profile["id"], {
        "config": {"base_url": "http://other.lan/v1", "api_key": "new-key"}})
    stored = _stored(created.profile["id"])
    assert stored["config"]["base_url"] == "http://other.lan/v1"
    assert stored["config"]["api_key"] == "new-key"


def test_echoed_stored_key_is_not_new(ctx, clean_env):
    created = ac.create_profile(_profile())
    assert ac.new_secret("profile-key", "profile-key") == ""
    ac.update_profile(created.profile["id"], {
        "config": {"base_url": "http://other.lan/v1", "api_key": "profile-key"}})
    assert _stored(created.profile["id"])["config"]["api_key"] == ""


# ---------------------------------------------------------------- migration


def _set_legacy(visible=None, default=None):
    if visible is not None:
        SystemSetting.set_setting(
            "transcription_models_visible_json",
            json.dumps(visible) if not isinstance(visible, str) else visible)
    if default is not None:
        SystemSetting.set_setting("transcription_default_model", default)


def test_migrate_from_visible_json_dicts(ctx, clean_env):
    _set_legacy(visible=[{"value": "large-v3", "label": "Whisper Large v3"}])
    assert ac.ensure_migrated() is True
    models = ac.load_document()["models"]
    assert len(models) == 1
    assert models[0]["label"] == "Whisper Large v3"
    assert models[0]["config"]["model"] == "large-v3"
    assert models[0]["connector"] == "openai_whisper"
    assert models[0]["visible"] is True


def test_migrate_from_visible_json_strings(ctx, clean_env):
    _set_legacy(visible=["small"])
    ac.ensure_migrated()
    models = ac.load_document()["models"]
    assert models[0]["label"] == "OpenAI Whisper: small"
    assert models[0]["config"]["model"] == "small"


def test_migrate_from_env_options(ctx, clean_env, monkeypatch):
    import src.config.app_config as app_config
    monkeypatch.setattr(app_config, "TRANSCRIPTION_MODEL_OPTIONS", [
        {"value": "gpt-4o-transcribe", "label": "GPT-4o"}])
    ac.ensure_migrated()
    models = ac.load_document()["models"]
    assert [model["config"]["model"] for model in models] == ["gpt-4o-transcribe"]
    assert models[0]["label"] == "GPT-4o"


def test_migrate_dedupes_and_skips_environment_model(ctx, clean_env):
    _set_legacy(visible=["whisper-1", "large-v3", "large-v3", {"value": "small"}])
    ac.ensure_migrated()
    models = ac.load_document()["models"]
    assert [model["config"]["model"] for model in models] == ["large-v3", "small"]


def test_migrate_caps_at_limit_and_keeps_legacy_default(ctx, clean_env):
    values = [f"model-{index:02d}" for index in range(13)]
    _set_legacy(visible=values, default="model-12")
    ac.ensure_migrated()
    document = ac.load_document()
    assert len(document["models"]) == ac.MAX_MODELS
    assert any(model["config"]["model"] == "model-12" for model in document["models"])
    default = next(model for model in document["models"] if model["config"]["model"] == "model-12")
    assert document["default"] == default["id"]


def test_migrate_makes_labels_unique(ctx, clean_env):
    _set_legacy(visible=[
        {"value": "large-v3", "label": "Same"},
        {"value": "small", "label": "Same"},
    ])
    ac.ensure_migrated()
    labels = [model["label"] for model in ac.load_document()["models"]]
    assert labels == ["Same", "Same (2)"]


def test_migrate_maps_legacy_default_to_profile(ctx, clean_env):
    _set_legacy(visible=["large-v3", "small"], default="small")
    ac.ensure_migrated()
    document = ac.load_document()
    target = next(model for model in document["models"] if model["config"]["model"] == "small")
    assert document["default"] == target["id"]


def test_migrate_legacy_default_equal_to_env_model_gives_null(ctx, clean_env):
    _set_legacy(visible=["large-v3"], default="whisper-1")
    ac.ensure_migrated()
    assert ac.load_document()["default"] is None


def test_migrate_unlisted_default_stays_the_default(ctx, clean_env):
    """Before the upgrade a transcription without a choice ran on the saved
    default even when it was not in the visible list."""
    _set_legacy(visible=["large-v3"], default="distil-large-v3")
    ac.ensure_migrated()
    document = ac.load_document()
    target = next(model for model in document["models"]
                  if model["config"]["model"] == "distil-large-v3")
    assert document["default"] == target["id"]
    connector, model = ac.resolve_asr_connector(None)
    assert model == "distil-large-v3"


def test_migrate_keeps_the_environment_model_selectable_under_another_default(ctx, clean_env):
    """The visible list offered whisper-1 (the environment model) and
    gpt-4o-transcribe with gpt-4o-transcribe as the default: whisper-1 must
    stay in the dropdown, since "use the default" now means gpt-4o."""
    _set_legacy(visible=[{"value": "whisper-1", "label": "Whisper"},
                         {"value": "gpt-4o-transcribe", "label": "GPT-4o"}],
                default="gpt-4o-transcribe")
    ac.ensure_migrated()
    document = ac.load_document()
    assert [(m["label"], m["config"]["model"]) for m in document["models"]] == [
        ("Whisper", "whisper-1"), ("GPT-4o", "gpt-4o-transcribe")]
    gpt = next(m for m in document["models"] if m["config"]["model"] == "gpt-4o-transcribe")
    assert document["default"] == gpt["id"]
    assert [m["label"] for m in ac.visible_models()] == ["Whisper", "GPT-4o"]


def test_migrate_saves_empty_document_when_nothing_to_migrate(ctx, clean_env):
    assert ac.ensure_migrated() is True
    assert ac.load_document() == {"version": 1, "models": [], "default": None}


def test_migrate_is_idempotent(ctx, clean_env):
    _set_legacy(visible=["large-v3"])
    assert ac.ensure_migrated() is True
    before = ac.load_document()
    _set_legacy(visible=["small"])
    assert ac.ensure_migrated() is False
    assert ac.load_document() == before


def test_migrate_keeps_legacy_keys(ctx, clean_env):
    _set_legacy(visible=["large-v3"], default="large-v3")
    ac.ensure_migrated()
    assert SystemSetting.get_setting("transcription_models_visible_json", None)
    assert SystemSetting.get_setting("transcription_default_model", None) == "large-v3"


# ---------------------------------------------------------------- environment


def test_environment_profile_id_is_env_connector_model(clean_env):
    profile = ac.environment_profile()
    assert profile["id"] == "env:openai-whisper:whisper-1"
    assert re.fullmatch(r"env:[a-z0-9-]+:.*", profile["id"])
    assert not profile["id"].startswith("m_")


def test_env_connector_name_maps_known_connectors():
    assert ac.env_connector_name("openai_whisper") == "openai-whisper"
    assert ac.env_connector_name("openai_transcribe") == "openai-transcribe"
    assert ac.env_connector_name("asr_endpoint") == "whisper-asr"
    assert ac.env_connector_name("azure_openai_transcribe") == "azure-openai-transcribe"
    assert ac.env_connector_name("tencent_asr") == "tencent-asr"
    assert ac.env_connector_name("vibevoice") == "vibevoice"


def test_environment_profile_id_lowercases_model(clean_env):
    clean_env.setenv("TRANSCRIPTION_MODEL", "Whisper-1")
    profile = ac.environment_profile()
    assert profile["id"] == "env:openai-whisper:whisper-1"


def test_connector_from_env_name_round_trips(clean_env):
    from src.services.transcription.registry import ConnectorRegistry
    for item in ConnectorRegistry().list_connectors():
        env_name = ac.env_connector_name(item["name"])
        assert ac.connector_from_env_name(env_name) == item["name"]
        assert ac.connector_from_env_name(item["name"]) == item["name"]
    assert ac.connector_from_env_name("nope") is None


def test_environment_profile_label_is_connector_colon_model(clean_env):
    profile = ac.environment_profile()
    assert profile["label"] == "openai-whisper:whisper-1"
    assert profile["id"] == f"env:{profile['label']}"


def test_environment_profile_is_marked_environment(clean_env):
    profile = ac.environment_profile()
    assert profile["environment"] is True
    assert profile["visible"] is True


def test_resolve_environment_id_with_slug(ctx, clean_env):
    connector, model = ac.resolve_asr_connector("env:openai-whisper:whisper-1")
    assert connector.PROVIDER_NAME == "openai_whisper"
    assert connector.config["api_key"] == "env-key"
    assert model == "whisper-1"


def test_import_from_environment_creates_m_profile(ctx, clean_env):
    created = ac.import_from_environment()
    assert re.fullmatch(r"m_[0-9a-f]{8}", created.profile["id"])
    assert created.profile["label"] == "openai-whisper:whisper-1"


def test_import_from_environment_returns_existing_identical_profile(ctx, clean_env):
    first = ac.import_from_environment()
    second = ac.import_from_environment()
    assert second.profile["id"] == first.profile["id"]
    assert len(ac.load_document()["models"]) == 1


def test_import_from_environment_without_env_raises(ctx, monkeypatch):
    monkeypatch.setattr(ac, "environment_profile", lambda **_: None)
    with pytest.raises(ac.ASRConfigError):
        ac.import_from_environment()


# ---------------------------------------------------------------- resolution


def test_resolve_profile_id(ctx, clean_env):
    created = ac.create_profile(_profile())
    connector, model = ac.resolve_asr_connector(created.profile["id"])
    assert connector.PROVIDER_NAME == "openai_whisper"
    assert connector.config["api_key"] == "profile-key"
    assert model == "whisper-1"


def test_resolve_environment_id(ctx, clean_env):
    connector, _ = ac.resolve_asr_connector("env:openai-whisper:whisper-1")
    assert connector.config["api_key"] == "env-key"


def test_resolve_legacy_model_name_uses_environment_credentials(ctx, clean_env):
    ac.create_profile(_profile())
    connector, model = ac.resolve_asr_connector("large-v3")
    assert connector.PROVIDER_NAME == "openai_whisper"
    assert connector.config["api_key"] == "env-key"
    assert model == "large-v3"


def test_resolve_unknown_profile_falls_back_to_environment(ctx, clean_env):
    connector, model = ac.resolve_asr_connector("m_deadbeef")
    assert connector.config["api_key"] == "env-key"
    assert model == "m_deadbeef"


def test_resolve_none_uses_explicit_default(ctx, clean_env):
    created = ac.create_profile(_profile())
    ac.set_default(created.profile["id"])
    connector, model = ac.resolve_asr_connector(None)
    assert connector.config["api_key"] == "profile-key"
    assert model == "whisper-1"


def test_resolve_none_with_profiles_but_no_default_uses_environment(ctx, clean_env):
    ac.create_profile(_profile())
    connector, _ = ac.resolve_asr_connector(None)
    assert connector.config["api_key"] == "env-key"


def test_hidden_profile_still_resolves_by_id(ctx, clean_env):
    created = ac.create_profile(_profile(visible=False))
    connector, _ = ac.resolve_asr_connector(created.profile["id"])
    assert connector.config["api_key"] == "profile-key"


def test_removed_llm_style_aliases_do_not_exist():
    for name in (
        "list_profiles", "create_model", "update_model", "delete_model",
        "set_default_profile", "set_default_model", "assign_default",
        "mark_default", "get_default_profile_id", "get_default", "get_default_model",
        "import_from_env", "import_environment", "env_profiles",
        "list_environment_profiles",
    ):
        assert not hasattr(ac, name), f"{name} should be gone"
    assert not hasattr(ac, "list")


# ------------------------------------------------- migration retries and races


def test_migration_waits_when_the_environment_cannot_be_read(ctx, clean_env, monkeypatch, caplog):
    """A first start that cannot read the environment provider stores nothing,
    so the legacy list is migrated at the next start instead of being lost."""
    _set_legacy(visible=["large-v3"], default="large-v3")
    real = ac.environment_profile
    monkeypatch.setattr(ac, "environment_profile", lambda **_: None)
    with caplog.at_level("WARNING", logger="src.services.asr_config"):
        assert ac.ensure_migrated() is False
    assert SystemSetting.query.filter_by(key=ac.SETTING_KEY).first() is None
    assert "postponed" in caplog.text

    monkeypatch.setattr(ac, "environment_profile", real)
    assert ac.ensure_migrated() is True
    document = ac.load_document()
    assert [m["config"]["model"] for m in document["models"]] == ["large-v3"]
    assert document["default"] == document["models"][0]["id"]


def test_migration_failure_stores_nothing(ctx, clean_env, monkeypatch, caplog):
    _set_legacy(visible=["large-v3"])

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(ac, "_migrated_document", broken)
    with caplog.at_level("WARNING", logger="src.services.asr_config"):
        assert ac.ensure_migrated() is False
    assert SystemSetting.query.filter_by(key=ac.SETTING_KEY).first() is None
    assert "retried at the next start" in caplog.text


def test_a_second_worker_migrating_at_the_same_time_keeps_one_document(ctx, clean_env, monkeypatch):
    """Two gunicorn workers start together: the one that inserts second keeps
    the first one's document and raises nothing."""
    _set_legacy(visible=["large-v3"])
    other = json.dumps({"version": 1, "models": [], "default": None})
    real = ac._migrated_document

    def racing():
        document = real()
        with db.engine.begin() as connection:
            connection.execute(SystemSetting.__table__.insert().values(
                key=ac.SETTING_KEY, value=other, setting_type="string",
                description="other worker"))
        return document

    monkeypatch.setattr(ac, "_migrated_document", racing)
    assert ac.ensure_migrated() is False
    rows = SystemSetting.query.filter_by(key=ac.SETTING_KEY).all()
    assert len(rows) == 1
    assert rows[0].value == other


def test_environment_label_without_a_model_is_the_connector(clean_env):
    clean_env.delenv("TRANSCRIPTION_MODEL", raising=False)
    clean_env.setenv("TRANSCRIPTION_CONNECTOR", "asr_endpoint")
    clean_env.setenv("ASR_BASE_URL", "http://asr.test:9000")
    get_registry()._active_connector = None
    get_registry()._connector_name = ""
    profile = ac.environment_profile()
    assert profile["label"] == "whisper-asr"
    assert profile["id"] == "env:whisper-asr:"
