"""Error bodies from outside services are summarised before they are kept (#593).

An ASR gateway answered a failed request with an HTML page that carried a
token and a configuration path. The page went into the log, the failure
webhook, the failure email and the recording's technical details. Each
connector now keeps a short summary instead: HTML reduced to its text,
credential-like values redacted, directories of absolute paths removed,
length bounded. A JSON or plain-text message survives.
"""

import io
import logging
import os
import sys
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.exceptions import ProviderError, TranscriptionError
from src.utils.error_body import redact_secrets, summarize_error_body
from src.utils.error_formatting import format_error_for_storage, format_error_for_user

GATEWAY_PAGE = ('<html><head><title>504 Gateway Time-out</title></head>'
                '<body>token=EXAMPLE /opt/example/app.conf</body></html>')
LEAKS = ('token=EXAMPLE', 'EXAMPLE', '/opt/example', '<html', '<body')


def _assert_clean(text):
    for leak in LEAKS:
        assert leak not in text, (leak, text)


# --- the summary -------------------------------------------------------------

def test_html_page_becomes_its_text_without_the_secrets():
    out = summarize_error_body(GATEWAY_PAGE)
    assert out.startswith('HTML error page: 504 Gateway Time-out')
    _assert_clean(out)
    assert 'app.conf' in out  # the file name is kept, its directories are not


def test_html_page_is_bounded():
    page = '<!DOCTYPE html><html><body>' + '<p>gateway trouble</p>' * 500 + '</body></html>'
    out = summarize_error_body(page)
    assert len(out) <= len('HTML error page: ') + 200


def test_script_and_style_text_is_dropped():
    page = ('<html><head><style>body{color:red}</style><script>var key="abc"</script></head>'
            '<body>Bad gateway</body></html>')
    assert summarize_error_body(page) == 'HTML error page: Bad gateway'


@pytest.mark.parametrize('raw, kept', [
    ('Authorization: Bearer abcdefghijklmnop123', 'Authorization: <redacted>'),
    ('use header Bearer abcdefghijklmnop123 here', 'Bearer <redacted>'),
    ('{"api_key": "abc123def"}', '"api_key": "<redacted>"'),
    ('https://user:hunter2@asr.internal/asr', 'https://<redacted>@asr.internal/asr'),
    ('?access_token=xyz&x=1', 'access_token=<redacted>&x=1'),
    ('password=hunter2', 'password=<redacted>'),
    ('key sk-abcdefghijklmnopqrstuvwx', 'key <redacted>'),
    ('session 3f9a7c1e0b2d4f6a8c9e1b3d5f7a9c0e2b4d', 'session <redacted>'),
])
def test_credential_like_values_are_redacted(raw, kept):
    assert kept in redact_secrets(raw)


@pytest.mark.parametrize('message', [
    "Invalid model size 'whisper-tiny'",
    'max_tokens: 4096 is above the limit',
    'RuntimeError: CUDA out of memory',
    'see http://asr.test:9000/asr for details',
])
def test_ordinary_messages_are_unchanged(message):
    assert summarize_error_body(message) == message


def test_traceback_keeps_the_file_name_and_the_exception():
    body = 'Traceback:\n  File "/workspace/app/pipeline.py", line 3\nRuntimeError: CUDA out of memory'
    out = summarize_error_body(body)
    assert 'File "<path>/pipeline.py", line 3' in out
    assert out.endswith('RuntimeError: CUDA out of memory')
    assert '/workspace' not in out


def test_empty_bodies():
    assert summarize_error_body(None) == ''
    assert summarize_error_body('   ') == ''
    assert summarize_error_body('<html></html>') == 'HTML error page'


# --- the connectors ----------------------------------------------------------

class _Client:
    def __init__(self, status, body, json_body=None):
        self.status, self.body, self.json_body = status, body, json_body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        request = httpx.Request('POST', url)
        if self.json_body is not None:
            return httpx.Response(self.status, request=request, json=self.json_body)
        return httpx.Response(self.status, request=request, text=self.body)


def _request():
    return TranscriptionRequest(audio_file=io.BytesIO(b'x'), filename='clip.wav')


def _asr_error(status, body, caplog):
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
    connector = ASREndpointConnector({'base_url': 'http://asr.test:9000', 'timeout': 5})
    with patch('src.services.transcription.connectors.asr_endpoint.httpx.Client',
               return_value=_Client(status, body)), caplog.at_level(logging.DEBUG):
        with pytest.raises(TranscriptionError) as exc:
            connector.transcribe(_request())
    return str(exc.value)


def test_asr_html_error_status_omits_the_page(caplog):
    message = _asr_error(504, GATEWAY_PAGE, caplog)
    assert message.startswith('ASR request failed with status 504: HTML error page: 504 Gateway Time-out')
    _assert_clean(message)
    _assert_clean(caplog.text)
    stored = format_error_for_storage(message)
    _assert_clean(stored)
    assert format_error_for_user(message)['title'] == format_error_for_user(
        'ASR request failed with status 504: x')['title']


def test_asr_json_error_status_keeps_the_model_message(caplog):
    message = _asr_error(400, '{"detail":"Invalid model size \'whisper-tiny\'"}', caplog)
    assert 'status 400' in message
    assert "Invalid model size 'whisper-tiny'" in message


def test_asr_ok_html_body_stays_a_status_only_error(caplog):
    message = _asr_error(200, GATEWAY_PAGE, caplog)
    assert message.endswith('ASR service returned HTML error page')
    _assert_clean(message)
    _assert_clean(caplog.text)


def test_openasr_html_error_omits_the_page(caplog):
    from src.services.transcription.connectors.openasr import OpenASRTranscriptionConnector
    connector = OpenASRTranscriptionConnector({'base_url': 'http://asr.test:8000', 'model': 'whisper-1'})
    with patch('src.services.transcription.connectors.openasr.httpx.Client',
               return_value=_Client(502, GATEWAY_PAGE)), caplog.at_level(logging.DEBUG):
        with pytest.raises(TranscriptionError) as exc:
            connector.transcribe(_request())
    assert 'status 502' in str(exc.value)
    _assert_clean(str(exc.value))
    _assert_clean(caplog.text)


def test_funasr_html_error_omits_the_page(caplog):
    from src.services.transcription.connectors.alibaba_funasr import AlibabaFunASRConnector
    connector = AlibabaFunASRConnector({'api_key': 'k-test', 'base_url': 'https://funasr.test/api/v1'})
    request = TranscriptionRequest(audio_file=io.BytesIO(b'x'), filename='clip.wav',
                                   extra_options={'funasr_file_urls': ['https://files.test/clip.wav']})
    with patch('src.services.transcription.connectors.alibaba_funasr.httpx.Client',
               return_value=_Client(503, GATEWAY_PAGE)), caplog.at_level(logging.DEBUG):
        with pytest.raises(TranscriptionError) as exc:
            connector.transcribe(request)
    _assert_clean(str(exc.value))
    _assert_clean(caplog.text)
