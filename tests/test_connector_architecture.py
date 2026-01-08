#!/usr/bin/env python3
"""
Test script for the transcription connector architecture.

This script tests:
1. Connector auto-detection from environment variables
2. Backwards compatibility with legacy config
3. Connector specifications and capabilities
4. Chunking logic (connector-aware)
5. Codec handling per connector
6. Request/Response data types

Run with: docker exec speakr-dev python /app/tests/test_connector_architecture.py
"""

import os
import sys
import io
import json
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Test results tracking
PASSED = 0
FAILED = 0
ERRORS = []


def run_test(name, func):
    """Run a test function and track results."""
    global PASSED, FAILED, ERRORS
    try:
        func()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILED += 1
        ERRORS.append((name, str(e)))
    except Exception as e:
        print(f"  ✗ {name}: EXCEPTION - {e}")
        FAILED += 1
        ERRORS.append((name, f"Exception: {e}"))


def clear_env():
    """Clear all transcription-related environment variables."""
    keys_to_clear = [
        'TRANSCRIPTION_CONNECTOR', 'TRANSCRIPTION_API_KEY', 'TRANSCRIPTION_BASE_URL',
        'TRANSCRIPTION_MODEL', 'WHISPER_MODEL', 'USE_ASR_ENDPOINT', 'ASR_BASE_URL',
        'ASR_DIARIZE', 'ASR_RETURN_SPEAKER_EMBEDDINGS', 'ASR_TIMEOUT',
        'ASR_MIN_SPEAKERS', 'ASR_MAX_SPEAKERS', 'ENABLE_CHUNKING', 'CHUNK_LIMIT',
        'CHUNK_OVERLAP_SECONDS', 'AUDIO_UNSUPPORTED_CODECS',
    ]
    for key in keys_to_clear:
        os.environ.pop(key, None)


def reset_registry():
    """Reset the connector registry singleton."""
    from src.services.transcription import registry
    registry._registry = None
    registry.ConnectorRegistry._instance = None
    registry.ConnectorRegistry._initialized = False
    registry.ConnectorRegistry._active_connector = None
    registry.ConnectorRegistry._connector_name = ""


# =============================================================================
# TEST SECTION 1: Base Classes and Data Types
# =============================================================================

def test_base_classes():
    """Test base classes and data types."""
    print("\n=== Testing Base Classes ===")

    from src.services.transcription.base import (
        TranscriptionCapability, ConnectorSpecifications, TranscriptionRequest,
        TranscriptionResponse, TranscriptionSegment,
    )

    def t1():
        assert TranscriptionCapability.DIARIZATION is not None
        assert TranscriptionCapability.TIMESTAMPS is not None
        assert TranscriptionCapability.SPEAKER_COUNT_CONTROL is not None
    run_test("TranscriptionCapability enum has expected values", t1)

    def t2():
        specs = ConnectorSpecifications()
        assert specs.max_file_size_bytes is None
        assert specs.handles_chunking_internally is False
        assert specs.recommended_chunk_seconds == 600
    run_test("ConnectorSpecifications has correct defaults", t2)

    def t3():
        specs = ConnectorSpecifications(
            max_file_size_bytes=25 * 1024 * 1024,
            handles_chunking_internally=True,
            unsupported_codecs=frozenset({'opus'})
        )
        assert specs.max_file_size_bytes == 25 * 1024 * 1024
        assert 'opus' in specs.unsupported_codecs
    run_test("ConnectorSpecifications with custom values", t3)

    def t4():
        audio = io.BytesIO(b"fake audio data")
        request = TranscriptionRequest(audio_file=audio, filename="test.wav", diarize=True)
        assert request.filename == "test.wav"
        assert request.diarize is True
    run_test("TranscriptionRequest creation", t4)

    def t5():
        segments = [
            TranscriptionSegment(text="Hello", speaker="SPEAKER_00", start_time=0.0, end_time=1.0),
            TranscriptionSegment(text="World", speaker="SPEAKER_01", start_time=1.0, end_time=2.0),
        ]
        response = TranscriptionResponse(text="Hello World", segments=segments, provider="test")
        storage = response.to_storage_format()
        data = json.loads(storage)
        assert len(data) == 2
        assert data[0]['speaker'] == "SPEAKER_00"
    run_test("TranscriptionResponse to_storage_format", t5)

    def t6():
        segments = [TranscriptionSegment(text="Hello", speaker="SPEAKER_00")]
        response = TranscriptionResponse(text="Hello", segments=segments)
        assert response.has_diarization() is True

        response2 = TranscriptionResponse(text="Hello", segments=None)
        assert response2.has_diarization() is False
    run_test("TranscriptionResponse has_diarization", t6)


