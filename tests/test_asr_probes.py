"""Connection probes and connector metadata.

Every probe performs one real, cheap request against the provider. These tests
replace ``httpx.get``/``httpx.head`` with a fake that records the URLs and
headers and returns a canned response.
"""

import httpx
import pytest

from src.services.transcription.base import (
    ProbeResult,
    ProbeUnsupportedError,
    filter_probe_models,
)
from src.services.transcription.connectors.alibaba_funasr import AlibabaFunASRConnector
from src.services.transcription.connectors.assemblyai import AssemblyAITranscriptionConnector
from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
from src.services.transcription.connectors.azure_openai_transcribe import (
    AzureOpenAITranscribeConnector,
)
from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
from src.services.transcription.connectors.mossland import MosslandTranscriptionConnector
from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector
from src.services.transcription.connectors.openai_whisper import OpenAIWhisperConnector
from src.services.transcription.connectors.openasr import OpenASRTranscriptionConnector
from src.services.transcription.connectors.tencent_asr import TencentASRConnector
from src.services.transcription.connectors.vibevoice import VibeVoiceTranscriptionConnector
from src.services.transcription.registry import ConnectorRegistry


def response(status=200, payload=None, text=''):
    request = httpx.Request('GET', 'http://fake.test')
    if payload is not None:
        return httpx.Response(status_code=status, json=payload, request=request)
    return httpx.Response(status_code=status, text=text, request=request)


