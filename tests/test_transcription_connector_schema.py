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
}

RUNTIME_ONLY = {"http_client"}


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