# =============================================================================
# TEST SECTION 2: Connector Auto-Detection
# =============================================================================

def test_auto_detection():
    """Test connector auto-detection from environment variables."""
    print("\n=== Testing Connector Auto-Detection ===")

    from src.services.transcription.registry import get_registry

    def t1():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_CONNECTOR'] = 'openai_whisper'
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['ASR_BASE_URL'] = 'http://should-be-ignored:9000'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'openai_whisper'
    run_test("Explicit TRANSCRIPTION_CONNECTOR takes priority", t1)

    def t2():
        clear_env()
        reset_registry()
        os.environ['ASR_BASE_URL'] = 'http://whisperx:9000'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'asr_endpoint'
    run_test("ASR_BASE_URL auto-detects asr_endpoint", t2)

    def t3():
        clear_env()
        reset_registry()
        os.environ['USE_ASR_ENDPOINT'] = 'true'
        os.environ['ASR_BASE_URL'] = 'http://whisperx:9000'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'asr_endpoint'
    run_test("Legacy USE_ASR_ENDPOINT=true still works", t3)

    def t4():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['TRANSCRIPTION_MODEL'] = 'gpt-4o-transcribe-diarize'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'openai_transcribe'
    run_test("gpt-4o model auto-detects openai_transcribe", t4)

    def t5():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['TRANSCRIPTION_MODEL'] = 'whisper-1'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'openai_whisper'
    run_test("whisper-1 model uses openai_whisper", t5)

    def t6():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['WHISPER_MODEL'] = 'whisper-1'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'openai_whisper'
    run_test("Legacy WHISPER_MODEL still works", t6)

    def t7():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'openai_whisper'
    run_test("Default falls back to openai_whisper", t7)


# =============================================================================
# TEST SECTION 3: Connector Specifications
# =============================================================================

def test_connector_specifications():
    """Test connector specifications are correctly defined."""
    print("\n=== Testing Connector Specifications ===")

    from src.services.transcription.connectors.openai_whisper import OpenAIWhisperConnector
    from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
    from src.services.transcription.base import TranscriptionCapability

    def t1():
        specs = OpenAIWhisperConnector.SPECIFICATIONS
        assert specs.max_file_size_bytes == 25 * 1024 * 1024
        assert specs.handles_chunking_internally is False
    run_test("OpenAI Whisper has 25MB limit", t1)

    def t2():
        specs = OpenAIWhisperConnector.SPECIFICATIONS
        assert specs.unsupported_codecs is not None
        assert 'opus' in specs.unsupported_codecs
    run_test("OpenAI Whisper declares opus as unsupported", t2)

    def t3():
        specs = OpenAITranscribeConnector.SPECIFICATIONS
        assert specs.handles_chunking_internally is True
        assert specs.requires_chunking_param is True
    run_test("OpenAI Transcribe has internal chunking", t3)

    def t4():
        specs = ASREndpointConnector.SPECIFICATIONS
        assert specs.max_file_size_bytes is None
        assert specs.handles_chunking_internally is True
    run_test("ASR Endpoint has no limits (handles internally)", t4)

    def t5():
        assert TranscriptionCapability.DIARIZATION not in OpenAIWhisperConnector.CAPABILITIES
    run_test("OpenAI Whisper does NOT support diarization", t5)

    def t6():
        # Diarization is added dynamically based on model at instance level
        connector = OpenAITranscribeConnector({'api_key': 'test', 'model': 'gpt-4o-transcribe-diarize'})
        assert TranscriptionCapability.DIARIZATION in connector.CAPABILITIES
        assert connector.supports_diarization is True
    run_test("OpenAI Transcribe with diarize model supports diarization", t6)

    def t7():
        assert TranscriptionCapability.DIARIZATION in ASREndpointConnector.CAPABILITIES
        assert TranscriptionCapability.SPEAKER_COUNT_CONTROL in ASREndpointConnector.CAPABILITIES
    run_test("ASR Endpoint supports diarization and speaker count control", t7)

    def t8():
        assert TranscriptionCapability.SPEAKER_COUNT_CONTROL not in OpenAIWhisperConnector.CAPABILITIES
        assert TranscriptionCapability.SPEAKER_COUNT_CONTROL not in OpenAITranscribeConnector.CAPABILITIES
    run_test("OpenAI connectors do NOT support speaker count control", t8)


# =============================================================================
# TEST SECTION 4: Chunking Logic
# =============================================================================

