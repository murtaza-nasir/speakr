"""Bounded, read-only spectrograms of a local audio window."""
import json
import math
import os
import struct
import subprocess
import threading
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass

_generation = threading.BoundedSemaphore(1)
MAX_WINDOW = 60.0
MAX_PNG = 2 * 1024 * 1024
MAX_TILE_PNG = 8 * 1024 * 1024


def cache_root():
    # One transient root shared by workers; never uploads, instance or the database.
    return Path(tempfile.gettempdir()) / f'speakr-spectrogram-{getattr(os, "getuid", lambda: 0)()}'


@contextmanager
def file_lock(path, wait=None):
    """Kernel-owned lock: process death releases it, including across workers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    if handle.tell() == 0:
        handle.write(b'0'); handle.flush()
    deadline = None if wait is None else time.monotonic() + wait
    acquired = False
    try:
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if deadline is not None and time.monotonic() >= deadline:
                    raise SpectrogramError('busy', 429)
                time.sleep(.02)
        yield
    finally:
        if acquired:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class SpectrogramError(Exception):
    def __init__(self, code, status=422):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class Spectrogram:
    png: bytes
    start: float
    end: float
    duration: float
    channels: int
    max_frequency: float = 8000
    sample_rate: int = 16000
    gain_db: int = 0


def validate_gain(value):
    # Query strings and browser preferences use the same nine bounded levels.
    if isinstance(value, bool) or str(value) not in tuple(str(n) for n in range(0, 41, 5)):
        raise SpectrogramError('gain', 400)
    return int(value)


def validate_frequency(value):
    value = str(value if value is not None else '8000')
    if value not in ('2000', '4000', '8000', 'full'):
        raise SpectrogramError('frequency', 400)
    return value


def validate_window(start, end):
    try:
        start, end = float(start), float(end)
    except (ValueError, TypeError):
        raise SpectrogramError('bounds', 400)
    if not (math.isfinite(start) and math.isfinite(end) and
            0 <= start < end and end - start <= MAX_WINDOW + 1e-8):
        raise SpectrogramError('bounds', 400)
    return start, end


def _run(command, timeout):
    try:
        # run() kills AND waits for the child on timeout. No shell or network protocols.
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SpectrogramError('timeout', 504)
    except FileNotFoundError:
        raise SpectrogramError('unavailable', 503)
    if result.returncode:
        raise SpectrogramError('media')
    return result.stdout


def source_fingerprint(path):
    try:
        stat = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        raise SpectrogramError('missing', 404)
    return [os.path.realpath(path), stat.st_size, stat.st_mtime_ns, stat.st_ino]


def probe_spectrogram(path, frequency, timeout=5):
    raw = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-select_streams', 'a:0', '-show_entries',
                'stream=channels,duration,sample_rate:format=duration', '-of', 'json', path], timeout)
    try:
        metadata = json.loads(raw)
        stream = metadata['streams'][0]
        channels = int(stream['channels'])
        sample_rate = int(stream['sample_rate'])
        duration = float(stream.get('duration') or metadata['format']['duration'])
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        raise SpectrogramError('media')
    if channels not in (1, 2):
        raise SpectrogramError('channels', 422)
    if sample_rate <= 0:
        raise SpectrogramError('media')
    if frequency == 'full' and sample_rate > 192000:
        raise SpectrogramError('frequency')
    output_rate = sample_rate if frequency == 'full' else min(sample_rate, int(frequency) * 2)
    if not math.isfinite(duration) or duration <= 0:
        raise SpectrogramError('media')
    return duration, channels, sample_rate, output_rate


def render_tile(path, start, end, width, metadata, maximum=MAX_TILE_PNG, timeout=15, *, gain_db=0):
    gain_db = validate_gain(gain_db)
    duration, channels, sample_rate, output_rate = metadata
    if not (1 <= width <= 4096 and 0 <= start < end <= duration and end - start <= MAX_WINDOW + 1e-8):
        raise SpectrogramError('bounds', 400)
    height = 256 if channels == 1 else 384
    # showspectrumpic stacks channel zero at the bottom. The editor labels
    # the upper band L and lower band R, so reverse the input stacking only.
    channel_order = 'pan=stereo|c0=c1|c1=c0,' if channels == 2 else ''
    picture = _run([
        'ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
        '-protocol_whitelist', 'file,pipe', '-ss', f'{start:.9f}',
        '-t', f'{end - start:.9f}', '-i', path,
        '-filter_complex_threads', '1', '-filter_complex',
        f'[0:a:0]{channel_order}aresample={output_rate},showspectrumpic=s={width}x{height}:mode=separate:'
        f'legend=0:fscale=lin:scale=log:color=magma:win_func=hann:drange=80:limit={-gain_db}[spectrum]',
        '-map', '[spectrum]', '-an',
        '-frames:v', '1', '-threads', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'
    ], timeout)
    if (len(picture) > maximum or not picture.startswith(b'\x89PNG\r\n\x1a\n') or
            len(picture) < 24 or struct.unpack('>II', picture[16:24]) != (width, height)):
        raise SpectrogramError('media')
    return Spectrogram(picture, start, end, duration, channels, output_rate / 2, sample_rate, gain_db)


def render_spectrogram(path, start, end, frequency=None, *, gain_db=0):
    start, end = validate_window(start, end)
    frequency = validate_frequency(frequency)
    gain_db = validate_gain(gain_db)
    if not _generation.acquire(blocking=False):
        raise SpectrogramError('busy', 429)
    try:
        with file_lock(cache_root() / 'renderer.lock', wait=0):
            before = source_fingerprint(path)
            metadata = probe_spectrogram(path, frequency)
            if start >= metadata[0]:
                raise SpectrogramError('bounds', 400)
            result = render_tile(path, start, min(end, metadata[0]), 1024, metadata, MAX_PNG, gain_db=gain_db)
            try:
                after = source_fingerprint(path)
            except SpectrogramError:
                raise SpectrogramError('changed', 409)
            if before != after:
                raise SpectrogramError('changed', 409)
            return result
    finally:
        _generation.release()
