"""Server-side repair of WebM files whose EBML header isn't at the front (#340).

A browser MediaRecorder can hand us a .webm whose leading bytes are not the EBML
magic (chunk-assembly artifact, usually on crash-recovered recordings). ffmpeg
then rejects the file. try_repair_malformed_webm trims the leading garbage to the
real header, but only commits the trim if the result actually probes as valid —
so a false-positive match never destroys the upload.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.ffprobe import try_repair_malformed_webm, get_codec_info, _EBML_MAGIC, FFProbeError


def _ffmpeg_available():
    return shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


pytestmark = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not installed")


@pytest.fixture
def valid_webm(tmp_path):
    """A tiny real Opus/WebM file produced by ffmpeg."""
    path = str(tmp_path / "good.webm")
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono',
         '-t', '1', '-c:a', 'libopus', path],
        check=True, capture_output=True,
    )
    # Sanity: a freshly-muxed webm really does start with the EBML magic.
    with open(path, 'rb') as f:
        assert f.read(4) == _EBML_MAGIC
    return path


def test_repairs_webm_with_leading_garbage(valid_webm):
    good_bytes = open(valid_webm, 'rb').read()
    broken = valid_webm.replace('good', 'broken')
    with open(broken, 'wb') as f:
        f.write(b'\x43\xc3\x81\x13' * 64)  # bogus prefix like the one reported
        f.write(good_bytes)

    # Precondition: it does NOT probe before repair.
    with pytest.raises(FFProbeError):
        get_codec_info(broken)

    info = try_repair_malformed_webm(broken)
    assert info is not None
    assert info['has_audio'] is True
    # File now starts at the real header and probes cleanly.
    with open(broken, 'rb') as f:
        assert f.read(4) == _EBML_MAGIC
    assert get_codec_info(broken)['has_audio'] is True


def test_wellformed_file_is_left_untouched(valid_webm):
    before = open(valid_webm, 'rb').read()
    assert try_repair_malformed_webm(valid_webm) is None
    assert open(valid_webm, 'rb').read() == before


def test_file_without_ebml_header_is_not_touched(tmp_path):
    garbage = str(tmp_path / "no_header.webm")
    payload = b'\x00\x01\x02\x03' * 4096  # no EBML magic anywhere
    with open(garbage, 'wb') as f:
        f.write(payload)
    assert try_repair_malformed_webm(garbage) is None
    assert open(garbage, 'rb').read() == payload  # untouched


def test_false_positive_match_does_not_destroy_original(tmp_path):
    """If the EBML magic appears by chance but the trimmed file still won't
    probe, the original must be preserved (repair returns None)."""
    path = str(tmp_path / "chance.webm")
    payload = b'\x11\x22\x33\x44' * 32 + _EBML_MAGIC + b'\x99' * 4096
    with open(path, 'wb') as f:
        f.write(payload)
    assert try_repair_malformed_webm(path) is None
    assert open(path, 'rb').read() == payload  # original intact, not clobbered


def test_missing_file_returns_none(tmp_path):
    assert try_repair_malformed_webm(str(tmp_path / "nope.webm")) is None
