"""Waveform peaks of a recording's audio, for the timeline of the segment editor.

A peaks file holds the audio as min/max pairs at several resolutions: a base
level of at most ``MAX_RATE`` buckets per second and coarser levels, each a
quarter of the one before, down to about a thousand buckets. The timeline
draws from the level that has one to four buckets per pixel, so a ten-hour
recording is drawn as fast as a short one.

Computing: ffmpeg decodes the audio to 8 kHz mono PCM on a pipe and the
samples are reduced chunk by chunk, so the decoded audio is never held in
memory (ten hours are 3.6 million base buckets, a few megabytes). A long file
takes a while, so the work runs in a thread of the process that got the
request and the route answers 202 until the file is there. A lock file
(flock) keeps the other gunicorn workers from computing the same file.

Caching: UPLOAD_FOLDER/_peaks/<recording id>-<fingerprint>.bin. The
fingerprint covers the stored locator and the file's size and modification
time, so another audio file (or a changed one) gets a new peaks file and the
old one is removed. remove_peaks() deletes them with the audio.

File layout (little endian):

    4 bytes   magic "SPK1"
    4 bytes   length of the header (uint32)
    N bytes   header, JSON: {"version", "duration", "levels": [{"rate", "length", "offset"}]}
    data      per level, `length` pairs of int8 (min, max); `offset` is the
              level's first byte counted from the start of the data

`rate` is buckets per second. The values are scaled so that the loudest
sample of the recording is 127.
"""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import logging
import os
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MAGIC = b'SPK1'
VERSION = 1
CACHE_FOLDER = '_peaks'
SAMPLE_RATE = 8000
MAX_RATE = 100                  # base buckets per second of a short recording
SAMPLES_PER_BUCKET = SAMPLE_RATE // MAX_RATE
MAX_BASE_BUCKETS = 1_440_000    # four hours at MAX_RATE; longer audio gets a coarser base level
LEVEL_FACTOR = 4
SMALLEST_LEVEL = 1024
CHUNK_BUCKETS = 8192            # buckets reduced per read (about 82 s of audio)
MAX_GAIN = 64.0                 # near-silence is not blown up into noise
MAX_CONCURRENT = 2              # computations per process
QUICK_WAIT_SECONDS = 1.5        # a short file is answered in the first request
FFMPEG_TIMEOUT = 3600
FAILED_RETRY_SECONDS = 600      # a failed computation is tried again after this long

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class PeaksError(Exception):
    """The audio could not be turned into peaks."""


@dataclass
class PeaksResult:
    status: str                 # ready | pending | unavailable
    path: Optional[str] = None
    etag: Optional[str] = None


# ---- computing ---------------------------------------------------------------

def _reduce(mins, maxs, factor):
    """Join every `factor` buckets into one."""
    count = len(mins)
    if count == 0 or factor <= 1:
        return mins, maxs
    pad = (-count) % factor
    if pad:
        mins = np.concatenate([mins, np.full(pad, mins[-1], dtype=mins.dtype)])
        maxs = np.concatenate([maxs, np.full(pad, maxs[-1], dtype=maxs.dtype)])
    return mins.reshape(-1, factor).min(axis=1), maxs.reshape(-1, factor).max(axis=1)


