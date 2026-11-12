"""Fragmented MP4 videos kept for playback are remuxed into regular MP4s (#539).

A fragmented MP4 (OBS writes these) makes a browser seek through many small
range requests. The box structure is read without decoding to detect one, and
a stream copy with +faststart rewrites it. Anything that does not parse is
left alone, and a failed or doubtful remux keeps the original.

The media files are made with the real ffmpeg of the image.
"""

import os
import shutil
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.utils import ffmpeg_utils
from src.utils.ffmpeg_utils import FFmpegError, remux_fragmented_mp4_in_place
from src.utils.ffprobe import get_codec_info, get_duration
from src.utils.mp4_boxes import is_fragmented_mp4

pytestmark = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='needs ffmpeg')


def _make(path, *, movflags, fmt=None, seconds=3):
    cmd = ['ffmpeg', '-y', '-v', 'error',
           '-f', 'lavfi', '-i', f'testsrc=duration={seconds}:size=160x120:rate=10',
           '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
           '-c:v', 'mpeg4', '-g', '10', '-c:a', 'aac', '-shortest']
    if movflags:
        cmd += ['-movflags', movflags]
    if fmt:
        cmd += ['-f', fmt]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    return str(path)


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp('mp4')
    return {
        'fragmented': _make(d / 'obs.mp4', movflags='frag_keyframe+empty_moov'),
        'faststart': _make(d / 'fast.mp4', movflags='+faststart'),
        'regular': _make(d / 'plain.mp4', movflags=None),  # moov after mdat
        'fragmented_mov': _make(d / 'obs.mov', movflags='frag_keyframe+empty_moov'),
        'fragmented_m4v': _make(d / 'obs.m4v', movflags='frag_keyframe+empty_moov', fmt='mp4'),
        'matroska': _make(d / 'clip.mkv', movflags=None),
    }


@pytest.fixture
def ctx():
    with app.app_context():
        yield


def _copy(src, tmp_path, name=None):
    dst = tmp_path / (name or os.path.basename(src))
    shutil.copyfile(src, dst)
    return str(dst)


def _box(box_type, payload=b''):
    return struct.pack('>I4s', 8 + len(payload), box_type) + payload


# --- detection -------------------------------------------------------------

def test_detects_fragmented_and_regular_files(media):
    assert is_fragmented_mp4(media['fragmented']) is True
    assert is_fragmented_mp4(media['fragmented_mov']) is True
    assert is_fragmented_mp4(media['fragmented_m4v']) is True
    assert is_fragmented_mp4(media['faststart']) is False
    assert is_fragmented_mp4(media['regular']) is False