class FakeHttp:
    """Records requests and answers with a per-URL handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self.handler('GET', url, kwargs)

    def head(self, url, **kwargs):
        self.calls.append(('HEAD', url, kwargs))
        return self.handler('HEAD', url, kwargs)


@pytest.fixture
def fake_http(monkeypatch):
    def install(handler):
        fake = FakeHttp(handler)
        monkeypatch.setattr(httpx, 'get', fake.get)
        monkeypatch.setattr(httpx, 'head', fake.head)
        return fake

    return install


def models_payload(*ids):
    return {'object': 'list', 'data': [{'id': model} for model in ids]}


def test_openai_whisper_probe_success(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, models_payload('whisper-1', 'tts-1')))
    connector = OpenAIWhisperConnector({'api_key': 'sk-test', 'base_url': 'https://api.openai.com/v1'})

    result = connector.probe()

    assert isinstance(result, ProbeResult)
    assert result.ok is True
    assert result.status == 200
    assert [m['id'] for m in result.models] == ['whisper-1', 'tts-1']
    assert fake.calls[0][1] == 'https://api.openai.com/v1/models'
    assert fake.calls[0][2]['headers']['Authorization'] == 'Bearer sk-test'


def test_openai_whisper_probe_401(fake_http):
    fake_http(lambda method, url, kwargs: response(401, {'error': {'message': 'Incorrect API key'}}))
    connector = OpenAIWhisperConnector({'api_key': 'wrong', 'base_url': 'https://api.openai.com/v1'})

    result = connector.probe()

    assert result.ok is False
    assert result.status == 401
    assert 'Incorrect API key' in result.message


def test_openai_whisper_probe_timeout(fake_http):
    def handler(method, url, kwargs):
        raise httpx.ReadTimeout('timed out', request=httpx.Request(method, url))

    fake_http(handler)
    connector = OpenAIWhisperConnector({'api_key': 'sk-test', 'base_url': 'https://api.openai.com/v1'})

    result = connector.probe()

    assert result.ok is False
    assert 'timed out' in result.message.lower()


def test_openai_whisper_probe_connection_error(fake_http):
    def handler(method, url, kwargs):
        raise httpx.ConnectError('connection refused', request=httpx.Request(method, url))

    fake_http(handler)
    connector = OpenAIWhisperConnector({'api_key': 'sk-test', 'base_url': 'https://api.openai.com/v1'})

    result = connector.probe()

    assert result.ok is False
    assert result.status is None
    assert 'reach' in result.message.lower()


def test_openai_transcribe_probe_uses_default_base_url(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, models_payload('gpt-4o-transcribe')))
    connector = OpenAITranscribeConnector({'api_key': 'sk-test'})

    result = connector.probe()

    assert result.ok is True
    assert fake.calls[0][1] == 'https://api.openai.com/v1/models'


def test_mistral_probe_hits_v1_models(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, models_payload('voxtral-mini-latest')))
    connector = MistralTranscriptionConnector({'api_key': 'k', 'base_url': 'https://api.mistral.ai'})

    result = connector.probe()

    assert result.ok is True
    assert fake.calls[0][1] == 'https://api.mistral.ai/v1/models'
    assert fake.calls[0][2]['headers']['Authorization'] == 'Bearer k'


def test_mossland_probe_hits_v1_models(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, models_payload('moss-transcribe-diarize')))
    connector = MosslandTranscriptionConnector({'api_key': 'k', 'base_url': 'https://api.mosi.cn'})

    result = connector.probe()

    assert result.ok is True
    assert fake.calls[0][1] == 'https://api.mosi.cn/v1/models'
    assert fake.calls[0][2]['headers']['Authorization'] == 'Bearer k'


def test_vibevoice_probe_works_without_key(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, models_payload('microsoft/VibeVoice-ASR')))
    connector = VibeVoiceTranscriptionConnector({'base_url': 'http://vllm.local:8000'})

    result = connector.probe()

    assert result.ok is True
    assert fake.calls[0][1] == 'http://vllm.local:8000/v1/models'
    assert 'Authorization' not in fake.calls[0][2]['headers']


def test_asr_endpoint_probe_health_then_models(fake_http):
    def handler(method, url, kwargs):
        if url.endswith('/health'):
            return response(200, {'status': 'ok'})
        if url.endswith('/v1/models'):
            return response(200, models_payload('large-v3', 'small'))
        return response(404)

    fake = fake_http(handler)
    connector = ASREndpointConnector({'base_url': 'http://asr.local:9000'})

    result = connector.probe()

    assert result.ok is True
    assert result.status == 200
    assert [m['id'] for m in result.models] == ['large-v3', 'small']
    assert [call[1] for call in fake.calls] == [
        'http://asr.local:9000/health',
        'http://asr.local:9000/v1/models',
    ]


def test_asr_endpoint_probe_models_404_is_ok_with_empty_models(fake_http):
    def handler(method, url, kwargs):
        if url.endswith('/health'):
            return response(200, {'status': 'ok'})
        return response(404)

    fake_http(handler)
    connector = ASREndpointConnector({'base_url': 'http://asr.local:9000'})

    result = connector.probe()

    assert result.ok is True
    assert result.models == []


def test_asr_endpoint_probe_falls_back_to_root(fake_http):
    def handler(method, url, kwargs):
        if url.endswith('/health'):
            return response(503)
        if url.endswith('/v1/models'):
            return response(404)
        return response(200, text='<html>service</html>')

    fake = fake_http(handler)
    connector = ASREndpointConnector({'base_url': 'http://asr.local:9000'})

    result = connector.probe()

    assert result.ok is True
    assert [call[1] for call in fake.calls][:2] == [
        'http://asr.local:9000/health',
        'http://asr.local:9000/',
    ]


def test_openasr_probe_models_then_health(fake_http):
    def handler(method, url, kwargs):
        if url.endswith('/v1/models'):
            return response(200, models_payload('qwen3-asr-0.6b'))
        return response(200, {'status': 'ok'})

    fake = fake_http(handler)
    connector = OpenASRTranscriptionConnector({'base_url': 'http://127.0.0.1:8080'})

    result = connector.probe()

    assert result.ok is True
    assert [m['id'] for m in result.models] == ['qwen3-asr-0.6b']
    assert fake.calls[0][1] == 'http://127.0.0.1:8080/v1/models'


def test_openasr_probe_falls_back_to_health(fake_http):
    def handler(method, url, kwargs):
        if url.endswith('/v1/models'):
            return response(404)
        return response(200, {'status': 'ok'})

    fake = fake_http(handler)
    connector = OpenASRTranscriptionConnector({'base_url': 'http://127.0.0.1:8080'})

    result = connector.probe()

    assert result.ok is True
    assert result.models == []
    assert fake.calls[1][1] == 'http://127.0.0.1:8080/health'


def test_assemblyai_probe_uses_account_endpoint_and_authorization_header(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, {'id': 'account'}))
    connector = AssemblyAITranscriptionConnector({'api_key': 'assembly-key'})

    result = connector.probe()

    assert result.ok is True
    assert result.models == []
    assert fake.calls[0][1] == 'https://api.assemblyai.com/v2/account'
    assert fake.calls[0][2]['headers']['Authorization'] == 'assembly-key'


def test_azure_probe_lists_deployments(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(200, {'data': [{'id': 'my-deployment'}]}))
    connector = AzureOpenAITranscribeConnector({
        'api_key': 'azure-key',
        'endpoint': 'https://resource.openai.azure.com',
        'deployment_name': 'my-deployment',
        'api_version': '2025-04-01-preview',
    })

    result = connector.probe()

    assert result.ok is True
    assert [m['id'] for m in result.models] == ['my-deployment']
    assert fake.calls[0][1] == 'https://resource.openai.azure.com/openai/deployments'
    assert fake.calls[0][2]['headers']['api-key'] == 'azure-key'
    assert fake.calls[0][2]['params']['api-version'] == '2025-04-01-preview'


def test_azure_probe_falls_back_to_head(fake_http):
    def handler(method, url, kwargs):
        if method == 'GET':
            return response(404)
        return response(200)

    fake = fake_http(handler)
    connector = AzureOpenAITranscribeConnector({
        'api_key': 'azure-key',
        'endpoint': 'https://resource.openai.azure.com',
        'deployment_name': 'my-deployment',
    })

    result = connector.probe()

    assert result.ok is True
    assert result.models == []
    assert fake.calls[1][0] == 'HEAD'
    assert fake.calls[1][1] == 'https://resource.openai.azure.com'


def test_azure_probe_401_is_not_masked_by_fallback(fake_http):
    fake = fake_http(lambda method, url, kwargs: response(401))
    connector = AzureOpenAITranscribeConnector({
        'api_key': 'wrong',
        'endpoint': 'https://resource.openai.azure.com',
        'deployment_name': 'my-deployment',
    })

    result = connector.probe()

    assert result.ok is False
    assert result.status == 401
    assert [call[0] for call in fake.calls] == ['GET']


def test_funasr_probe_is_unsupported():
    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'k',
    })

    with pytest.raises(ProbeUnsupportedError):
        connector.probe()


def test_tencent_probe_is_unsupported():
    connector = TencentASRConnector({'secret_id': 'id', 'secret_key': 'key'})

    with pytest.raises(ProbeUnsupportedError):
        connector.probe()


def test_probe_filters_discovery_but_test_counts_all(fake_http):
    fake_http(lambda method, url, kwargs: response(200, models_payload('whisper-1', 'tts-1', 'dall-e-3')))
    connector = OpenAIWhisperConnector({'api_key': 'sk-test', 'base_url': 'https://api.openai.com/v1'})

    result = connector.probe()

    assert len(result.models) == 3
    assert [m['id'] for m in filter_probe_models(result.models, connector.MODEL_FILTERS)] == ['whisper-1']


def test_discovery_filters_per_connector():
    assert OpenAIWhisperConnector.MODEL_FILTERS == ('whisper',)
    assert OpenAITranscribeConnector.MODEL_FILTERS == ('transcribe', 'whisper')
    assert MistralTranscriptionConnector.MODEL_FILTERS == ('voxtral',)
    assert MosslandTranscriptionConnector.MODEL_FILTERS == ('moss-transcribe',)
    assert ASREndpointConnector.MODEL_FILTERS == ()


def test_health_check_unchanged():
    whisper = OpenAIWhisperConnector({'api_key': 'k'})
    assert whisper.health_check() is True
    whisper.config = {'api_key': ''}
    assert whisper.health_check() is False
    mistral = MistralTranscriptionConnector({'api_key': 'k'})
    assert mistral.health_check() is True
    mistral.config = {'api_key': ''}
    assert mistral.health_check() is False


@pytest.mark.parametrize('connector_name', [
    'openai_whisper', 'openai_transcribe', 'asr_endpoint', 'azure_openai_transcribe',
    'mistral', 'vibevoice', 'funasr', 'mossland', 'assemblyai', 'tencent_asr', 'openasr',
    'audiocpp',
])
def test_secret_fields_are_write_only(connector_name):
    cls = ConnectorRegistry().get_connector_class(connector_name)
    properties = cls.get_config_schema().get('properties', {})
    for name, spec in properties.items():
        if name in ('api_key', 'secret_key') or spec.get('writeOnly'):
            assert spec.get('writeOnly') is True, f'{connector_name}.{name} is secret but not writeOnly'


def test_asr_endpoint_schema_has_model_with_whisperx_examples():
    properties = ASREndpointConnector.get_config_schema()['properties']
    assert 'model' in properties
    examples = properties['model'].get('examples', [])
    assert 'large-v3' in examples
    assert 'distil-large-v3' in examples


@pytest.mark.parametrize('connector_name', [
    'openai_whisper', 'openai_transcribe', 'asr_endpoint', 'azure_openai_transcribe',
    'mistral', 'vibevoice', 'funasr', 'mossland', 'assemblyai', 'tencent_asr', 'openasr',
    'audiocpp',
])
def test_enum_fields_declare_their_values(connector_name):
    cls = ConnectorRegistry().get_connector_class(connector_name)
    properties = cls.get_config_schema().get('properties', {})
    for name, spec in properties.items():
        if 'enum' in spec:
            assert isinstance(spec['enum'], list) and spec['enum'], f'{connector_name}.{name} enum is empty'
            if 'default' in spec:
                assert spec['default'] in spec['enum'], \
                    f'{connector_name}.{name} default {spec["default"]!r} not in enum'


def test_every_connector_has_display_name_and_description():
    for connector in ConnectorRegistry().list_connectors():
        assert isinstance(connector['display_name'], str) and connector['display_name'].strip(), \
            f"{connector['name']} has no display name"
        assert isinstance(connector['description'], str) and connector['description'].strip(), \
            f"{connector['name']} has no description"


def test_registry_exposes_display_name_description_schema_capabilities():
    connectors = {item['name']: item for item in ConnectorRegistry().list_connectors()}

    assert set(connectors) == {
        'openai_whisper', 'openai_transcribe', 'asr_endpoint',
        'azure_openai_transcribe', 'mistral', 'vibevoice', 'funasr', 'mossland',
        'assemblyai', 'tencent_asr', 'openasr', 'audiocpp',
    }
    whisper = connectors['openai_whisper']
    assert whisper['display_name'] == 'OpenAI Whisper'
    assert whisper['description']
    assert whisper['config_schema']['type'] == 'object'
    assert 'HOTWORDS' in whisper['capabilities']