def test_chunking_logic():
    """Test connector-aware chunking logic."""
    print("\n=== Testing Chunking Logic ===")

    from src.audio_chunking import get_effective_chunking_config
    from src.services.transcription.base import ConnectorSpecifications

    def t1():
        clear_env()
        os.environ['ENABLE_CHUNKING'] = 'true'
        os.environ['CHUNK_LIMIT'] = '20MB'
        specs = ConnectorSpecifications(handles_chunking_internally=True)
        config = get_effective_chunking_config(specs)
        assert config.enabled is False
        assert config.source == 'connector_internal'
    run_test("Connector with internal chunking disables app chunking", t1)

    def t2():
        clear_env()
        os.environ['ENABLE_CHUNKING'] = 'true'
        os.environ['CHUNK_LIMIT'] = '15MB'
        os.environ['CHUNK_OVERLAP_SECONDS'] = '5'
        specs = ConnectorSpecifications(handles_chunking_internally=False)
        config = get_effective_chunking_config(specs)
        assert config.enabled is True
        assert config.source == 'env'
        assert config.mode == 'size'
        assert config.limit_value == 15.0
    run_test("Connector without internal chunking uses ENV settings", t2)

    def t3():
        clear_env()
        os.environ['ENABLE_CHUNKING'] = 'false'
        specs = ConnectorSpecifications(handles_chunking_internally=False)
        config = get_effective_chunking_config(specs)
        assert config.enabled is False
        assert config.source == 'disabled'
    run_test("ENABLE_CHUNKING=false disables chunking", t3)

    def t4():
        clear_env()
        os.environ['ENABLE_CHUNKING'] = 'true'
        os.environ['CHUNK_LIMIT'] = '10m'
        specs = ConnectorSpecifications(handles_chunking_internally=False)
        config = get_effective_chunking_config(specs)
        assert config.enabled is True
        assert config.mode == 'duration'
        assert config.limit_value == 600.0
    run_test("Duration-based chunk limit parsing (10m = 600s)", t4)


# =============================================================================
# TEST SECTION 5: Codec Handling
# =============================================================================

def test_codec_handling():
    """Test codec handling with connector specifications."""
    print("\n=== Testing Codec Handling ===")

    from src.services.transcription.base import ConnectorSpecifications

    def reload_audio_module():
        """Properly reload audio_conversion module with fresh env vars."""
        import sys
        # Remove relevant modules from cache to force fresh import
        # app_config reads AUDIO_UNSUPPORTED_CODECS at import time
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith('src.utils') or mod_name.startswith('src.config'):
                del sys.modules[mod_name]
        from src.utils import audio_conversion
        return audio_conversion

    def t1():
        clear_env()
        mod = reload_audio_module()
        codecs = mod.get_supported_codecs()
        assert 'mp3' in codecs
        assert 'flac' in codecs
    run_test("Default supported codecs include common formats", t1)

    def t2():
        clear_env()
        mod = reload_audio_module()
        specs = ConnectorSpecifications(unsupported_codecs=frozenset({'opus', 'vorbis'}))
        codecs = mod.get_supported_codecs(connector_specs=specs)
        assert 'opus' not in codecs
        assert 'vorbis' not in codecs
        assert 'mp3' in codecs
    run_test("Connector unsupported_codecs removes from defaults", t2)

    def t3():
        clear_env()
        os.environ['AUDIO_UNSUPPORTED_CODECS'] = 'aac,opus'
        mod = reload_audio_module()
        codecs = mod.get_supported_codecs()
        assert 'aac' not in codecs, f"aac should not be in {codecs}"
        assert 'opus' not in codecs, f"opus should not be in {codecs}"
    run_test("AUDIO_UNSUPPORTED_CODECS env var still works", t3)

    def t4():
        clear_env()
        os.environ['AUDIO_UNSUPPORTED_CODECS'] = 'aac'
        mod = reload_audio_module()
        specs = ConnectorSpecifications(unsupported_codecs=frozenset({'opus'}))
        codecs = mod.get_supported_codecs(connector_specs=specs)
        assert 'aac' not in codecs, f"aac should not be in {codecs}"
        assert 'opus' not in codecs, f"opus should not be in {codecs}"
        assert 'mp3' in codecs
    run_test("Both connector specs and ENV var work together", t4)


# =============================================================================
# TEST SECTION 6: Connector Capabilities
# =============================================================================

