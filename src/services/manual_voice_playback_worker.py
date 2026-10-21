"""DB/provider-free, bounded streaming PCM for a saved sample."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import wave
import ctypes

from src.services.manual_voice_worker import source_digest, RangeError, MAX_CLIP_BYTES

_libc = ctypes.CDLL(None, use_errno=True) if os.name != 'nt' else None
_prctl = getattr(_libc, 'prctl', None)


def parent_death_options():
    """Kernel containment for both media children if their worker disappears.

    The fork callback uses only preloaded libc/os calls, no imports or locks.
    The parent check closes the fork-to-prctl parent-exit race.
    """
    if _prctl is None:
        raise RangeError('unsupported')
    parent = os.getpid()
    def guard():
        if _prctl(1, 9, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(70)
    return {'preexec_fn': guard}


def crop_streaming(path, output, start_ms, end_ms):
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or not 0 < end_ms - start_ms <= 300000):
        raise RangeError('bounds')
    try:
        info = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-select_streams', 'a:0', '-show_entries', 'stream=sample_rate,channels',
            '-of', 'json', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, timeout=10, **parent_death_options())
        if len(info.stdout) > 4096:
            raise RangeError('media')
        stream = json.loads(info.stdout)['streams'][0]
        rate, channels = int(stream['sample_rate']), int(stream['channels'])
        if rate <= 0 or channels not in (1, 2):
            raise RangeError('channels')
        first, last = start_ms * rate // 1000, (end_ms * rate + 999) // 1000
        expected = (last - first) * channels * 2
        if expected + 44 > MAX_CLIP_BYTES:
            raise RangeError('size')
        process = subprocess.Popen(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
            '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:a:0', '-vn',
            '-af', f'atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS',
            '-c:a', 'pcm_s16le', '-threads', '1', '-f', 's16le', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **parent_death_options())
        expired = threading.Event()
        def timeout():
            expired.set()
            if process.poll() is None:
                process.kill()
        timer = threading.Timer(90, timeout)
        timer.start()
        count = 0
        try:
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as raw, wave.open(raw, 'wb') as audio:
                audio.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > expected or count + 44 > MAX_CLIP_BYTES:
                        raise RangeError('size')
                    audio.writeframesraw(chunk)
            if process.wait(timeout=10) != 0 or expired.is_set():
                raise RangeError('timeout' if expired.is_set() else 'media')
            if count != expected:
                raise RangeError('bounds')
            return {'rate': rate, 'channels': channels, 'frames': last - first}
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdout.close()
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError, wave.Error):
        raise RangeError('media')


def main():
    # The manager sends no payload until it has published process ownership.
    # Independent bounds also cover a lost parent while reading/hash/decoding.
    parent_pid = int(os.environ.get('SPEAKR_PLAYBACK_PARENT_PID', os.getppid()))
    from src.utils.media_job import process_identity
    parent_birth = os.environ.get('SPEAKR_PLAYBACK_PARENT_BIRTH', process_identity(parent_pid))
    deadline = time.monotonic() + 310
    def supervise_parent():
        while True:
            if time.monotonic() >= deadline or process_identity(parent_pid) != parent_birth:
                if os.name != 'nt' and os.getpgrp() == os.getpid():
                    import signal
                    os.killpg(os.getpid(), signal.SIGKILL)
                elif os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(os.getpid()), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                os._exit(70)
            time.sleep(.2)
    threading.Thread(target=supervise_parent, daemon=True).start()
    result = {'code': 'unavailable'}
    payload = None
    try:
        encoded = sys.stdin.buffer.read(16385)
        if len(encoded) > 16384:
            raise RangeError('bounds')
        payload = json.loads(encoded)
        before = source_digest(payload['path'])
        if before[0] != payload['digest']:
            raise RangeError('changed')
        result = crop_streaming(payload['path'], payload['output'], payload['start_ms'], payload['end_ms'])
        if source_digest(payload['path']) != before:
            raise RangeError('changed')
        result['ok'] = True
    except RangeError as error:
        result = {'code': error.code}
    except Exception:
        result = {'code': 'unavailable'}
    if payload:
        target = Path(payload['output']).parent / 'result.json'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream)


if __name__ == '__main__':
    main()
