#!/usr/bin/env python3
"""
Tests for the Mistral/Voxtral transcription connector.

Validates:
1. Connector initialization and configuration validation
2. Capabilities and specifications
3. Segment parsing (speaker_id mapping, confidence/score)
4. Context bias (hotwords) splitting
5. Request building (diarize, language, timestamps)
6. Error handling
7. Registry integration

Run with: docker exec speakr-dev python /app/tests/test_mistral_connector.py
"""

import os
import sys
import io
import json
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Test results tracking
PASSED = 0
FAILED = 0
ERRORS = []


def run_test(name, func):
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


# =============================================================================
# TEST SECTION 1: Initialization and Configuration
# =============================================================================

def test_initialization():
    """Test connector initialization and config validation."""
    print("\n=== Testing Initialization ===")

    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
    from src.services.transcription.exceptions import ConfigurationError

    def t1():
        connector = MistralTranscriptionConnector({
            'api_key': 'test-key-123',
            'base_url': 'https://api.mistral.ai',
            'model': 'voxtral-mini-latest',
        })
        assert connector.api_key == 'test-key-123'
        assert connector.base_url == 'https://api.mistral.ai'
        assert connector.model == 'voxtral-mini-latest'
    run_test("Basic initialization with all config", t1)

    def t2():
        connector = MistralTranscriptionConnector({
            'api_key': 'test-key',
        })
        assert connector.base_url == 'https://api.mistral.ai'
        assert connector.model == 'voxtral-mini-latest'
    run_test("Default base_url and model when not provided", t2)

    def t3():
        connector = MistralTranscriptionConnector({
            'api_key': 'test-key',
            'base_url': 'https://custom.endpoint.com/',
        })
        assert connector.base_url == 'https://custom.endpoint.com'  # Trailing slash stripped
    run_test("Trailing slash stripped from base_url", t3)

    def t4():
        connector = MistralTranscriptionConnector({
            'api_key': 'test-key',
            'base_url': '',  # Empty string should fall back to default
        })
        assert connector.base_url == 'https://api.mistral.ai'
    run_test("Empty base_url falls back to default", t4)

    def t5():
        try:
            MistralTranscriptionConnector({'api_key': ''})
            assert False, "Should have raised ConfigurationError"
        except ConfigurationError as e:
            assert 'api_key' in str(e)
    run_test("Empty api_key raises ConfigurationError", t5)

    def t6():
        try:
            MistralTranscriptionConnector({})
            assert False, "Should have raised ConfigurationError"
        except (ConfigurationError, KeyError):
            pass  # Either is acceptable
    run_test("Missing api_key raises error", t6)


# =============================================================================
# TEST SECTION 2: Capabilities and Specifications
# =============================================================================

def test_capabilities():
    """Test connector capabilities and specifications."""
    print("\n=== Testing Capabilities ===")

    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
    from src.services.transcription.base import TranscriptionCapability

    def t1():
        assert TranscriptionCapability.DIARIZATION in MistralTranscriptionConnector.CAPABILITIES
    run_test("Supports diarization", t1)

    def t2():
        assert TranscriptionCapability.TIMESTAMPS in MistralTranscriptionConnector.CAPABILITIES
    run_test("Supports timestamps", t2)

    def t3():
        assert TranscriptionCapability.LANGUAGE_DETECTION in MistralTranscriptionConnector.CAPABILITIES
    run_test("Supports language detection", t3)

    def t4():
        assert TranscriptionCapability.SPEAKER_COUNT_CONTROL not in MistralTranscriptionConnector.CAPABILITIES
    run_test("Does NOT support speaker count control (Mistral doesn't have min/max speakers)", t4)

    def t5():
        specs = MistralTranscriptionConnector.SPECIFICATIONS
        assert specs.handles_chunking_internally is True
    run_test("Handles chunking internally", t5)

    def t6():
        specs = MistralTranscriptionConnector.SPECIFICATIONS
        assert specs.max_duration_seconds is None
    run_test("No max_duration_seconds (avoids triggering app-level chunking)", t6)

    def t7():
        specs = MistralTranscriptionConnector.SPECIFICATIONS
        assert specs.recommended_chunk_seconds == 0
    run_test("recommended_chunk_seconds is 0 (disabled)", t7)

    def t8():
        specs = MistralTranscriptionConnector.SPECIFICATIONS
        assert specs.max_file_size_bytes is None
    run_test("No file size limit", t8)

    def t9():
        connector = MistralTranscriptionConnector({'api_key': 'test-key'})
        assert connector.supports_diarization is True
    run_test("Instance supports_diarization property", t9)

    def t10():
        connector = MistralTranscriptionConnector({'api_key': 'test-key'})
        assert connector.PROVIDER_NAME == 'mistral'
    run_test("Provider name is 'mistral'", t10)


