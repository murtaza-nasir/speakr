"""Schema coverage tests for transcription connectors."""

import re
from pathlib import Path

import pytest

from src.services.transcription.registry import ConnectorRegistry


CONNECTOR_FILES = {
    "openai_whisper": "openai_whisper.py",
    "openai_transcribe": "openai_transcribe.py",
    "asr_endpoint": "asr_endpoint.py",
    "azure_openai_transcribe": "azure_openai_transcribe.py",
    "mistral": "mistral.py",
    "vibevoice": "vibevoice.py",
    "funasr": "alibaba_funasr.py",
    "mossland": "mossland.py",
    "assemblyai": "assemblyai.py",
    "tencent_asr": "tencent_asr.py",
    "openasr": "openasr.py",
    "audiocpp": "audiocpp.py",
}

RUNTIME_ONLY = {"http_client"}
# Environment keys a connector receives but never uses: the WhisperX
# connector gets ASR_API_KEY as api_key and sends no key, so its schema has
# no key field.
UNUSED_ENV_KEYS = {("asr_endpoint", "api_key")}


@pytest.fixture(scope="module")
def registry():
    return ConnectorRegistry()


@pytest.mark.parametrize("connector_name", list(CONNECTOR_FILES))
def test_schema_covers_env_builder_keys(registry, connector_name):
    config = registry._build_config_from_env(connector_name)
    cls = registry.get_connector_class(connector_name)
    schema = cls.get_config_schema()
    props = schema.get("properties", {})
    for key in config:
        if (connector_name, key) in UNUSED_ENV_KEYS:
            continue
        assert key in props, f"{connector_name}: env key {key!r} missing from schema"


@pytest.mark.parametrize("connector_name", list(CONNECTOR_FILES))
def test_schema_covers_constructor_keys(connector_name):
    path = Path("src/services/transcription/connectors") / CONNECTOR_FILES[connector_name]
    text = path.read_text()
    keys = set(re.findall(r"config\.get\(\s*['\"]([^'\"]+)['\"]", text))
    keys |= set(re.findall(r"config\[\s*['\"]([^'\"]+)['\"]\s*\]", text))
    keys -= RUNTIME_ONLY
    cls = ConnectorRegistry().get_connector_class(connector_name)
    schema = cls.get_config_schema()
    props = schema.get("properties", {})
    for key in keys:
        assert key in props, f"{connector_name}: constructor key {key!r} missing from schema"


@pytest.mark.parametrize("connector_name", list(CONNECTOR_FILES))
def test_schema_properties_have_type_and_description(registry, connector_name):
    cls = registry.get_connector_class(connector_name)
    schema = cls.get_config_schema()
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert props
    for key, spec in props.items():
        assert "type" in spec, f"{connector_name}.{key} missing type"
        assert spec.get("description"), f"{connector_name}.{key} missing description"


@pytest.mark.parametrize("connector_name", list(CONNECTOR_FILES))
def test_schema_required_subset_of_properties(registry, connector_name):
    cls = registry.get_connector_class(connector_name)
    schema = cls.get_config_schema()
    props = set(schema.get("properties", {}))
    for key in schema.get("required", []):
        assert key in props, f"{connector_name}: required {key!r} not in properties"


def test_the_whisperx_schema_has_no_key_field(registry):
    """The WhisperX connector sends no key, so the form offers none."""
    props = registry.get_connector_class("asr_endpoint").get_config_schema()["properties"]
    assert "api_key" not in props


def test_whisperx_speaker_embeddings_are_off_unless_asked(registry):
    cls = registry.get_connector_class("asr_endpoint")
    assert cls.get_config_schema()["properties"]["return_speaker_embeddings"]["default"] is False
    connector = cls({"base_url": "http://asr.test:9000"})
    assert connector.return_embeddings is False


def test_a_whisperx_profile_never_holds_a_key(monkeypatch):
    from src.app import app
    from src.services import asr_config as ac

    monkeypatch.setenv("ASR_API_KEY", "unused-key")
    with app.app_context():
        stored = ac._stored_profile({
            "id": "m_00000001", "label": "Old", "connector": "asr_endpoint",
            "config": {"base_url": "http://asr.test:9000", "api_key": "old-key"}})
        assert "api_key" not in stored["config"]
        config = ac._drop_undeclared_secrets(
            "asr_endpoint", ConnectorRegistry()._build_config_from_env("asr_endpoint"))
        assert "api_key" not in config
