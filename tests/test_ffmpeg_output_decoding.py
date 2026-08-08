"""
Regression tests for defensive decoding of ffmpeg/ffprobe output.

ffmpeg copies container metadata (ID3/INFO tags, etc.) into its stderr — and
some writers into stdout — as raw bytes without transcoding. A GBK-tagged
file therefore produces non-UTF-8 output, and the old `text=True` /
`.decode('utf-8')` call sites raised UnicodeDecodeError inside
subprocess.run before any error handling could run. Reproduced on Linux
inside the Docker container, not only on Windows locales.

Unit tests cover decode_ffmpeg_output directly; the integration test builds
a real GBK-tagged WAV and drives it through _run_ffmpeg_command.
"""

import locale
import struct
import subprocess
import sys
import os
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.ffmpeg_utils import decode_ffmpeg_output, _run_ffmpeg_command, FFmpegError

GBK_BYTES = '中文标题测试'.encode('gbk')


# ===========================================================================
# decode_ffmpeg_output unit tests (no ffmpeg required)
# ===========================================================================

def test_decode_none_returns_empty():
    assert decode_ffmpeg_output(None) == ''


def test_decode_str_passthrough():
    assert decode_ffmpeg_output('already text') == 'already text'


def test_decode_valid_utf8():
    assert decode_ffmpeg_output('héllo'.encode('utf-8')) == 'héllo'


def test_decode_gbk_via_preferred_encoding():
    # On a system whose preferred encoding is GBK (Windows zh locales),
    # the bytes decode to the original text.
    with mock.patch.object(locale, 'getpreferredencoding', return_value='gbk'):
        assert decode_ffmpeg_output(GBK_BYTES) == '中文标题测试'


def test_decode_gbk_on_utf8_system_never_raises():
    # On a UTF-8 system (Linux/Docker) the GBK bytes cannot be decoded
    # correctly, but the result must be a string, never an exception.
    with mock.patch.object(locale, 'getpreferredencoding', return_value='UTF-8'):
        out = decode_ffmpeg_output(GBK_BYTES)
    assert isinstance(out, str)


def test_decode_garbage_bytes_never_raises():
    out = decode_ffmpeg_output(bytes(range(256)))
    assert isinstance(out, str)


def test_decode_unknown_preferred_encoding_falls_back():
    with mock.patch.object(locale, 'getpreferredencoding', return_value='not-a-codec'):
        out = decode_ffmpeg_output(b'\xd6\xd0')
    assert isinstance(out, str)


# ===========================================================================
# Integration: real ffmpeg over a GBK-tagged WAV (skips without ffmpeg)
# ===========================================================================

def _ffmpeg_available():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _write_gbk_tagged_wav(path):
    """Minimal WAV with a LIST/INFO INAM tag holding raw GBK bytes."""
    def chunk(cid, data):
        if len(data) % 2:
            data += b'\x00'
        return cid + struct.pack('<I', len(data)) + data

    fmt = chunk(b'fmt ', struct.pack('<HHIIHH', 1, 1, 8000, 16000, 2, 16))
    data = chunk(b'data', b'\x00\x00' * 8000)
    info = chunk(b'LIST', b'INFO' + chunk(b'INAM', GBK_BYTES + b'\x00'))
    body = b'WAVE' + fmt + data + info
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body)) + body)


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not found")
def test_run_ffmpeg_command_gbk_metadata_fails_cleanly(tmp_path):
    # ffmpeg dumps the GBK tag bytes raw into stderr. A failing command must
    # surface as FFmpegError (with a decoded message), not UnicodeDecodeError
    # from inside subprocess.run.
    from src.app import app

    wav = tmp_path / 'gbk_tagged.wav'
    _write_gbk_tagged_wav(str(wav))
    out = tmp_path / 'out.mp3'

    with app.app_context():
        with pytest.raises(FFmpegError):
            _run_ffmpeg_command(
                ['ffmpeg', '-i', str(wav), '-codec:a', 'nonexistent_codec', str(out), '-y'],
                'gbk metadata regression',
            )


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not found")
def test_probe_gbk_tagged_wav(tmp_path):
    # probe() must parse the file cleanly regardless of raw tag bytes.
    from src.utils.ffprobe import probe

    wav = tmp_path / 'gbk_tagged.wav'
    _write_gbk_tagged_wav(str(wav))
    data = probe(str(wav))
    assert 'format' in data