# =============================================================================
# TEST SECTION 3: Segment Parsing
# =============================================================================

def test_segment_parsing():
    """Test _parse_segments with various Mistral response formats."""
    print("\n=== Testing Segment Parsing ===")

    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector

    connector = MistralTranscriptionConnector({'api_key': 'test-key'})

    def t1():
        """Mistral uses speaker_id, not speaker."""
        segments = connector._parse_segments([
            {'text': 'Bonjour', 'speaker_id': 'speaker_0', 'start': 0.0, 'end': 1.5, 'score': 0.95},
            {'text': 'Salut', 'speaker_id': 'speaker_1', 'start': 1.5, 'end': 3.0, 'score': 0.88},
        ])
        assert len(segments) == 2
        assert segments[0].speaker == 'speaker_0'
        assert segments[1].speaker == 'speaker_1'
    run_test("speaker_id field maps to speaker", t1)

    def t2():
        """Fallback to 'speaker' key if speaker_id is absent."""
        segments = connector._parse_segments([
            {'text': 'Hello', 'speaker': 'SPEAKER_00', 'start': 0.0, 'end': 1.0},
        ])
        assert segments[0].speaker == 'SPEAKER_00'
    run_test("Falls back to 'speaker' key if speaker_id missing", t2)

    def t3():
        """Score field maps to confidence."""
        segments = connector._parse_segments([
            {'text': 'Test', 'score': 0.92, 'start': 0.0, 'end': 1.0},
        ])
        assert segments[0].confidence == 0.92
    run_test("score field maps to confidence", t3)

    def t4():
        """Timestamps are correctly parsed."""
        segments = connector._parse_segments([
            {'text': 'Timestamp test', 'start': 5.5, 'end': 10.2},
        ])
        assert segments[0].start_time == 5.5
        assert segments[0].end_time == 10.2
    run_test("start/end timestamps are parsed", t4)

    def t5():
        """Handles missing optional fields gracefully."""
        segments = connector._parse_segments([
            {'text': 'Minimal segment'},
        ])
        assert len(segments) == 1
        assert segments[0].text == 'Minimal segment'
        assert segments[0].speaker is None
        assert segments[0].start_time is None
        assert segments[0].confidence is None
    run_test("Missing optional fields default to None", t5)

    def t6():
        """Empty segments list returns empty list."""
        segments = connector._parse_segments([])
        assert segments == []
    run_test("Empty segments list returns empty", t6)


# =============================================================================
# TEST SECTION 4: Hotwords / Context Bias Splitting
# =============================================================================

def test_hotwords_splitting():
    """Test context_bias generation from hotwords string."""
    print("\n=== Testing Hotwords Splitting ===")

    def split_hotwords(hotwords_str):
        """Replicate the connector's splitting logic."""
        return [w for w in re.split(r'[,\s]+', hotwords_str) if w]

    def t1():
        result = split_hotwords("Speakr, Voxtral, PyAnnote")
        assert result == ['Speakr', 'Voxtral', 'PyAnnote']
    run_test("Comma-separated hotwords split correctly", t1)

    def t2():
        result = split_hotwords("Speakr Voxtral PyAnnote")
        assert result == ['Speakr', 'Voxtral', 'PyAnnote']
    run_test("Space-separated hotwords split correctly", t2)

    def t3():
        result = split_hotwords("Speakr,Voxtral,PyAnnote")
        assert result == ['Speakr', 'Voxtral', 'PyAnnote']
    run_test("Comma-only (no spaces) hotwords split correctly", t3)

    def t4():
        result = split_hotwords("  Speakr ,  Voxtral  , PyAnnote  ")
        assert result == ['Speakr', 'Voxtral', 'PyAnnote']
    run_test("Extra whitespace is trimmed", t4)

    def t5():
        result = split_hotwords("")
        assert result == []
    run_test("Empty string produces empty list", t5)

    def t6():
        result = split_hotwords("SingleWord")
        assert result == ['SingleWord']
    run_test("Single word produces one-item list", t6)

    def t7():
        """Each item must match ^[^,\s]+$ per Mistral spec."""
        result = split_hotwords("hello world, foo bar, baz")
        for item in result:
            assert re.match(r'^[^,\s]+$', item), f"'{item}' contains comma or whitespace"
    run_test("All split items conform to Mistral's single-token pattern", t7)


