"""An ASR error body is cut from the end, so the exception line survives.

A 5xx from the ASR endpoint usually carries the worker's traceback. The
body was cut to its first 800 characters, then the stored technical detail
to 500, both from the start: what reached the recording was the top of the
stack, and the one line that said why (the exception at the bottom) was gone.
"""

import os
import sys
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
from src.services.transcription.exceptions import ProviderError
from src.utils.error_formatting import format_error_for_storage, truncate_middle

FRAME = '  File "/workspace/app/pipeline.py", line 313, in transcribe\n    result = whisper_model.transcribe(audio)\n'
TRACEBACK = 'worker error:\nTraceback (most recent call last):\n' + FRAME * 30 + 'RuntimeError: CUDA out of memory'


def test_truncate_middle_keeps_both_ends_and_respects_the_limit():
    text = 'A' * 1000 + 'Z' * 1000
    out = truncate_middle(text, 500)
    assert len(out) <= 500
    assert out.startswith('AAA') and out.endswith('ZZZ')
    assert ' … ' in out
    assert truncate_middle('short', 500) == 'short'


def test_stored_technical_detail_ends_with_the_exception():
    stored = format_error_for_storage(f'ASR request failed with status 500: {TRACEBACK}')
    assert 'RuntimeError: CUDA out of memory' in stored
    assert 'Traceback (most recent call last)' in stored


class _Client:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        request = httpx.Request('POST', url)
        return httpx.Response(500, request=request, text=self.body)


def test_provider_error_carries_the_exception_from_a_long_body():
    connector = ASREndpointConnector({'base_url': 'http://asr.test:9000', 'timeout': 5})
    body = '{"detail":"' + TRACEBACK.replace('\n', '\\n') + '"}'
    from src.services.transcription.base import TranscriptionRequest
    import io
    req = TranscriptionRequest(audio_file=io.BytesIO(b'x'), filename='a.mp3')
    with patch('src.services.transcription.connectors.asr_endpoint.httpx.Client', return_value=_Client(body)), \
            patch.object(ASREndpointConnector, 'timeout', 5):
        with pytest.raises(ProviderError) as exc:
            connector.transcribe(req)
    assert 'status 500' in str(exc.value)
    assert 'RuntimeError: CUDA out of memory' in str(exc.value)