def test_non_mp4_and_broken_input_is_unknown(media, tmp_path):
    assert is_fragmented_mp4(media['matroska']) is None

    garbage = tmp_path / 'garbage.mp4'
    garbage.write_bytes(os.urandom(4096))
    assert is_fragmented_mp4(str(garbage)) is None

    text = tmp_path / 'notes.mp4'
    text.write_bytes(b'hello, this is not a video at all\n' * 20)
    assert is_fragmented_mp4(str(text)) is None

    empty = tmp_path / 'empty.mp4'
    empty.write_bytes(b'')
    assert is_fragmented_mp4(str(empty)) is None

    assert is_fragmented_mp4(str(tmp_path / 'missing.mp4')) is None

    data = open(media['fragmented'], 'rb').read()
    truncated = tmp_path / 'truncated.mp4'
    truncated.write_bytes(data[:len(data) // 2])
    assert is_fragmented_mp4(str(truncated)) is None


def test_box_headers_with_largesize_and_size_zero(tmp_path):
    ftyp = _box(b'ftyp', b'isom\x00\x00\x02\x00isomiso2')
    mvhd = _box(b'mvhd', b'\x00' * 100)
    # A 64-bit size header: size field 1, then the full size as 8 bytes.
    payload = b'\x00' * 32
    large_mdat = struct.pack('>I4sQ', 1, b'mdat', 16 + len(payload)) + payload

    regular = tmp_path / 'regular.mp4'
    regular.write_bytes(ftyp + large_mdat + _box(b'moov', mvhd))
    assert is_fragmented_mp4(str(regular)) is False

    with_mvex = tmp_path / 'mvex.mp4'
    with_mvex.write_bytes(ftyp + _box(b'moov', mvhd + _box(b'mvex', _box(b'trex', b'\x00' * 24))))
    assert is_fragmented_mp4(str(with_mvex)) is True

    # Size 0: the last box runs to the end of the file.
    moof_to_end = tmp_path / 'moof.mp4'
    moof_to_end.write_bytes(ftyp + _box(b'moov', mvhd) + struct.pack('>I4s', 0, b'moof') + b'\x00' * 40)
    assert is_fragmented_mp4(str(moof_to_end)) is True

    too_small = tmp_path / 'small.mp4'
    too_small.write_bytes(ftyp + struct.pack('>I4s', 4, b'moov') + b'\x00' * 16)
    assert is_fragmented_mp4(str(too_small)) is None

    past_end = tmp_path / 'past.mp4'
    past_end.write_bytes(ftyp + struct.pack('>I4s', 4000, b'moov') + mvhd)
    assert is_fragmented_mp4(str(past_end)) is None

    no_moov = tmp_path / 'nomoov.mp4'
    no_moov.write_bytes(ftyp + _box(b'mdat', b'\x00' * 64))
    assert is_fragmented_mp4(str(no_moov)) is None


def test_scan_is_capped(tmp_path):
    ftyp = _box(b'ftyp', b'isom\x00\x00\x02\x00')
    many = tmp_path / 'many.mp4'
    many.write_bytes(ftyp + _box(b'moov', _box(b'mvex')) + _box(b'free') * 50)
    with patch('src.utils.mp4_boxes.MAX_TOP_LEVEL_BOXES', 10):
        assert is_fragmented_mp4(str(many)) is None
    assert is_fragmented_mp4(str(many)) is True


# --- conversion ------------------------------------------------------------

def _assert_same_content(original, converted):
    a, b = get_codec_info(original, timeout=30), get_codec_info(converted, timeout=30)
    assert a['has_video'] and b['has_video'] and a['has_audio'] and b['has_audio']
    assert abs(get_duration(original, timeout=30) - get_duration(converted, timeout=30)) <= 0.5


@pytest.mark.parametrize('key, format_name', [
    ('fragmented', 'mp4'), ('fragmented_mov', 'mov'), ('fragmented_m4v', 'mp4')])
def test_fragmented_file_is_remuxed_in_place(ctx, media, tmp_path, key, format_name):
    path = _copy(media[key], tmp_path)
    os.chmod(path, 0o644)
    assert remux_fragmented_mp4_in_place(path) is True
    assert os.path.basename(path) == os.path.basename(media[key])
    assert is_fragmented_mp4(path) is False
    assert format_name in get_codec_info(path, timeout=30)['format_name']
    _assert_same_content(media[key], path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert sorted(os.listdir(tmp_path)) == [os.path.basename(path)]  # no temp file left

    # The moov index is at the front now.
    with open(path, 'rb') as fh:
        head = fh.read(4096)
    assert head.find(b'moov') != -1 and (head.find(b'mdat') == -1 or head.find(b'moov') < head.find(b'mdat'))


def test_regular_and_unknown_files_are_left_alone(ctx, media, tmp_path):
    garbage = tmp_path / 'garbage.mp4'
    garbage.write_bytes(os.urandom(2048))
    for path in (_copy(media['faststart'], tmp_path), _copy(media['regular'], tmp_path),
                 _copy(media['matroska'], tmp_path), str(garbage)):
        before = open(path, 'rb').read()
        with patch.object(ffmpeg_utils, '_run_ffmpeg_command') as run:
            assert remux_fragmented_mp4_in_place(path) is False
        run.assert_not_called()
        assert open(path, 'rb').read() == before


def test_ffmpeg_failure_keeps_the_original(ctx, media, tmp_path):
    path = _copy(media['fragmented'], tmp_path)
    before = open(path, 'rb').read()
    with patch.object(ffmpeg_utils, '_run_ffmpeg_command', side_effect=FFmpegError('boom')):
        assert remux_fragmented_mp4_in_place(path) is False
    assert open(path, 'rb').read() == before
    assert sorted(os.listdir(tmp_path)) == ['obs.mp4']


def test_output_still_fragmented_or_empty_keeps_the_original(ctx, media, tmp_path):
    path = _copy(media['fragmented'], tmp_path)
    before = open(path, 'rb').read()

    def still_fragmented(cmd, description):
        shutil.copyfile(media['fragmented'], cmd[-1])

    def empty(cmd, description):
        open(cmd[-1], 'wb').close()

    for fake in (still_fragmented, empty):
        with patch.object(ffmpeg_utils, '_run_ffmpeg_command', side_effect=fake):
            assert remux_fragmented_mp4_in_place(path) is False
        assert open(path, 'rb').read() == before
        assert sorted(os.listdir(tmp_path)) == ['obs.mp4']


def test_duration_mismatch_keeps_the_original(ctx, media, tmp_path):
    path = _copy(media['fragmented'], tmp_path)
    before = open(path, 'rb').read()
    durations = iter([30.0, 10.0])
    with patch('src.utils.ffprobe.get_duration', side_effect=lambda *a, **k: next(durations)):
        assert remux_fragmented_mp4_in_place(path) is False
    assert open(path, 'rb').read() == before
    assert sorted(os.listdir(tmp_path)) == ['obs.mp4']


def test_lost_audio_stream_keeps_the_original(ctx, media, tmp_path):
    path = _copy(media['fragmented'], tmp_path)
    before = open(path, 'rb').read()

    def video_only(cmd, description):
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', media['fragmented'], '-an', '-c', 'copy',
                        '-movflags', '+faststart', '-f', 'mp4', cmd[-1]], check=True, capture_output=True)

    with patch.object(ffmpeg_utils, '_run_ffmpeg_command', side_effect=video_only):
        assert remux_fragmented_mp4_in_place(path) is False
    assert open(path, 'rb').read() == before


# --- the step in the transcription job -------------------------------------

def _recording(path):
    return SimpleNamespace(id=1, audio_path='local://recordings/x/obs.mp4', file_size=os.path.getsize(path))


def test_stored_local_file_is_rewritten_and_size_updated(ctx, media, tmp_path):
    from src.tasks import processing
    path = _copy(media['fragmented'], tmp_path)
    rec = _recording(path)
    storage = SimpleNamespace(resolve_local_filesystem_path=lambda loc: path)
    with patch('src.services.storage.get_storage_service', return_value=storage), \
            patch.object(db.session, 'commit') as commit:
        assert processing.make_retained_video_seekable(rec, path) is True
    commit.assert_called_once()
    assert rec.file_size == os.path.getsize(path)
    assert is_fragmented_mp4(path) is False


def test_downloaded_copy_or_s3_object_is_not_rewritten(ctx, media, tmp_path):
    from src.tasks import processing
    path = _copy(media['fragmented'], tmp_path)
    rec = _recording(path)

    def s3(loc):
        raise ValueError('Locator is not local')

    for storage in (SimpleNamespace(resolve_local_filesystem_path=s3),
                    SimpleNamespace(resolve_local_filesystem_path=lambda loc: str(tmp_path / 'other.mp4'))):
        with patch('src.services.storage.get_storage_service', return_value=storage):
            assert processing.make_retained_video_seekable(rec, path) is False
        assert is_fragmented_mp4(path) is True


def test_transcription_remuxes_only_retained_videos():
    """The call sits behind is_video and effective_video_retention, before the
    passthrough and extraction branches read the file."""
    import inspect
    from src.tasks import processing
    source = inspect.getsource(processing)
    call = source.index('\n                make_retained_video_seekable(recording, filepath)')
    assert 'if is_video and effective_video_retention:' in source[call - 300:call]
    assert call < source.index('Video passthrough: sending original video to ASR (no audio extraction)')
    assert call < source.index('extract_audio_from_video(filepath, cleanup_original=False)')
