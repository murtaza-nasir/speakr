"""Tests for the Alibaba Cloud FunASR connector."""
import sys, os
sys.path.insert(0, os.getcwd())

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.alibaba_funasr import (
    AlibabaFunASRConnector,
    prepare_funasr_file_url,
)
from src.services.transcription.exceptions import ProviderError, TranscriptionError
import io


def test_funasr_connector_reads_urls_from_extra_options():
    """AlibabaFunASRConnector reads funasr_file_urls from extra_options."""
    captured = {}

    def _fake_post(url, **kwargs):
        if 'json' in kwargs:
            captured['payload'] = kwargs['json']
        resp = MagicMock()
        resp.status_code = 200
        result = {
            "output": {
                "task_id": "tid",
                "task_status": "SUCCEEDED",
                "results": [{
                    "subtask_status": "SUCCEEDED",
                    "transcription_url": "https://signed.example/result.json",
                }],
            }
        }
        resp.text = '{}'
        resp.json.return_value = result
        resp.raise_for_status.return_value = None
        return resp

    connector = AlibabaFunASRConnector(
        config={'base_url': 'https://dashscope.aliyuncs.com/api/v1', 'api_key': 'sk-test'}
    )
    req = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.mp3',
        extra_options={'funasr_file_urls': ['https://signed.example/x.mp3?X-Amz-Sig=abc']},
    )

    with patch('httpx.Client') as client_cls:
        client_instance = MagicMock()
        client_instance.__enter__ = MagicMock(return_value=client_instance)
        client_instance.__exit__ = MagicMock(return_value=False)
        client_instance.post = MagicMock(side_effect=_fake_post)
        client_instance.get = MagicMock(side_effect=_fake_post)
        client_cls.return_value = client_instance

        connector.transcribe(req)

    assert captured['payload']['input']['file_urls'] == ['https://signed.example/x.mp3?X-Amz-Sig=abc']
    print('PASS: test_funasr_connector_reads_urls_from_extra_options')


def test_funasr_connector_raises_when_no_url():
    """If extra_options has no funasr_file_urls, connector raises clearly."""
    connector = AlibabaFunASRConnector(
        config={'base_url': 'https://dashscope.aliyuncs.com/api/v1', 'api_key': 'sk-test'}
    )
    req = TranscriptionRequest(audio_file=io.BytesIO(b'x'), filename='x.mp3')

    raised = False
    try:
        connector.transcribe(req)
    except TranscriptionError as exc:
        raised = True
        msg = str(exc).lower()
        assert 'file url' in msg or 's3' in msg, f'Unexpected error: {exc}'

    assert raised, 'Expected TranscriptionError'
    print('PASS: test_funasr_connector_raises_when_no_url')


def test_transcription_request_has_no_file_urls_field():
    """Bug C guard: TranscriptionRequest must not carry a fork-only file_urls."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(TranscriptionRequest)}
    assert 'file_urls' not in field_names, (
        "TranscriptionRequest.file_urls pollutes the upstream interface; "
        "use extra_options['funasr_file_urls'] instead."
    )
    print('PASS: test_transcription_request_has_no_file_urls_field')


def test_transcription_request_extra_options_pass_through():
    """extra_options is the supported passthrough."""
    req = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.mp3',
        extra_options={'funasr_file_urls': ['https://signed.example/x.mp3']},
    )
    assert req.extra_options['funasr_file_urls'] == ['https://signed.example/x.mp3']
    print('PASS: test_transcription_request_extra_options_pass_through')


def test_terminal_provider_failure_is_not_retried():
    """A terminal task failure must return immediately instead of polling again."""
    response = MagicMock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        'output': {
            'task_status': 'FAILED',
            'code': 'INVALID_PARAMETER',
            'message': 'bad request',
        }
    }
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = response
    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'sk-test',
        'timeout': 20,
        'poll_interval': 10,
    })

    with patch('httpx.Client', return_value=client), patch('time.sleep') as sleep:
        with pytest.raises(ProviderError, match='INVALID_PARAMETER'):
            connector._poll_task_result('task-1', {'Authorization': 'Bearer sk-test'})

    assert client.get.call_count == 1
    sleep.assert_not_called()


def test_transcribe_preserves_provider_error_type():
    """Provider metadata must survive the connector's public error boundary."""
    response = MagicMock(status_code=200, text='{}')
    response.raise_for_status.return_value = None
    response.json.return_value = {'output': {'task_id': 'task-1'}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.post.return_value = response
    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'sk-test',
    })
    request = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.wav',
        extra_options={'funasr_file_urls': ['https://signed.example/x.wav']},
    )
    provider_error = ProviderError(
        'task rejected', provider='funasr', status_code=400
    )

    with patch('httpx.Client', return_value=client), patch.object(
        connector, '_poll_task_result', side_effect=provider_error
    ):
        with pytest.raises(ProviderError) as exc_info:
            connector.transcribe(request)

    assert exc_info.value.provider == 'funasr'
    assert exc_info.value.status_code == 400


