"""Smoke test for FunASR connector URL injection (standalone, no src.app)."""
import sys, os
sys.path.insert(0, os.getcwd())

from unittest.mock import patch, MagicMock
from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.alibaba_funasr import AlibabaFunASRConnector
from src.services.transcription.exceptions import TranscriptionError
import io


def test_funasr_connector_reads_urls_from_extra_options():
    """AlibabaFunASRConnector reads funasr_file_urls from extra_options."""
    captured = {}

    def _fake_post(url, **kwargs):
        if 'json' in kwargs:
            captured['payload'] = kwargs['json']
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"output": {"task_id": "tid", "task_status": "SUCCEEDED", "results": []}}'
        resp.json.return_value = {"output": {"task_id": "tid", "task_status": "SUCCEEDED", "results": []}}
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


if __name__ == '__main__':
    test_transcription_request_has_no_file_urls_field()
    test_transcription_request_extra_options_pass_through()
    test_funasr_connector_reads_urls_from_extra_options()
    test_funasr_connector_raises_when_no_url()
    print('\nAll 4 standalone FunASR tests passed.')
