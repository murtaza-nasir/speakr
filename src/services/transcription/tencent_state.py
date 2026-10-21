"""Durable Tencent task checkpoints; one OS lock per audio/options fingerprint."""
import json
import os
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