def test_presigned_urls_are_not_written_to_logs(caplog):
    """Signed query credentials must not be included in informational logs."""
    signed_audio_url = (
        'https://signed.example/x.wav?X-Amz-Signature=audio-secret'
    )
    signed_result_url = (
        'https://signed.example/result.json?X-Amz-Signature=result-secret'
    )
    response = MagicMock(status_code=200, text='{}')
    response.raise_for_status.return_value = None
    response.json.return_value = {'output': {'task_id': 'task-1'}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.post.return_value = response
    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'sk-test',
    })
    request = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.wav',
        extra_options={'funasr_file_urls': [signed_audio_url]},
    )
    result = {'output': {'results': [{
        'subtask_status': 'SUCCEEDED',
        'transcription_url': signed_result_url,
    }]}}
    parsed = {'transcripts': [{'sentences': [{
        'text': 'hello', 'begin_time': 0, 'end_time': 1000,
    }]}]}

    with caplog.at_level('INFO'), patch('httpx.Client', return_value=client), patch.object(
        connector, '_poll_task_result', return_value=result
    ), patch.object(
        connector, '_download_transcription_result', return_value=parsed
    ):
        connector.transcribe(request)

    assert 'audio-secret' not in caplog.text
    assert 'result-secret' not in caplog.text


def _fake_storage(audio_locator='local://recordings/source.wav'):
    s3 = MagicMock()
    s3.build_locator.return_value = 's3://bucket/funasr/1/source.wav'
    s3.exists.return_value = False
    s3.presign_get_url.return_value = (
        'https://signed.example/source.wav?X-Amz-Signature=secret'
    )
    storage = MagicMock(s3=s3)
    storage.maybe_normalize_local_legacy_locator.return_value = audio_locator
    storage.exists.return_value = True

    @contextmanager
    def materialize(_locator):
        yield SimpleNamespace(local_path='/tmp/source.wav')

    storage.materialize.side_effect = materialize
    return storage


def test_prepare_local_audio_does_not_persist_signed_url():
    """Ephemeral object-store credentials stay inside the active task."""
    storage = _fake_storage()
    recording = SimpleNamespace(
        id=1,
        audio_path='local://recordings/source.wav',
        original_filename='source.wav',
        mime_type='audio/wav',
    )

    with Flask(__name__).app_context(), patch(
        'src.services.storage.get_storage_service', return_value=storage
    ):
        success, urls = prepare_funasr_file_url(recording)

    assert success is True
    assert urls == [storage.s3.presign_get_url.return_value]
    assert not hasattr(recording, 'bucket_urls')


def test_prepare_existing_s3_audio_without_duplicate_upload():
    """An existing S3 locator is signed directly instead of copied again."""
    original_locator = 's3://bucket/recordings/source.wav'
    storage = _fake_storage(audio_locator=original_locator)
    recording = SimpleNamespace(
        id=1,
        audio_path=original_locator,
        original_filename='source.wav',
        mime_type='audio/wav',
    )

    with Flask(__name__).app_context(), patch(
        'src.services.storage.get_storage_service', return_value=storage
    ):
        success, urls = prepare_funasr_file_url(recording)

    assert success is True
    assert urls == [storage.s3.presign_get_url.return_value]
    storage.s3.upload_local_file.assert_not_called()
    signed_locator = storage.s3.presign_get_url.call_args.args[0]
    assert signed_locator.raw == original_locator


def test_prepare_audio_signs_directly_for_configured_intranet_endpoint():
    original_locator = 's3://bucket/recordings/source.wav'
    storage = _fake_storage(audio_locator=original_locator)
    recording = SimpleNamespace(
        id=1,
        audio_path=original_locator,
        original_filename='source.wav',
        mime_type='audio/wav',
    )

    with Flask(__name__).app_context(), patch(
        'src.services.storage.get_storage_service', return_value=storage
    ), patch(
        'src.config.app_config.S3_INTRANET_ENDPOINT_URL',
        'https://oss-cn-shanghai-internal.aliyuncs.com',
    ):
        success, urls = prepare_funasr_file_url(recording)

    assert success is True
    assert urls == [storage.s3.presign_get_url.return_value]
    assert storage.s3.presign_get_url.call_args.kwargs['endpoint_url'] == (
        'https://oss-cn-shanghai-internal.aliyuncs.com'
    )


