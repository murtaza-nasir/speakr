"""Tencent ASR contract tests. No credentials or external network required."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors import tencent_asr as mod
from src.services.transcription.exceptions import ConfigurationError, ProviderError, TranscriptionError


@pytest.fixture
def connector():
    return mod.TencentASRConnector({'secret_id': 'test-id', 'secret_key': 'test-secret',
                                   'poll_interval': .001, 'timeout': .5})


def request(data=b'audio', **kwargs):
    return TranscriptionRequest(audio_file=io.BytesIO(data), filename='test.wav', **kwargs)


def transport(monkeypatch, responses):
    calls = []
    def handler(req):
        calls.append((req.headers['X-TC-Action'], json.loads(req.content)))
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return httpx.Response(value)
        return httpx.Response(200, json={'Response': value})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(mod.httpx, 'Client', lambda **kwargs: client)
    return calls


def test_submit_poll_parse_and_no_duplicate_submit(connector, monkeypatch):
    calls = transport(monkeypatch, [
        {'Data': {'TaskId': 42}}, 503, httpx.ReadTimeout('hidden'),
        {'Error': {'Code': 'RequestLimitExceeded.UinLimitExceeded'}},
        {'Data': {'Status': 1}},
        {'Data': {'Status': 2, 'AudioDuration': 2.5, 'ResultDetail': [
            {'FinalSentence': '你好', 'StartMs': 0, 'EndMs': 1200, 'SpeakerId': 0},
            {'FinalSentence': '测试', 'StartMs': 1300, 'EndMs': 2500, 'SpeakerId': 1}]}}])
    result = connector.transcribe(request(diarize=True, hotwords='志恒, Skill,志恒', model='whisper-1'))
    assert [c[0] for c in calls].count('CreateRecTask') == 1
    payload = calls[0][1]
    assert payload['DataLen'] == 5 and payload['SourceType'] == 1
    assert payload['EngineModelType'] == '16k_zh_en_meeting'
    assert payload['HotwordList'] == '志恒|5,Skill|5'
    assert result.text == '你好\n测试'
    assert result.segments[0].start_time == 0
    assert result.segments[0].end_time == 1.2
    assert result.speakers == ['SPEAKER_00', 'SPEAKER_01']
    assert result.duration == 2.5


def test_off_toggle_and_model_override(connector):
    req = request(diarize=False, model='16k_zh_en_2.0')
    assert connector._parameters(req)['SpeakerDiarization'] == 0
    result = connector._parse_result({'ResultDetail': [
        {'FinalSentence': 'hello', 'SpeakerId': 0, 'StartMs': 0, 'EndMs': 10}]}, req.model, False)
    assert result.speakers is None and not result.has_diarization()


@pytest.mark.parametrize('config', [{}, {'secret_id': 'a'},
    {'secret_id': 'a', 'secret_key': 'b', 'model': 'whisper'},
    {'secret_id': 'a', 'secret_key': 'b', 'timeout': 'nan'},
    {'secret_id': 'a', 'secret_key': 'b', 'poll_interval': 0}])
def test_invalid_config(config):
    with pytest.raises(ConfigurationError):
        mod.TencentASRConnector(config)


@pytest.mark.parametrize('words, kept', [
    ('a|100,alpha', 'alpha|5'),
    ('中' * 11 + ',' + '中' * 10, '中' * 10 + '|5'),
    (','.join(str(i) for i in range(129)), ','.join(f'{i}|5' for i in range(128)))])
def test_hotwords_tencent_cannot_take_are_left_out(connector, words, kept):
    assert connector._parameters(request(hotwords=words))['HotwordList'] == kept


def test_hotword_table_used_when_no_request_hotword_fits():
    connector = mod.TencentASRConnector({'secret_id': 'test-id', 'secret_key': 'test-secret',
                                         'hotword_id': 'table-1'})
    params = connector._parameters(request(hotwords='a|1'))
    assert 'HotwordList' not in params and params['HotwordId'] == 'table-1'


def test_quota_error_no_secrets(connector, monkeypatch):
    calls = transport(monkeypatch, [{'Error': {'Code': 'FailedOperation.UserHasNoAmount',
                                              'Message': 'SECRET URL PRIVATE'}}])
    with pytest.raises(ProviderError, match='recording-file ASR quota') as exc:
        connector.transcribe(request())
    assert 'SECRET' not in str(exc.value) and len(calls) == 1


def test_submission_transport_failure_never_retries(connector, monkeypatch):
    calls = transport(monkeypatch, [httpx.ReadTimeout('PRIVATE')])
    with pytest.raises(TranscriptionError, match='not retried') as exc:
        connector.transcribe(request())
    assert len(calls) == 1 and 'PRIVATE' not in str(exc.value)


def test_poll_deadline_retains_task_id(connector, monkeypatch):
    monkeypatch.setattr(connector, '_call', lambda *args: {'Status': 1})
    with pytest.raises(TranscriptionError, match='task 42: polling timed out'):
        connector._poll(None, 42)


def test_empty_audio_never_submits(connector, monkeypatch):
    monkeypatch.setattr(connector, '_call', Mock(side_effect=AssertionError('network')))
    with pytest.raises(TranscriptionError, match='empty'):
        connector.transcribe(request(b''))


@pytest.mark.parametrize('status', [2, 3])
def test_large_file_uses_private_staging_and_cleans_terminal(connector, monkeypatch, status):
    monkeypatch.setattr(mod, 'INLINE_LIMIT', 5)
    storage = Mock()
    locator = object()
    def stage(path, req, *args):
        assert mod.Path(path).read_bytes() == b'123456'
        return storage, locator, 'https://bucket/file?signed=PRIVATE'
    monkeypatch.setattr(connector, '_stage', stage)
    calls = transport(monkeypatch, [{'Data': {'TaskId': 42}}, {'Data': {'Status': status, 'Result': 'text'}}])
    if status == 2:
        assert connector.transcribe(request(b'123456')).text == 'text'
    else:
        with pytest.raises(TranscriptionError, match='task 42 failed'):
            connector.transcribe(request(b'123456'))
    assert calls[0][1]['SourceType'] == 0 and 'Data' not in calls[0][1]
    storage.delete.assert_called_once_with(locator, missing_ok=True)


def test_ambiguous_submission_preserves_staging(connector, monkeypatch):
    monkeypatch.setattr(mod, 'INLINE_LIMIT', 5)
    storage = Mock()
    monkeypatch.setattr(connector, '_stage', lambda *args: (storage, object(), 'https://bucket/file'))
    transport(monkeypatch, [httpx.ReadTimeout('private')])
    with pytest.raises(TranscriptionError):
        connector.transcribe(request(b'123456'))
    storage.delete.assert_not_called()


def test_staging_requires_s3(connector, monkeypatch):
    import src.services.storage as module
    monkeypatch.setattr(module, 'get_storage_service', lambda: SimpleNamespace(s3=None))
    with pytest.raises(ConfigurationError, match='over 5 MiB'):
        connector._stage('/unused', request())


def test_registry_isolates_credentials_and_model(monkeypatch):
    from src.services.transcription.registry import get_registry
    monkeypatch.setenv('TENCENT_SECRET_ID', 'tencent-id')
    monkeypatch.setenv('TENCENT_SECRET_KEY', 'tencent-key')
    monkeypatch.setenv('TRANSCRIPTION_API_KEY', 'foreign')
    monkeypatch.setenv('TRANSCRIPTION_MODEL', 'whisper-1')
    monkeypatch.setenv('TRANSCRIPTION_BASE_URL', 'https://untrusted')
    registry = get_registry()
    config = registry._build_config_from_env('tencent_asr')
    connector = registry.create_connector('tencent_asr', config)
    assert connector.secret_id == 'tencent-id'
    assert connector.secret_key == 'tencent-key'
    assert connector.model == '16k_zh_en_meeting'
    assert 'base_url' not in config


def test_inline_boundary(connector, monkeypatch):
    monkeypatch.setattr(mod, 'INLINE_LIMIT', 5)
    monkeypatch.setattr(connector, '_stage', Mock(side_effect=AssertionError('unexpected staging')))
    calls = transport(monkeypatch, [{'Data': {'TaskId': 42}}, {'Data': {'Status': 2, 'Result': 'ok'}}])
    assert connector.transcribe(request(b'12345')).text == 'ok'
    assert calls[0][1]['SourceType'] == 1


def test_file_limit_before_upload(connector, monkeypatch):
    monkeypatch.setattr(mod, 'INLINE_LIMIT', 5)
    monkeypatch.setattr(mod, 'FILE_LIMIT', 10)
    monkeypatch.setattr(connector, '_stage', Mock(side_effect=AssertionError('unexpected staging')))
    with pytest.raises(TranscriptionError, match='exceeds'):
        connector.transcribe(request(b'12345678901'))


def test_staging_presigns_and_cleans_partial_failure(connector, monkeypatch, tmp_path):
    import src.services.storage as module
    backend = Mock()
    backend.build_locator.return_value = 's3://bucket/tencent-asr/test.wav'
    backend.presign_get_url.return_value = 'https://bucket/test?signed=PRIVATE'
    monkeypatch.setattr(module, 'get_storage_service', lambda: SimpleNamespace(s3=backend))
    path = tmp_path / 'audio'
    path.write_bytes(b'audio')
    actual_backend, locator, url = connector._stage(str(path), request())
    assert actual_backend is backend and 'signed=' in url
    assert backend.upload_local_file.call_args.args[1].startswith('tencent-asr/')
    backend.presign_get_url.assert_called_once_with(locator, expires_seconds=86400)
    backend.presign_get_url.side_effect = RuntimeError('PRIVATE')
    with pytest.raises(TranscriptionError) as exc:
        connector._stage(str(path), request())
    assert 'PRIVATE' not in str(exc.value)
    backend.delete.assert_called_once()


def test_submission_rejection_removes_staging(connector, monkeypatch):
    monkeypatch.setattr(mod, 'INLINE_LIMIT', 5)
    storage = Mock()
    monkeypatch.setattr(connector, '_stage', lambda *args: (storage, 'locator', 'https://bucket/file'))
    transport(monkeypatch, [{'Error': {'Code': 'AuthFailure.SecretIdNotFound'}}])
    with pytest.raises(ProviderError):
        connector.transcribe(request(b'123456'))
    storage.delete.assert_called_once_with('locator', missing_ok=True)


@pytest.mark.parametrize('envelope', [[], {'Data': []}, {'Error': 'bad'}, {}])
def test_malformed_response_is_safe(connector, monkeypatch, envelope):
    transport(monkeypatch, [envelope])
    with pytest.raises(ProviderError, match='invalid JSON'):
        connector.transcribe(request())


def test_unknown_speaker_not_fabricated(connector):
    result = connector._parse_result({'ResultDetail': [
        {'FinalSentence': 'one', 'SpeakerId': -1}, {'FinalSentence': 'two'}]}, mod.MODELS[0], True)
    assert not result.has_diarization()
    assert result.segments[0].start_time is None


def test_cos_client_configuration_and_private_upload(tmp_path):
    from unittest.mock import patch
    from src.services.storage.s3 import S3StorageBackend
    backend=S3StorageBackend(bucket='test-123',region='ap-hongkong',
                             endpoint_url='https://cos.ap-hongkong.myqcloud.com')
    with patch('boto3.client') as factory:
        backend._create_client()
        config=factory.call_args.kwargs['config']
        assert config.s3['addressing_style']=='virtual'
        assert config.request_checksum_calculation=='when_required'
        assert config.response_checksum_validation=='when_required'
    path=tmp_path/'audio.webm'
    path.write_bytes(b'original audio unchanged')
    fake=Mock()
    with patch.object(backend,'_get_client',return_value=fake), patch.object(backend,'stat'):
        backend.upload_local_file(str(path),'recordings/audio.webm',acl='private')
        fake.upload_file.assert_called_once_with(str(path),'test-123','recordings/audio.webm',ExtraArgs={'ACL':'private'})
    assert path.read_bytes()==b'original audio unchanged'


def test_webm_pipeline_preserves_original_even_when_compression_enabled(tmp_path,monkeypatch):
    import src.utils.audio_conversion as conversion
    p=tmp_path/'meeting.webm'
    p.write_bytes(b'original webm bytes')
    monkeypatch.setattr(conversion,'AUDIO_COMPRESS_UPLOADS',True)
    monkeypatch.setattr(conversion,'get_codec_info',Mock(side_effect=AssertionError('must not inspect or convert')))
    result=conversion.convert_if_needed(str(p),connector_specs=mod.TencentASRConnector.SPECIFICATIONS)
    assert result.output_path==str(p)
    assert not result.was_compressed and not result.was_converted
    assert p.read_bytes()==b'original webm bytes'
