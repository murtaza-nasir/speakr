"""Private, transient segment ASR jobs shared by HTTP workers. No application DB writes."""
import contextlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from src.services.segment_transcription_worker import source_signature


class SegmentTranscriptionError(Exception):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


def validate_bounds(start, end):
    if isinstance(start, bool) or isinstance(end, bool):
        raise SegmentTranscriptionError('bounds', 400)
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        raise SegmentTranscriptionError('bounds', 400)
    if not (math.isfinite(start) and math.isfinite(end) and start >= 0 and .25 <= end - start <= 300):
        raise SegmentTranscriptionError('bounds', 400)
    return start, end


def process_identity(pid):
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                    return None
                return f'{times[0].dwHighDateTime}:{times[0].dwLowDateTime}'
            finally:
                kernel.CloseHandle(handle)
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat[stat.rfind(')') + 2:].split()[19]  # starttime, unaffected by spaces in comm
    except (OSError, IndexError):
        return None


@contextlib.contextmanager
def file_lock(path, blocking=True):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            # LK_LOCK has a bounded built-in retry; acquisition failure is busy.
            msvcrt.locking(descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        # Closing releases the lock even on cancellation/exception.
        os.close(descriptor)


class SegmentJobs:
    def __init__(self, root=None, deadline=300, worker_command=None, ttl=900):
        self.root = Path(root or Path(tempfile.gettempdir()) / 'speakr-segment-transcriptions')
        self.deadline, self.ttl = deadline, ttl
        self.worker_command = worker_command or [sys.executable, '-m', 'src.services.segment_transcription_worker']
        self._janitor_lock = threading.Lock()
        self._janitor = None

    def _prepare(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or (hasattr(os, 'getuid') and self.root.stat().st_uid != os.getuid()):
            raise SegmentTranscriptionError('unavailable', 503)
        os.chmod(self.root, 0o700)

    def _directory(self, job_id):
        try:
            if str(uuid.UUID(job_id, version=4)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SegmentTranscriptionError('missing', 404)
        directory = self.root / job_id
        if directory.is_symlink():
            raise SegmentTranscriptionError('missing', 404)
        return directory

    def _read(self, directory):
        try:
            return json.loads((directory / 'meta.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise SegmentTranscriptionError('missing', 404)

    def _write(self, directory, metadata):
        temporary = directory / f'.{uuid.uuid4()}.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(metadata, output, ensure_ascii=False)
        os.replace(temporary, directory / 'meta.json')

    def _expire(self, directory, meta):
        if meta['status'] == 'running' and process_identity(meta['pid']) != meta['birth']:
            meta.update(status='failed', code='worker', finished=time.time())
            self._write(directory, meta)
        if meta['status'] != 'running' and time.time() - meta['finished'] >= self.ttl:
            # Owned UUID directories only; do not follow a substituted symlink.
            if not directory.is_symlink() and directory.parent.resolve() == self.root.resolve():
                shutil.rmtree(directory)
            return None
        return meta

    def _cleanup(self):
        completed = []
        for directory in self.root.iterdir():
            try:
                directory = self._directory(directory.name)
                if not directory.is_dir():
                    continue
                meta = self._expire(directory, self._read(directory))
                if meta and meta['status'] != 'running':
                    completed.append((meta['finished'], directory))
            except SegmentTranscriptionError:
                continue
        for _, directory in sorted(completed)[:-32]:
            shutil.rmtree(directory)

    def _start_janitor(self):
        with self._janitor_lock:
            if self._janitor and self._janitor.is_alive():
                return
            def cleanup():
                while True:
                    time.sleep(min(60, max(.1, self.ttl)))
                    try:
                        with file_lock(self.root / '.metadata.lock'):
                            self._cleanup()
                            if not any(d.is_dir() for d in self.root.iterdir()):
                                return
                    except OSError:
                        return
            self._janitor = threading.Thread(target=cleanup, daemon=True, name='segment-asr-cleanup')
            self._janitor.start()

    def start(self, user_id, recording_id, path, start, end, connector, config, params):
        start, end = validate_bounds(start, end)
        try:
            signature = source_signature(path)
        except (OSError, TypeError):
            raise SegmentTranscriptionError('missing', 404)
        self._prepare()
        lease = file_lock(self.root / '.execution.lock', blocking=False)
        try:
            lease.__enter__()
        except OSError:
            raise SegmentTranscriptionError('busy', 429)
        directory, accepted = None, False
        try:
            job_id = str(uuid.uuid4())
            directory = self._directory(job_id)
            directory.mkdir(mode=0o700)
            work = directory / 'work'
            work.mkdir(mode=0o700)
            metadata = {'job_id': job_id, 'user_id': user_id, 'recording_id': recording_id,
                'start': start, 'end': end, 'path': path, 'signature': signature,
                'pid': os.getpid(), 'birth': process_identity(os.getpid()), 'status': 'running'}
            payload = {'path': path, 'signature': signature, 'start': start, 'end': end,
                'connector': connector, 'config': config, 'params': params,
                'work': str(work), 'deadline': self.deadline}
            # Validate serializability before accepting; config stays in memory/stdin.
            encoded = json.dumps(payload).encode('utf-8')
            with file_lock(self.root / '.metadata.lock'):
                self._cleanup()
                self._write(directory, metadata)
            threading.Thread(target=self._supervise, args=(directory, encoded, lease),
                daemon=True, name='segment-asr-job').start()
            accepted = True
            try:
                self._start_janitor()
            except RuntimeError:
                pass  # Subsequent requests also clean expired jobs.
            return self._public(metadata)
        except Exception:
            if not accepted:
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
                lease.__exit__(*sys.exc_info())
            raise

    def _kill(self, process):
        if os.name == 'nt':
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        else:
            # Kill the entire owned session even if its leader exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)

    def _supervise(self, directory, encoded, lease):
        process, result = None, {'code': 'unavailable'}
        try:
            process = subprocess.Popen(self.worker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {}))
            deadline = time.monotonic() + self.deadline
            first = True
            while True:
                if (directory / 'cancel').exists():
                    result = {'code': 'cancelled'}
                    break
                if time.monotonic() >= deadline:
                    result = {'code': 'timeout'}
                    break
                try:
                    stdout, _ = process.communicate(input=encoded if first else None, timeout=.25)
                    if process.returncode == 0 and len(stdout) <= 96 * 1024:
                        result = json.loads(stdout)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if (directory / 'cancel').exists():
                result = {'code': 'cancelled'}
        except Exception:
            result = {'code': 'unavailable'}
        finally:
            try:
                if process is not None:
                    self._kill(process)
            finally:
                try:
                    shutil.rmtree(directory / 'work', ignore_errors=True)
                    with file_lock(self.root / '.metadata.lock'):
                        metadata = self._read(directory)
                        if (directory / 'cancel').exists():
                            result = {'code': 'cancelled'}
                        text = result.get('text')
                        if isinstance(text, str) and text.strip() and len(text.encode('utf-8')) <= 64 * 1024:
                            metadata.update(status='done', text=text.strip(), finished=time.time())
                        else:
                            code = result.get('code', 'provider')
                            allowed = {'bounds', 'media', 'channels', 'size', 'empty', 'changed', 'missing',
                                'provider', 'timeout', 'cancelled', 'unavailable'}
                            metadata.update(status='cancelled' if code == 'cancelled' else 'failed',
                                code=code if code in allowed else 'provider', finished=time.time())
                        self._write(directory, metadata)
                        self._cleanup()
                finally:
                    lease.__exit__(None, None, None)

    def _public(self, meta):
        return {key: meta[key] for key in ('job_id', 'status', 'start', 'end', 'text', 'code') if key in meta}

    def get(self, user_id, recording_id, job_id, path, cancel=False):
        self._prepare()
        directory = self._directory(job_id)
        with file_lock(self.root / '.metadata.lock'):
            metadata = self._read(directory)
            if metadata['user_id'] != user_id or metadata['recording_id'] != recording_id:
                raise SegmentTranscriptionError('missing', 404)
            metadata = self._expire(directory, metadata)
            if not metadata:
                raise SegmentTranscriptionError('missing', 404)
            if cancel:
                (directory / 'cancel').touch(mode=0o600)
                if metadata['status'] != 'running':
                    metadata.update(status='cancelled', code='cancelled')
                    metadata.pop('text', None)
                    self._write(directory, metadata)
                return {**self._public(metadata), 'status': 'cancelled'}
            try:
                changed = path != metadata['path'] or source_signature(path) != metadata['signature']
            except (OSError, TypeError):
                changed = True
            if changed:
                (directory / 'cancel').touch(mode=0o600)
                return {'job_id': job_id, 'status': 'failed', 'code': 'changed'}
            return self._public(metadata)


segment_jobs = SegmentJobs()