# =============================================================================
# TEST SECTION 5: Request Building
# =============================================================================

def test_request_building():
    """Test that transcribe() builds the correct multipart request."""
    print("\n=== Testing Request Building ===")

    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
    from src.services.transcription.base import TranscriptionRequest

    connector = MistralTranscriptionConnector({'api_key': 'test-key'})

    def make_request(**kwargs):
        defaults = {
            'audio_file': io.BytesIO(b'fake audio'),
            'filename': 'test.wav',
        }
        defaults.update(kwargs)
        return TranscriptionRequest(**defaults)

    def t1():
        """Verify diarize and timestamp_granularities are always sent together."""
        captured = {}

        def mock_post(url, **kwargs):
            captured['url'] = url
            captured['files'] = kwargs.get('files', [])
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'text': 'test', 'segments': []}
            return mock_resp

        connector.client.post = mock_post
        req = make_request(diarize=True)
        connector.transcribe(req)

        field_names = [f[0] for f in captured['files']]
        assert 'diarize' in field_names, f"diarize not in fields: {field_names}"
        assert 'timestamp_granularities' in field_names, f"timestamp_granularities not in fields: {field_names}"
    run_test("Diarize sends both diarize and timestamp_granularities", t1)

    def t2():
        """Language is passed through when provided."""
        captured = {}

        def mock_post(url, **kwargs):
            captured['files'] = kwargs.get('files', [])
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'text': 'test', 'segments': []}
            return mock_resp

        connector.client.post = mock_post
        req = make_request(language='fr')
        connector.transcribe(req)

        fields = {f[0]: f[1] for f in captured['files'] if f[0] != 'file'}
        assert 'language' in fields
        # Value is a tuple (None, 'fr') for multipart encoding
        assert fields['language'] == (None, 'fr')
    run_test("Language parameter is sent when provided", t2)

    def t3():
        """Hotwords are sent as individual context_bias fields."""
        captured = {}

        def mock_post(url, **kwargs):
            captured['files'] = kwargs.get('files', [])
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'text': 'test', 'segments': []}
            return mock_resp

        connector.client.post = mock_post
        req = make_request(hotwords='Speakr, Voxtral, Python')
        connector.transcribe(req)

        context_bias_fields = [(name, val) for name, val in captured['files'] if name == 'context_bias']
        assert len(context_bias_fields) == 3, f"Expected 3 context_bias fields, got {len(context_bias_fields)}"
    run_test("Hotwords are sent as individual context_bias fields", t3)

    def t4():
        """Model is always sent."""
        captured = {}

        def mock_post(url, **kwargs):
            captured['files'] = kwargs.get('files', [])
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'text': 'test', 'segments': []}
            return mock_resp

        connector.client.post = mock_post
        req = make_request()
        connector.transcribe(req)

        field_names = [f[0] for f in captured['files']]
        assert 'model' in field_names
    run_test("Model is always included in request", t4)

    def t5():
        """Prompt parameter is ignored (Mistral doesn't support it)."""
        captured = {}

        def mock_post(url, **kwargs):
            captured['files'] = kwargs.get('files', [])
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'text': 'test', 'segments': []}
            return mock_resp

        connector.client.post = mock_post
        req = make_request(prompt='This is a prompt')
        connector.transcribe(req)

        field_names = [f[0] for f in captured['files']]
        assert 'prompt' not in field_names
        assert 'initial_prompt' not in field_names
    run_test("Prompt parameter is NOT sent to Mistral", t5)


# =============================================================================
# TEST SECTION 6: Response Parsing
# =============================================================================