def test_connector_capabilities():
    """Test connector capabilities are exposed correctly."""
    print("\n=== Testing Connector Capabilities ===")

    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
    from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector
    from src.services.transcription.base import TranscriptionCapability

    def t1():
        connector = ASREndpointConnector({'base_url': 'http://test:9000'})
        assert connector.supports_diarization is True
    run_test("ASR connector supports_diarization property", t1)

    def t2():
        connector = ASREndpointConnector({'base_url': 'http://test:9000'})
        assert connector.supports_speaker_count_control is True
    run_test("ASR connector supports_speaker_count_control property", t2)

    def t3():
        connector = OpenAITranscribeConnector({'api_key': 'test-key', 'model': 'gpt-4o-transcribe-diarize'})
        assert connector.supports_diarization is True
        assert connector.supports_speaker_count_control is False
    run_test("OpenAI Transcribe supports diarization but not speaker_count_control", t3)

    def t4():
        connector = ASREndpointConnector({'base_url': 'http://test:9000'})
        assert connector.supports(TranscriptionCapability.DIARIZATION) is True
        assert connector.supports(TranscriptionCapability.STREAMING) is False
    run_test("supports() method works correctly", t4)


# =============================================================================
# TEST SECTION 7: Registry Operations
# =============================================================================

def test_registry_operations():
    """Test registry listing and connector info."""
    print("\n=== Testing Registry Operations ===")

    from src.services.transcription.registry import get_registry

    def t1():
        clear_env()
        reset_registry()
        registry = get_registry()
        connectors = registry.list_connectors()
        names = [c['name'] for c in connectors]
        assert 'openai_whisper' in names
        assert 'openai_transcribe' in names
        assert 'asr_endpoint' in names
    run_test("Registry lists all built-in connectors", t1)

    def t2():
        clear_env()
        reset_registry()
        registry = get_registry()
        connectors = registry.list_connectors()
        asr = next(c for c in connectors if c['name'] == 'asr_endpoint')
        assert 'DIARIZATION' in asr['capabilities']
        assert 'SPEAKER_COUNT_CONTROL' in asr['capabilities']
    run_test("Connector info includes capabilities", t2)

    def t3():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['TRANSCRIPTION_MODEL'] = 'whisper-1'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'openai_whisper'

        os.environ['TRANSCRIPTION_MODEL'] = 'gpt-4o-transcribe-diarize'
        registry.reinitialize()
        assert registry.get_active_connector_name() == 'openai_transcribe'
    run_test("reinitialize() resets the active connector", t3)


# =============================================================================
# TEST SECTION 8: Edge Cases
# =============================================================================

def test_edge_cases():
    """Test edge cases and error handling."""
    print("\n=== Testing Edge Cases ===")

    from src.services.transcription.registry import get_registry
    from src.services.transcription.exceptions import ConfigurationError
    from src.services.transcription.base import TranscriptionResponse, TranscriptionSegment

    def t1():
        # Empty segments list returns the text (empty string), not "[]"
        response = TranscriptionResponse(text="", segments=[], provider="test")
        assert response.to_storage_format() == ""
        assert response.has_diarization() is False
    run_test("Empty transcription response handling", t1)

    def t2():
        segments = [TranscriptionSegment(text="Hello", speaker=None)]
        response = TranscriptionResponse(text="Hello", segments=segments)
        storage = response.to_storage_format()
        data = json.loads(storage)
        assert data[0]['speaker'] == 'Unknown Speaker'
    run_test("Transcription with unknown speaker handling", t2)

    def t3():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_CONNECTOR'] = 'nonexistent_connector'
        registry = get_registry()
        try:
            registry.initialize_from_env()
            assert False, "Should have raised ConfigurationError"
        except ConfigurationError as e:
            assert 'Unknown connector' in str(e)
    run_test("Invalid connector name raises ConfigurationError", t3)

    def t4():
        from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
        try:
            ASREndpointConnector({})
            assert False, "Should have raised ConfigurationError"
        except ConfigurationError as e:
            assert 'base_url' in str(e)
    run_test("ASR connector validates base_url is required", t4)

    def t5():
        clear_env()
        reset_registry()
        os.environ['ASR_BASE_URL'] = 'http://whisperx:9000  # This is a comment'
        registry = get_registry()
        connector = registry.initialize_from_env()
        assert connector.base_url == 'http://whisperx:9000'
    run_test("ASR_BASE_URL with trailing comment is handled", t5)


# =============================================================================
# Main
# =============================================================================

def main():
    """Run all tests."""
    global PASSED, FAILED, ERRORS

    print("=" * 60)
    print("Transcription Connector Architecture Tests")
    print("=" * 60)

    test_base_classes()
    test_auto_detection()
    test_connector_specifications()
    test_chunking_logic()
    test_codec_handling()
    test_connector_capabilities()
    test_registry_operations()
    test_edge_cases()

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=" * 60)

    if ERRORS:
        print("\nFailed tests:")
        for name, error in ERRORS:
            print(f"  - {name}: {error}")

    clear_env()
    return 0 if FAILED == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
