"""Private leased saved-range playback. No embedding/provider/DB writes."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid
import weakref

from flask import current_app
from src.database import db
from src.services import manual_voice_samples as manual
from src.utils.media_job import process_identity
from src.services.manual_voice_worker import source_digest, MAX_CLIP_BYTES

Error = manual.ManualVoiceError
_managers = weakref.WeakKeyDictionary()
_guard = threading.Lock()


def get_jobs():
    app = current_app._get_current_object()
    with _guard:
        scope, family = manual.default_job_scope()
        if db.engine.dialect.name == 'sqlite' and (db.engine.url.database in ('', None, ':memory:') or db.engine.url.query.get('mode') == 'memory'):
            family = hashlib.sha256((family + manual._memory_engine_scopes[db.engine]).encode()).hexdigest()[:32]
        root = scope.parent / ('speakr-manual-voice-playback-v1-' + family)
        if app not in _managers or _managers[app].root != root:
            _managers[app] = PlaybackJobs(root, family=family)
            _managers[app].start_maintenance(weakref.ref(app))
        return _managers[app]


class PlaybackJobs(manual.ManualJobs):
    reservation = MAX_CLIP_BYTES + 64 * 1024
    budget = 512 * 1024 * 1024
    overhead = 64 * 1024

    def __init__(self, root, deadline=300, ttl=900, family=None):
        super().__init__(root, deadline=deadline, ttl=ttl, family=family,
            worker_command=[sys.executable, '-m', 'src.services.manual_voice_playback_worker'])

    def _prepare(self):
        super()._prepare()
        # Recover bounded atomic owner-marker remnants only after the marker
        # proves this installation owns the namespace, under its writer lock.
        with manual.private_lock(self.root / '.ownership.lock'):
            for path in self.root.glob('.owner-*.tmp'):
                token = path.name[len('.owner-'):-len('.tmp')]
                try:
                    valid = len(token) == 32 and uuid.UUID(token).hex == token
                except ValueError:
                    valid = False
                if not valid or manual.unsafe_path(path) or not path.is_file() or path.stat().st_size > 4096:
                    raise Error('unavailable', 503)
                path.unlink()

    def _load(self, directory):
        path = directory / 'meta.json'
        if manual.unsafe_path(path) or not path.is_file() or path.stat().st_size > 16384:
            raise Error('unavailable', 503)
        try:
            meta = json.loads(path.read_text())
            if meta['job_id'] != directory.name or meta['kind'] != 'playback-v1':
                raise ValueError()
            return meta
        except (KeyError, ValueError):
            raise Error('unavailable', 503)

    def _write(self, directory, metadata):
        if metadata['state'] != 'preparing':
            metadata.setdefault('expires_at', min(metadata['finished'], metadata['created'] + metadata['deadline']) + metadata['ttl'])
        if len(json.dumps(metadata).encode()) > 16384:
            raise Error('unavailable', 503)
        # All calls hold the family metadata lock. Remove only private atomic
        # metadata remnants from a crashed writer before producing another.
        for path in directory.glob('.*.tmp'):
            if manual.unsafe_path(path) or not path.is_file() or path.stat().st_size > 16384 or len(path.stem) != 33:
                raise Error('unavailable', 503)
            try:
                uuid.UUID(path.stem[1:])
            except ValueError:
                raise Error('unavailable', 503)
            path.unlink()
        super()._write(directory, metadata)

    @contextlib.contextmanager
    def admission(self):
        self._prepare()
        with manual.private_lock(self.root / '.metadata.lock'):
            self._cleanup_locked()
            yield

    def _jobs(self):
        # One reservation per owned job directory is the atomic shared ledger.
        # Metadata and bounded clip are charged at their maximum until unlink.
        dirs = []
        for path in self.root.iterdir():
            if path.name.startswith('.'):
                if path.name not in ('.owner.json', '.ownership.lock', '.metadata.lock') or manual.unsafe_path(path) or not path.is_file() or path.stat().st_size > 4096:
                    raise Error('unavailable', 503)
                continue
            if manual.unsafe_path(path) or not path.is_dir() or str(uuid.UUID(path.name)) != path.name:
                raise Error('unavailable', 503)
            dirs.append(path)
            if len(dirs) > self.budget // self.reservation:
                raise Error('limit', 429)
        return dirs

    def _cleanup_locked(self):
        now = time.time()
        for directory in self._jobs():
            meta = self._load(directory)
            alive = process_identity(meta['parent_pid']) == meta['parent_birth']
            if meta['state'] == 'preparing' and (not alive or now >= meta['created'] + meta['deadline']):
                # The worker's process group is verified before orphan cleanup.
                self._terminate_stored(meta)
                meta.update(state='failed', code='unavailable' if not alive else 'timeout', finished=now,
                            expires_at=min(now, meta['created'] + meta['deadline']) + meta['ttl'])
                self._write(directory, meta)
            if meta['state'] != 'preparing' and now >= meta['expires_at']:
                self._purge(directory, meta)

    def _terminate_stored(self, meta):
        pid = meta.get('worker_pid')
        if pid and self._worker_alive(meta):
            import signal
            # A pinned kernel handle closes the PID-check-to-kill race. Media
            # children carry PDEATHSIG, so no unverified group is ever killed.
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                return
            try:
                if process_identity(pid) == meta.get('worker_birth'):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)

    @staticmethod
    def _group_alive(meta):
        if PlaybackJobs._worker_alive(meta):
            return True
        if os.name == 'nt' or not meta.get('worker_birth'):
            return False
        current_birth = process_identity(meta.get('worker_pid', -1))
        if current_birth is not None and current_birth != meta['worker_birth']:
            return False  # A reused leader PID never proves our old group.
        # Linux retains the session/group identity while its descendants live,
        # even after the leader has died. Verify membership and birth before
        # terminating that owned group, never a bare reused PID.
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / 'stat').read_text()
                fields = stat[stat.rfind(')') + 2:].split()
                if fields[0] != 'Z' and int(fields[2]) == meta['worker_pid'] and int(fields[3]) == meta['worker_pid'] and int(fields[19]) >= int(meta['worker_birth']):
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def _purge(self, directory, meta):
        self._terminate_stored(meta)
        # A local supervisor reaps outside the metadata lock. A killed orphan
        # may remain a zombie until PID 1 reaps it; zombies cannot write files.
        until = time.monotonic() + 1
        while self._group_alive(meta) and time.monotonic() < until:
            time.sleep(.01)
        if self._group_alive(meta):
            raise Error('unavailable', 503)
        if manual.unsafe_path(directory) or any(manual.unsafe_path(p) for p in directory.iterdir()):
            raise Error('unavailable', 503)
        shutil.rmtree(directory)
        if directory.exists():
            raise Error('unavailable', 503)

    @staticmethod
    def _worker_alive(meta):
        pid, birth = meta.get('worker_pid'), meta.get('worker_birth')
        if not pid or not birth or process_identity(pid) != birth:
            return False
        if os.name != 'nt':
            try:
                stat = Path(f'/proc/{pid}/stat').read_text()
                return stat[stat.rfind(')') + 2:].split()[0] != 'Z'
            except OSError:
                return False
        return True

    def start(self, identity, path):
        if sys.platform != 'linux' or not hasattr(os, 'pidfd_open'):
            # Runtime is Linux Docker. Native Windows cannot prove descendant
            # containment after a leader crash using taskkill alone.
            raise Error('unsupported', 422)
        encoded_identity = json.dumps(identity, sort_keys=True)
        with self.admission():
            now = time.time()
            for directory in self._jobs():
                meta = self._load(directory)
                if meta['identity'] == encoded_identity and meta['state'] in ('preparing', 'ready'):
                    if meta['state'] == 'ready' and source_digest(path)[0] != identity['digest']:
                        raise Error('changed', 409)
                    return self._lease(directory, meta)
                if meta['user_id'] == identity['user_id'] and meta['state'] == 'preparing':
                    raise Error('busy', 429)
            if self.overhead + (len(self._jobs()) + 1) * self.reservation > self.budget:
                raise Error('limit', 429)
            job_id = str(uuid.uuid4())
            directory = self.directory(job_id)
            directory.mkdir(mode=0o700)
            meta = dict(identity, kind='playback-v1', identity=encoded_identity, job_id=job_id,
                        state='preparing', parent_pid=os.getpid(), parent_birth=process_identity(os.getpid()),
                        created=now, leases={}, ttl=self.ttl, deadline=self.deadline)
            self._write(directory, meta)
            public = self._lease(directory, meta)
            threading.Thread(target=self._run, args=(directory, path), daemon=True).start()
            return public

    def _lease(self, directory, meta):
        now = time.time()
        meta['leases'] = {k: v for k, v in meta['leases'].items() if v > now}
        if len(meta['leases']) >= 8:
            raise Error('limit', 429)
        token = uuid.uuid4().hex
        meta['leases'][token] = min(meta.get('expires_at', meta['created'] + self.deadline + self.ttl), now + self.deadline + self.ttl)
        self._write(directory, meta)
        return {**self.public(meta), 'lease': token}

    def public(self, meta):
        data = {k: meta[k] for k in ('state', 'recording_id', 'start_ms', 'end_ms')}
        data['preview_id'] = meta['job_id']
        if 'code' in meta:
            data['code'] = manual.safe_code(meta['code'])
        if 'expires_at' in meta:
            data['expires_at'] = meta['expires_at'] * 1000
        return data

    def read(self, identity, preview_id, lease, path, release=False, open_audio=False):
        directory = self.directory(preview_id)
        with self.admission():
            if not directory.exists():
                raise Error('expired', 410)
            meta = self._load(directory)
            if meta['identity'] != json.dumps(identity, sort_keys=True) or meta['leases'].get(lease, 0) <= time.time():
                raise Error('missing', 404)
            if release:
                meta['leases'].pop(lease)
                if not any(end > time.time() for end in meta['leases'].values()):
                    (directory / 'cancel').touch(mode=0o600)
                    meta.update(state='cancelled', code='cancelled')
                    meta.setdefault('finished', time.time())
                    meta.setdefault('expires_at', meta['finished'] + meta['ttl'])
                self._write(directory, meta)
                if meta['state']=='cancelled' and not meta.get('worker_birth'):
                    self._purge(directory,meta)
                elif meta['state']=='cancelled' and process_identity(meta.get('worker_pid',-1)) != meta.get('worker_birth'):
                    self._purge(directory,meta)
            elif meta['state'] == 'ready' and source_digest(path)[0] != identity['digest']:
                raise Error('changed', 409)
            if open_audio:
                if meta['state'] != 'ready':
                    raise Error('changed', 409)
                clip = directory / 'clip.wav'
                if manual.unsafe_path(clip) or not clip.is_file():
                    raise Error('unavailable', 503)
                stream = clip.open('rb')
                if os.fstat(stream.fileno()).st_size > MAX_CLIP_BYTES:
                    stream.close()
                    raise Error('unavailable', 503)
                return meta, stream
            return meta, directory

    def _run(self, directory, path):
        process, result = None, {'code': 'unavailable'}
        worker_birth = None
        try:
            with manual.private_lock(self.root / '.metadata.lock'):
                if not directory.exists():
                    return
                meta = self._load(directory)
                if meta['state'] != 'preparing' or (directory / 'cancel').exists() or time.time() >= meta['created'] + meta['deadline']:
                    return
                payload = json.dumps({k: meta[k] for k in ('start_ms', 'end_ms', 'digest')}
                    | {'path': path, 'output': str(directory / 'clip.wav')}).encode()
                environment = os.environ.copy()
                environment['SPEAKR_PLAYBACK_PARENT_PID'] = str(os.getpid())
                environment['SPEAKR_PLAYBACK_PARENT_BIRTH'] = str(process_identity(os.getpid()))
                # Spawn and publish worker ownership atomically with admission.
                process = subprocess.Popen(self.worker_command, stdin=subprocess.PIPE, env=environment,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                    **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {}))
                worker_birth = process_identity(process.pid)
                if not worker_birth:
                    process.terminate()
                    raise Error('unavailable', 503)
                meta.update(worker_pid=process.pid, worker_birth=worker_birth)
                self._write(directory, meta)
            deadline, first = time.monotonic() + max(0, meta['created'] + meta['deadline'] - time.time()), True
            while time.monotonic() < deadline:
                if (directory / 'cancel').exists():
                    result = {'code': 'cancelled'}
                    break
                try:
                    process.communicate(payload if first else None, timeout=.2)
                    target = directory / 'result.json'
                    if process.returncode == 0 and target.is_file() and target.stat().st_size < 4096:
                        result = json.loads(target.read_text())
                    break
                except subprocess.TimeoutExpired:
                    first = False
            else:
                result = {'code': 'timeout'}
        except Exception:
            pass
        finally:
            if process:
                self._terminate_stored({'worker_pid':process.pid, 'worker_birth':worker_birth})
                process.wait(timeout=10)
            with manual.private_lock(self.root / '.metadata.lock'):
                if not directory.exists():
                    return
                meta = self._load(directory)
                cancelled = meta['state'] == 'cancelled' or (directory / 'cancel').exists()
                if meta['state'] == 'failed':
                    result = {'code': meta.get('code', 'unavailable')}
                meta.update(state='cancelled' if cancelled else 'ready' if result.get('ok') else 'failed',
                            finished=time.time())
                if cancelled or not result.get('ok'):
                    meta['code'] = 'cancelled' if cancelled else result.get('code', 'unavailable')
                    (directory / 'clip.wav').unlink(missing_ok=True)
                meta.setdefault('expires_at', min(meta['finished'], meta['created'] + meta['deadline']) + meta['ttl'])
                self._write(directory, meta)
                (directory / 'result.json').unlink(missing_ok=True)

    def cleanup(self, all_jobs=False):
        """Explicit verified owned purge before downgrade; refuses live consumers."""
        with self.admission():
            if all_jobs:
                for directory in self._jobs():
                    meta = self._load(directory)
                    (directory / 'cancel').touch(mode=0o600)
                    self._purge(directory, meta)
            return len(self._jobs())

    def start_maintenance(self, app_ref=None):
        def maintain():
            while app_ref is None or app_ref() is not None:
                try:
                    self.cleanup()
                except Exception:
                    pass  # Admission remains fail-closed on corrupt state.
                time.sleep(30)
        threading.Thread(target=maintain, daemon=True).start()