def test_response_parsing():
    """Test full response parsing from a mock Mistral API response."""
    print("\n=== Testing Response Parsing ===")

    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
    from src.services.transcription.base import TranscriptionRequest

    connector = MistralTranscriptionConnector({'api_key': 'test-key'})

    def t1():
        """Full diarized response with speaker_id and score."""
        mock_response = {
            'text': 'Bonjour comment allez-vous? Très bien merci.',
            'language': 'fr',
            'segments': [
                {'text': 'Bonjour comment allez-vous?', 'speaker_id': 'speaker_0', 'start': 0.0, 'end': 2.5, 'score': 0.95},
                {'text': 'Très bien merci.', 'speaker_id': 'speaker_1', 'start': 2.5, 'end': 4.0, 'score': 0.88},
            ],
            'usage': {'prompt_tokens': 100, 'total_tokens': 100},
        }

        def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_response
            return resp

        connector.client.post = mock_post
        req = TranscriptionRequest(audio_file=io.BytesIO(b'fake'), filename='test.wav', diarize=True)
        result = connector.transcribe(req)

        assert result.text == 'Bonjour comment allez-vous? Très bien merci.'
        assert result.language == 'fr'
        assert result.provider == 'mistral'
        assert len(result.segments) == 2
        assert result.segments[0].speaker == 'speaker_0'
        assert result.segments[1].speaker == 'speaker_1'
        assert result.segments[0].confidence == 0.95
        assert result.speakers == ['speaker_0', 'speaker_1']
    run_test("Full diarized response parsed correctly", t1)

    def t2():
        """Response without segments (plain text only)."""
        mock_response = {
            'text': 'Hello world',
            'language': 'en',
        }

        def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_response
            return resp

        connector.client.post = mock_post
        req = TranscriptionRequest(audio_file=io.BytesIO(b'fake'), filename='test.wav')
        result = connector.transcribe(req)

        assert result.text == 'Hello world'
        assert result.segments == []
        assert result.speakers is None
    run_test("Plain text response (no segments) parsed correctly", t2)

    def t3():
        """has_diarization() returns True only when segments have speakers."""
        mock_response = {
            'text': 'Test',
            'segments': [
                {'text': 'Test', 'speaker_id': 'speaker_0', 'start': 0.0, 'end': 1.0},
            ],
        }

        def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_response
            return resp

        connector.client.post = mock_post
        req = TranscriptionRequest(audio_file=io.BytesIO(b'fake'), filename='test.wav', diarize=True)
        result = connector.transcribe(req)
        assert result.has_diarization() is True
    run_test("has_diarization() returns True for diarized response", t3)


# =============================================================================
# TEST SECTION 7: Error Handling
# =============================================================================

def test_error_handling():
    """Test error handling for API failures."""
    print("\n=== Testing Error Handling ===")

    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
    from src.services.transcription.base import TranscriptionRequest
    from src.services.transcription.exceptions import ProviderError, TranscriptionError

    connector = MistralTranscriptionConnector({'api_key': 'test-key'})

    def t1():
        """Non-200 response raises ProviderError."""
        def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 422
            resp.text = '{"detail": "Invalid request"}'
            resp.json.return_value = {'detail': 'Invalid request'}
            return resp

        connector.client.post = mock_post
        req = TranscriptionRequest(audio_file=io.BytesIO(b'fake'), filename='test.wav')

        try:
            connector.transcribe(req)
            assert False, "Should have raised ProviderError"
        except ProviderError as e:
            assert e.provider == 'mistral'
            assert e.status_code == 422
    run_test("API 422 error raises ProviderError with details", t1)

    def t2():
        """Timeout raises TranscriptionError."""
        import httpx

        def mock_post(url, **kwargs):
            raise httpx.TimeoutException("Connection timed out")

        connector.client.post = mock_post
        req = TranscriptionRequest(audio_file=io.BytesIO(b'fake'), filename='test.wav')

        try:
            connector.transcribe(req)
            assert False, "Should have raised TranscriptionError"
        except TranscriptionError as e:
            assert 'timed out' in str(e)
    run_test("Timeout raises TranscriptionError", t2)

    def t3():
        """Health check returns True with valid api_key."""
        connector2 = MistralTranscriptionConnector({'api_key': 'valid-key'})
        assert connector2.health_check() is True
    run_test("health_check() returns True with valid key", t3)

    def t4():
        """Health check returns False with empty api_key (if bypassing validation)."""
        # Directly set empty key to test health_check logic
        connector2 = MistralTranscriptionConnector.__new__(MistralTranscriptionConnector)
        connector2.config = {'api_key': ''}
        assert connector2.health_check() is False
    run_test("health_check() returns False with empty key", t4)


