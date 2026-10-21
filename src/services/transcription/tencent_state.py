"""Durable Tencent task checkpoints; one OS lock per audio/options fingerprint."""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .exceptions import TranscriptionError


@contextmanager
def locked_checkpoint(directory, fingerprint):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (fingerprint + '.json')
    with (root / (fingerprint + '.lock')).open('a+b') as lock:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise TranscriptionError('This Tencent recording is already being processed; wait before retrying') from None
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TranscriptionError('This Tencent recording is already being processed; wait before retrying') from None
        try:
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
            except (ValueError, OSError):
                raise TranscriptionError('Tencent checkpoint cannot be read; refusing a potentially duplicate submission') from None

            def save():
                temporary = root / (fingerprint + '.' + uuid4().hex + '.tmp')
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w', encoding='utf-8') as output:
                        json.dump(state, output, ensure_ascii=False)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            yield state, save
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def prune_checkpoints(directory, max_age_seconds):
    """Remove checkpoints that have not been written for max_age_seconds.

    A checkpoint holds the full transcription result, and it is kept only to
    recover a task after a worker restart, so it must not outlive Tencent's
    24-hour task retention by much. Returns the staging locators that expired
    checkpoints still held (their objects are left to the bucket lifecycle rule).
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    cutoff = time.time() - max_age_seconds
    leftovers = []
    for path in root.glob('*.json'):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        try:
            with locked_checkpoint(root, path.stem) as (state, _save):
                if state.get('staging_locator'):
                    leftovers.append(state['staging_locator'])
                path.unlink(missing_ok=True)
        except TranscriptionError:
            # In use by another worker, or unreadable (checkpoints are written
            # atomically, so that needs outside interference); left alone.
            continue
    return leftovers