def test_poll_4xx_is_terminal_and_not_retried():
    """A 4xx response during polling (e.g. revoked key) must raise immediately."""
    from httpx import HTTPStatusError, Request, Response

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _raise_for_status():
        resp = Response(401, request=Request('GET', 'https://dashscope.aliyuncs.com/tasks/x'))
        raise HTTPStatusError('401 Unauthorized', request=resp.request, response=resp)

    resp = MagicMock(status_code=401)
    resp.raise_for_status.side_effect = _raise_for_status
    client.get.return_value = resp

    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'sk-test',
        'timeout': 30,
        'poll_interval': 10,
    })

    with patch('httpx.Client', return_value=client), patch('time.sleep') as sleep:
        with pytest.raises(ProviderError) as exc_info:
            connector._poll_task_result('task-1', {'Authorization': 'Bearer sk-test'})

    assert exc_info.value.status_code == 401
    assert client.get.call_count == 1
    sleep.assert_not_called()


def test_poll_5xx_is_retried():
    """A 5xx response during polling is transient and must be retried."""
    from httpx import HTTPStatusError, Request, Response

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _raise_for_status():
        resp = Response(503, request=Request('GET', 'https://dashscope.aliyuncs.com/tasks/x'))
        raise HTTPStatusError('503 Service Unavailable', request=resp.request, response=resp)

    resp = MagicMock(status_code=503)
    resp.raise_for_status.side_effect = _raise_for_status
    client.get.return_value = resp

    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'sk-test',
        'timeout': 30,
        'poll_interval': 10,
    })

    with patch('httpx.Client', return_value=client), patch('time.sleep') as sleep:
        with pytest.raises(HTTPStatusError):
            connector._poll_task_result('task-1', {'Authorization': 'Bearer sk-test'})

    assert client.get.call_count > 1
    sleep.assert_called()
    # 503 (non-429) retries with the normal poll interval, not doubled.
    assert sleep.call_args.args[0] == 10


def test_poll_429_is_retried_with_doubled_backoff():
    """429 is transient and must be retried with a doubled backoff."""
    from httpx import HTTPStatusError, Request, Response

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _raise_for_status():
        resp = Response(429, request=Request('GET', 'https://dashscope.aliyuncs.com/tasks/x'))
        raise HTTPStatusError('429 Too Many Requests', request=resp.request, response=resp)

    resp = MagicMock(status_code=429)
    resp.raise_for_status.side_effect = _raise_for_status
    client.get.return_value = resp

    connector = AlibabaFunASRConnector({
        'base_url': 'https://dashscope.aliyuncs.com/api/v1',
        'api_key': 'sk-test',
        'timeout': 30,
        'poll_interval': 10,
    })

    with patch('httpx.Client', return_value=client), patch('time.sleep') as sleep:
        with pytest.raises(HTTPStatusError):
            connector._poll_task_result('task-1', {'Authorization': 'Bearer sk-test'})

    assert client.get.call_count > 1
    sleep.assert_called()
    # 429 retries with a doubled backoff.
    assert sleep.call_args.args[0] == 20


def test_transcribe_diarize_false_overrides_config_default():
    """An explicit off toggle must beat the connector's default_diarize."""
    captured = {}

    def _fake_post(url, **kwargs):
        if 'json' in kwargs:
            captured['payload'] = kwargs['json']
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{}'
        resp.json.return_value = {
            'output': {
                'task_id': 'tid',
                'task_status': 'SUCCEEDED',
                'results': [{
                    'subtask_status': 'SUCCEEDED',
                    'transcription_url': 'https://signed.example/result.json',
                }],
            }
        }
        resp.raise_for_status.return_value = None
        return resp

    connector = AlibabaFunASRConnector(
        config={
            'base_url': 'https://dashscope.aliyuncs.com/api/v1',
            'api_key': 'sk-test',
            'diarize': True,
        }
    )
    req = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.mp3',
        diarize=False,
        extra_options={'funasr_file_urls': ['https://signed.example/x.mp3']},
    )

    with patch('httpx.Client') as client_cls:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = False
        client_instance.post = MagicMock(side_effect=_fake_post)
        client_instance.get = MagicMock(side_effect=_fake_post)
        client_cls.return_value = client_instance

        connector.transcribe(req)

    params = captured['payload'].get('parameters', {})
    assert 'diarization_enabled' not in params