# =============================================================================
# TEST SECTION 8: Registry Integration
# =============================================================================

def test_registry_integration():
    """Test that Mistral connector is properly registered."""
    print("\n=== Testing Registry Integration ===")

    def clear_env():
        keys = [
            'TRANSCRIPTION_CONNECTOR', 'TRANSCRIPTION_API_KEY', 'TRANSCRIPTION_BASE_URL',
            'TRANSCRIPTION_MODEL', 'USE_ASR_ENDPOINT', 'ASR_BASE_URL',
        ]
        for key in keys:
            os.environ.pop(key, None)

    def reset_registry():
        from src.services.transcription import registry
        registry._registry = None
        registry.ConnectorRegistry._instance = None
        registry.ConnectorRegistry._initialized = False
        registry.ConnectorRegistry._active_connector = None
        registry.ConnectorRegistry._connector_name = ""

    from src.services.transcription.registry import get_registry

    def t1():
        clear_env()
        reset_registry()
        registry = get_registry()
        connectors = registry.list_connectors()
        names = [c['name'] for c in connectors]
        assert 'mistral' in names, f"'mistral' not in registered connectors: {names}"
    run_test("Mistral connector is registered in registry", t1)

    def t2():
        clear_env()
        reset_registry()
        registry = get_registry()
        connectors = registry.list_connectors()
        mistral = next(c for c in connectors if c['name'] == 'mistral')
        assert 'DIARIZATION' in mistral['capabilities']
        assert 'TIMESTAMPS' in mistral['capabilities']
        assert 'LANGUAGE_DETECTION' in mistral['capabilities']
    run_test("Mistral connector info shows correct capabilities", t2)

    def t3():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_CONNECTOR'] = 'mistral'
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        registry = get_registry()
        registry.initialize_from_env()
        assert registry.get_active_connector_name() == 'mistral'
    run_test("TRANSCRIPTION_CONNECTOR=mistral activates Mistral connector", t3)

    def t4():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_CONNECTOR'] = 'mistral'
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['TRANSCRIPTION_MODEL'] = 'voxtral-mini-2602'
        registry = get_registry()
        connector = registry.initialize_from_env()
        assert connector.model == 'voxtral-mini-2602'
    run_test("Custom model name is passed to connector", t4)

    def t5():
        clear_env()
        reset_registry()
        os.environ['TRANSCRIPTION_CONNECTOR'] = 'mistral'
        os.environ['TRANSCRIPTION_API_KEY'] = 'test-key'
        os.environ['TRANSCRIPTION_BASE_URL'] = 'https://custom.endpoint.com  # comment'
        registry = get_registry()
        connector = registry.initialize_from_env()
        assert connector.base_url == 'https://custom.endpoint.com'
    run_test("Trailing comment stripped from TRANSCRIPTION_BASE_URL", t5)

    # Cleanup
    clear_env()


# =============================================================================
# TEST SECTION 9: Chunking Behavior
# =============================================================================

def test_chunking_behavior():
    """Test that Mistral connector specs correctly prevent app-level chunking."""
    print("\n=== Testing Chunking Behavior ===")

    from src.audio_chunking import get_effective_chunking_config
    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector

    def t1():
        os.environ['ENABLE_CHUNKING'] = 'true'
        os.environ['CHUNK_LIMIT'] = '20MB'
        config = get_effective_chunking_config(MistralTranscriptionConnector.SPECIFICATIONS)
        assert config.enabled is False, f"Chunking should be disabled for Mistral but got enabled={config.enabled}"
        assert config.source == 'connector_internal'
        os.environ.pop('ENABLE_CHUNKING', None)
        os.environ.pop('CHUNK_LIMIT', None)
    run_test("Mistral specs disable app-level chunking", t1)


# =============================================================================
# Main
# =============================================================================

def main():
    global PASSED, FAILED, ERRORS

    print("=" * 60)
    print("Mistral/Voxtral Transcription Connector Tests")
    print("=" * 60)

    test_initialization()
    test_capabilities()
    test_segment_parsing()
    test_hotwords_splitting()
    test_request_building()
    test_response_parsing()
    test_error_handling()
    test_registry_integration()
    test_chunking_behavior()

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=" * 60)

    if ERRORS:
        print("\nFailed tests:")
        for name, error in ERRORS:
            print(f"  - {name}: {error}")

    return 0 if FAILED == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