def reduce_pcm(chunks):
    """Min and max per bucket of a stream of 16-bit mono PCM byte chunks.

    Returns (mins, maxs) as int16 arrays with one entry per
    SAMPLES_PER_BUCKET samples. A last, shorter bucket is kept.
    """
    step = SAMPLES_PER_BUCKET
    mins, maxs = [], []
    rest = b''
    for chunk in chunks:
        if not chunk:
            continue
        data = rest + chunk if rest else chunk
        usable = (len(data) // (2 * step)) * 2 * step
        rest = data[usable:]
        if usable:
            samples = np.frombuffer(data, dtype='<i2', count=usable // 2).reshape(-1, step)
            mins.append(samples.min(axis=1))
            maxs.append(samples.max(axis=1))
    tail = np.frombuffer(rest, dtype='<i2', count=len(rest) // 2)
    if tail.size:
        mins.append(np.array([tail.min()], dtype=np.int16))
        maxs.append(np.array([tail.max()], dtype=np.int16))
    if not mins:
        return np.zeros(0, dtype=np.int16), np.zeros(0, dtype=np.int16)
    return np.concatenate(mins), np.concatenate(maxs)


def build_levels(mins, maxs):
    """The pyramid of a base level: [(rate, mins int8, maxs int8)], finest first."""
    rate = float(MAX_RATE)
    if len(mins) > MAX_BASE_BUCKETS:
        factor = -(-len(mins) // MAX_BASE_BUCKETS)
        mins, maxs = _reduce(mins, maxs, factor)
        rate /= factor
    peak = int(max(abs(int(mins.min())), abs(int(maxs.max())))) if len(mins) else 0
    gain = min(MAX_GAIN, 32767.0 / peak) if peak > 0 else 1.0
    scale = gain * 127.0 / 32767.0

    def to_int8(values):
        return np.clip(np.rint(values.astype(np.float32) * scale), -127, 127).astype(np.int8)

    levels = [(rate, to_int8(mins), to_int8(maxs))]
    while len(levels[-1][1]) > SMALLEST_LEVEL:
        rate, low, high = levels[-1]
        low, high = _reduce(low, high, LEVEL_FACTOR)
        levels.append((rate / LEVEL_FACTOR, low, high))
    return levels


def encode_peaks(levels, duration):
    """The bytes of a peaks file."""
    blocks, described, offset = [], [], 0
    for rate, low, high in levels:
        pairs = np.empty(len(low) * 2, dtype=np.int8)
        pairs[0::2] = low
        pairs[1::2] = high
        blocks.append(pairs.tobytes())
        described.append({'rate': rate, 'length': int(len(low)), 'offset': offset})
        offset += len(pairs)
    header = json.dumps({'version': VERSION, 'duration': round(float(duration), 3), 'levels': described},
                        separators=(',', ':')).encode('utf-8')
    return MAGIC + struct.pack('<I', len(header)) + header + b''.join(blocks)


def decode_peaks(data):
    """A peaks file as {'duration', 'levels': [{'rate', 'min', 'max'}]} (for tests and tools)."""
    if data[:4] != MAGIC:
        raise PeaksError('not a peaks file')
    (length,) = struct.unpack('<I', data[4:8])
    header = json.loads(data[8:8 + length].decode('utf-8'))
    start = 8 + length
    levels = []
    for level in header['levels']:
        pairs = np.frombuffer(data, dtype=np.int8, count=level['length'] * 2, offset=start + level['offset'])
        levels.append({'rate': level['rate'], 'min': pairs[0::2], 'max': pairs[1::2]})
    return {'duration': header['duration'], 'levels': levels}


def _pcm_chunks(process):
    size = CHUNK_BUCKETS * SAMPLES_PER_BUCKET * 2
    while True:
        chunk = process.stdout.read(size)
        if not chunk:
            return
        yield chunk


def compute_peaks(audio_path):
    """Decode a media file with ffmpeg and return the bytes of its peaks file."""
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', audio_path,
               '-map', '0:a:0', '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 's16le', '-']
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except FileNotFoundError as error:
        raise PeaksError('ffmpeg is not installed') from error
    # ffmpeg writes little to stderr at -v error, but a full pipe would stop it
    errors = []
    reader = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
    reader.start()
    killer = threading.Timer(FFMPEG_TIMEOUT, process.kill)
    killer.daemon = True
    killer.start()
    try:
        mins, maxs = reduce_pcm(_pcm_chunks(process))
        code = process.wait()
    finally:
        killer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=5)
    if code != 0 or len(mins) == 0:
        detail = (errors[0] if errors else b'').decode('utf-8', 'replace').strip()[-300:]
        raise PeaksError(f'ffmpeg could not decode the audio (exit {code}): {detail}')
    duration = len(mins) * SAMPLES_PER_BUCKET / SAMPLE_RATE
    return encode_peaks(build_levels(mins, maxs), duration)


# ---- cache -------------------------------------------------------------------

def cache_dir(upload_folder):
    return os.path.join(upload_folder, CACHE_FOLDER)


def fingerprint(storage, audio_path):
    """What identifies the stored audio: its locator, size and (local files) modification time."""
    parts = [f'v{VERSION}', str(audio_path)]
    try:
        locator = storage.parse_locator(audio_path)
        if locator is not None and locator.is_local:
            stat = os.stat(storage.resolve_local_filesystem_path(audio_path))
            parts += [str(stat.st_size), str(stat.st_mtime_ns)]
        else:
            resolved = storage._resolve_backend_for_locator(audio_path)
            stat = resolved.backend.stat(resolved.locator)
            parts += [str(stat.size), str(stat.etag or ''), str(stat.last_modified or '')]
    except Exception:
        return None
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:20]


def remove_peaks(upload_folder, recording_id, keep=None):
    """Delete the peaks files of a recording (all but `keep`). Never raises."""
    if not upload_folder:
        return
    try:
        for path in glob.glob(os.path.join(cache_dir(upload_folder), f'{int(recording_id)}-*')):
            if keep and os.path.basename(path).startswith(os.path.basename(keep)):
                continue
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception as error:  # a stray cache file must not fail a delete
        logger.warning('Could not remove the peaks of recording %s: %s', recording_id, error)


# Cache folders already reported as unwritable by this process, so that the
# warning is logged once and not on every request for peaks.
_unwritable_reported = set()


def _report_unwritable(folder, error, log):
    if folder in _unwritable_reported:
        return
    _unwritable_reported.add(folder)
    log.warning('Waveform peaks are unavailable: the cache folder %s cannot be written (%s). '
                'The segment editor is shown without a waveform.', folder, error)


def _failed_recently(target):
    try:
        return time.time() - os.path.getmtime(f'{target}.failed') < FAILED_RETRY_SECONDS
    except OSError:
        return False


def _write_atomic(path, data):
    temporary = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(temporary, 'wb') as handle:
        handle.write(data)
    os.replace(temporary, path)


def _compute_into(storage, audio_path, target, lock_handle, upload_folder, recording_id, log):
    try:
        try:
            # Another process may have finished between the check and the lock
            if not os.path.exists(target):
                with storage.materialize(audio_path) as materialized:
                    data = compute_peaks(materialized.local_path)
                _write_atomic(target, data)
                try:
                    os.remove(f'{target}.failed')
                except OSError:
                    pass
                remove_peaks(upload_folder, recording_id, keep=target)
        except Exception as error:
            log.warning('No waveform peaks for recording %s: %s', recording_id, error)
            try:
                _write_atomic(f'{target}.failed', str(error)[:500].encode('utf-8'))
            except OSError:
                pass
    finally:
        try:
            os.remove(f'{target}.lock')
        except OSError:
            pass
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
        except OSError:
            pass
        _slots.release()


def get_peaks(storage, upload_folder, recording_id, audio_path, log=logger, wait=None):
    """The peaks file of a recording's audio, starting its computation when there is none.

    Returns a PeaksResult: 'ready' with the file's path, 'pending' while it
    is computed (ask again), or 'unavailable' when the audio is missing or
    cannot be decoded, or when the cache folder cannot be written (a
    read-only UPLOAD_FOLDER). A peaks file that is already there is served
    from a read-only folder as well.
    """
    if not audio_path:
        return PeaksResult('unavailable')
    try:
        if not storage.exists(audio_path):
            return PeaksResult('unavailable')
    except Exception:
        return PeaksResult('unavailable')
    mark = fingerprint(storage, audio_path)
    if mark is None:
        return PeaksResult('unavailable')
    folder = cache_dir(upload_folder)
    target = os.path.join(folder, f'{int(recording_id)}-{mark}.bin')
    if os.path.exists(target):
        return PeaksResult('ready', target, mark)
    if _failed_recently(target):
        return PeaksResult('unavailable')

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        _report_unwritable(folder, error, log)
        return PeaksResult('unavailable')
    if not _slots.acquire(blocking=False):
        return PeaksResult('pending')
    try:
        handle = open(f'{target}.lock', 'a')
    except OSError as error:
        # The folder is there but cannot be written (read-only storage, or
        # another owner). Checked before any audio is decoded.
        _slots.release()
        _report_unwritable(folder, error, log)
        return PeaksResult('unavailable')
    _unwritable_reported.discard(folder)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another worker is computing this file
        handle.close()
        _slots.release()
        return PeaksResult('pending')
    worker = threading.Thread(
        target=_compute_into, name=f'peaks-{recording_id}', daemon=True,
        args=(storage, audio_path, target, handle, upload_folder, recording_id, log))
    worker.start()
    worker.join(timeout=QUICK_WAIT_SECONDS if wait is None else wait)
    if os.path.exists(target):
        return PeaksResult('ready', target, mark)
    if _failed_recently(target):
        return PeaksResult('unavailable')
    return PeaksResult('pending')