def test_transcribe_honors_request_diarize_and_speaker_count():
    """Per-request diarize / speaker bounds must reach the FunASR payload."""
    captured = {}

    def _fake_post(url, **kwargs):
        if 'json' in kwargs:
            captured['payload'] = kwargs['json']
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{}'
        resp.json.return_value = {
            'output': {
                'task_id': 'tid',
                'task_status': 'SUCCEEDED',
                'results': [{
                    'subtask_status': 'SUCCEEDED',
                    'transcription_url': 'https://signed.example/result.json',
                }],
            }
        }
        resp.raise_for_status.return_value = None
        return resp

    connector = AlibabaFunASRConnector(
        config={'base_url': 'https://dashscope.aliyuncs.com/api/v1', 'api_key': 'sk-test'}
    )
    req = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.mp3',
        diarize=True,
        min_speakers=2,
        max_speakers=4,
        extra_options={'funasr_file_urls': ['https://signed.example/x.mp3']},
    )

    with patch('httpx.Client') as client_cls:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = False
        client_instance.post = MagicMock(side_effect=_fake_post)
        client_instance.get = MagicMock(side_effect=_fake_post)
        client_cls.return_value = client_instance

        connector.transcribe(req)

    params = captured['payload'].get('parameters', {})
    assert params.get('diarization_enabled') is True
    assert params.get('speaker_count') == 4


def test_transcribe_speaker_count_falls_back_to_config_default():
    """When the request carries no speaker bounds, config default is used."""
    captured = {}

    def _fake_post(url, **kwargs):
        if 'json' in kwargs:
            captured['payload'] = kwargs['json']
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{}'
        resp.json.return_value = {
            'output': {
                'task_id': 'tid',
                'task_status': 'SUCCEEDED',
                'results': [{
                    'subtask_status': 'SUCCEEDED',
                    'transcription_url': 'https://signed.example/result.json',
                }],
            }
        }
        resp.raise_for_status.return_value = None
        return resp

    connector = AlibabaFunASRConnector(
        config={
            'base_url': 'https://dashscope.aliyuncs.com/api/v1',
            'api_key': 'sk-test',
            'diarize': True,
            'speaker_count': 3,
        }
    )
    req = TranscriptionRequest(
        audio_file=io.BytesIO(b'x'),
        filename='x.mp3',
        extra_options={'funasr_file_urls': ['https://signed.example/x.mp3']},
    )

    with patch('httpx.Client') as client_cls:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = False
        client_instance.post = MagicMock(side_effect=_fake_post)
        client_instance.get = MagicMock(side_effect=_fake_post)
        client_cls.return_value = client_instance

        connector.transcribe(req)

    params = captured['payload'].get('parameters', {})
    # diarize is a plain bool (default False), so it never falls back to the
    # connector default here; processing.py resolves the default before
    # building the request. Only speaker_count falls back to config.
    assert 'diarization_enabled' not in params
    assert params.get('speaker_count') == 3


def test_cleanup_funasr_staging_deletes_staged_keys():
    """Staging keys recorded during prepare are deleted by cleanup."""
    from src.services.transcription.connectors.alibaba_funasr import cleanup_funasr_staging

    s3 = MagicMock()
    s3.build_locator.return_value = 's3://bucket/funasr/1/source.wav'
    storage = MagicMock(s3=s3)
    recording = SimpleNamespace(
        id=1,
        _funasr_staging_keys=['funasr/1/source.wav'],
    )

    with Flask(__name__).app_context(), patch(
        'src.services.storage.get_storage_service', return_value=storage
    ):
        cleanup_funasr_staging(recording)

    s3.delete.assert_called_once()
    assert s3.delete.call_args.args[0].raw == 's3://bucket/funasr/1/source.wav'


def test_cleanup_funasr_staging_noop_without_keys():
    """Cleanup with no recorded staging keys must not touch storage."""
    from src.services.transcription.connectors.alibaba_funasr import cleanup_funasr_staging

    s3 = MagicMock()
    storage = MagicMock(s3=s3)
    recording = SimpleNamespace(id=1)

    with Flask(__name__).app_context(), patch(
        'src.services.storage.get_storage_service', return_value=storage
    ):
        cleanup_funasr_staging(recording)

    s3.delete.assert_not_called()


def test_prepare_local_audio_always_reuploads_staging():
    """A replaced audio file must never re-sign a stale staging object."""
    storage = _fake_storage()
    recording = SimpleNamespace(
        id=1,
        audio_path='local://recordings/source.wav',
        original_filename='source.wav',
        mime_type='audio/wav',
    )

    with Flask(__name__).app_context(), patch(
        'src.services.storage.get_storage_service', return_value=storage
    ):
        success, urls = prepare_funasr_file_url(recording)

    assert success is True
    storage.s3.upload_local_file.assert_called_once()
    assert recording._funasr_staging_keys == ['funasr/1/source.wav']


if __name__ == '__main__':
    test_transcription_request_has_no_file_urls_field()
    test_transcription_request_extra_options_pass_through()
    test_funasr_connector_reads_urls_from_extra_options()
    test_funasr_connector_raises_when_no_url()
    print('\nAll 4 standalone FunASR tests passed.')
