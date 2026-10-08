"""Transient, worker-shared full-range spectra. Reads never run media commands.

The JSON ledger and artifact publication use the same kernel lock. Renderers
hold a separate shared lock, also used by the legacy endpoint. No application
database, scheduler, or persistent upload files participate in this cache.
"""
import hashlib
import json
import math
import os
import secrets
import shutil
import threading
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path

from . import segment_spectrogram as spectra

MAX_BYTES = 512 * 1024 * 1024
MAX_TILES = 4096
MAX_QUEUE = 2
QUEUE_SECONDS = 30
PREPARE_SECONDS = 180
LEASE_SECONDS = 120
TTL_SECONDS = 30 * 60


def _process_alive(pid):
    if os.name == 'nt':
        # os.kill(pid, 0) terminates processes on Windows; query instead.
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class SpectrogramCache:
    def __init__(self, root=None, maintenance=False):
        self.root = Path(root) if root is not None else spectra.cache_root()
        self.maintenance = maintenance
        self._maintenance_lock = threading.Lock()
        self._maintenance_started = False

    def _start_maintenance(self):
        if not self.maintenance:
            return
        with self._maintenance_lock:
            if self._maintenance_started:
                return
            self._maintenance_started = True
            def cleanup():
                while True:
                    time.sleep(30)
                    try:
                        with self._state():
                            pass
                    except (OSError, ValueError):
                        # A removed transient root or shutdown does not affect
                        # source data. Next request still validates the ledger.
                        pass
            threading.Thread(target=cleanup, daemon=True, name='spectrogram-cache-cleanup').start()

    @contextmanager
    def _state(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with spectra.file_lock(self.root / 'state.lock'):
            path = self.root / 'state.json'
            state = json.loads(path.read_text()) if path.exists() else {'jobs': {}}
            self._sweep(state)
            try:
                yield state
            finally:
                temporary = self.root / 'state.new'
                with temporary.open('w') as file:
                    json.dump(state, file, separators=(',', ':'))
                    file.flush(); os.fsync(file.fileno())
                os.replace(temporary, path)

    def _sweep(self, state):
        now = time.time()
        for identifier, job in list(state['jobs'].items()):
            job['leases'] = {token: lease for token, lease in job['leases'].items() if lease['until'] > now}
            if job.get('worker_active') and not _process_alive(job['pid']):
                job['worker_active'] = False
            # Readers are bounded by the request lifetime, with a crash fallback.
            for token, reader in list(job.get('readers', {}).items()):
                if not _process_alive(reader['pid']):
                    del job['readers'][token]
            if job['status'] in ('queued', 'preparing'):
                limit = QUEUE_SECONDS if job['status'] == 'queued' else PREPARE_SECONDS
                timestamp = job.get('began', job['created'])
                alive = _process_alive(job['pid'])
                if not alive or now - timestamp > limit:
                    job['status'] = 'failed'
                    job['code'] = 'timeout'
                    if not alive:
                        job['reserve'] = 0
                elif not job['leases']:
                    job['status'] = 'cancelled'
            if job['status'] in ('failed', 'cancelled') and job.get('reserve', 0) and not _process_alive(job['pid']):
                job['reserve'] = 0
            if job['status'] in ('failed', 'cancelled') and not job['readers'] and not job.get('reserve', 0):
                shutil.rmtree(self.root / identifier, ignore_errors=True)
                job['bytes'] = 0
                if job['status'] == 'cancelled' and not job['leases'] and not job.get('worker_active'):
                    self._remove(state, identifier)
                    continue
            if not job['leases'] and not job['readers'] and not job.get('worker_active') and job['status'] not in ('queued', 'preparing'):
                if now - job['used'] >= TTL_SECONDS:
                    self._remove(state, identifier)

    def _remove(self, state, identifier):
        job = state['jobs'][identifier]
        if job['leases'] or job['readers'] or job.get('reserve', 0) or job.get('worker_active'):
            return False
        shutil.rmtree(self.root / identifier, ignore_errors=True)
        del state['jobs'][identifier]
        return True

    def _reserve(self, state, identifier):
        total = self._footprint(state)
        for candidate, job in sorted(list(state['jobs'].items()), key=lambda pair: pair[1]['used']):
            if total + spectra.MAX_TILE_PNG <= MAX_BYTES:
                break
            if candidate != identifier and job['status'] == 'ready' and not job['leases'] and not job['readers']:
                self._remove(state, candidate)
                total = self._footprint(state)
        if total + spectra.MAX_TILE_PNG > MAX_BYTES:
            raise spectra.SpectrogramError('limit', 413)
        state['jobs'][identifier]['reserve'] = spectra.MAX_TILE_PNG
        state['peakBytes'] = max(state.get('peakBytes', 0), total + spectra.MAX_TILE_PNG)

    @staticmethod
    def _footprint(state):
        # Both old and replacement ledgers exist during atomic publication;
        # include metadata and small lock/filesystem bookkeeping in the quota.
        return (65536 + 2 * len(json.dumps(state, separators=(',', ':')).encode()) +
                sum(job['bytes'] + job.get('reserve', 0) for job in state['jobs'].values()))

    @staticmethod
    def _bounds(start, end, span):
        try:
            start, end, span = float(start), float(end), float(span)
        except (TypeError, ValueError):
            raise spectra.SpectrogramError('bounds', 400)
        if not (all(math.isfinite(value) for value in (start, end, span)) and 0 <= start < end and .25 <= span <= max(60, end-start)):
            raise spectra.SpectrogramError('bounds', 400)
        # Stable float normalization independent of viewport location.
        return start, end, round(span, 9)

    def _result(self, identifier, job, token):
        result = {'id': identifier, 'status': job['status'], 'lease': token,
                  'leaseSeconds': LEASE_SECONDS, 'manifest': job.get('manifest')}
        result['progress'] = job.get('progress', {'done': 0, 'total': 0})
        if job.get('code'):
            result['code'] = job['code']
        return result

    def prepare(self, principal, recording, path, start, end, frequency, span, existing_id=None, *, gain_db=0):
        start, end, span = self._bounds(start, end, span)
        frequency = spectra.validate_frequency(frequency)
        gain_db = spectra.validate_gain(gain_db)
        try:
            fingerprint = spectra.source_fingerprint(path)
        except spectra.SpectrogramError:
            if existing_id is not None:
                raise spectra.SpectrogramError('expired', 410)
            raise
        if existing_id is None and math.ceil((end - start) / min(4 * span, 60)) > MAX_TILES:
            raise spectra.SpectrogramError('limit', 413)
        key = hashlib.sha256(json.dumps(['gain-v1', str(principal), recording, fingerprint, start, end, frequency, span, gain_db]).encode()).hexdigest()
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._state() as state:
            if existing_id is not None:
                if not isinstance(existing_id, str):
                    raise spectra.SpectrogramError('expired', 410)
                job = state['jobs'].get(existing_id)
                if not job or job['key'] != key or job['status'] != 'ready':
                    raise spectra.SpectrogramError('expired', 410)
                job['leases'][token] = {'principal': str(principal), 'until': now + LEASE_SECONDS}
                job['used'] = now
                return self._result(existing_id, job, token)
            for identifier, job in state['jobs'].items():
                if job['key'] == key and job['status'] in ('ready', 'queued', 'preparing'):
                    job['leases'][token] = {'principal': str(principal), 'until': now + LEASE_SECONDS}
                    job['used'] = now
                    return self._result(identifier, job, token)
            pending = [job for job in state['jobs'].values() if job.get('worker_active', job['status'] in ('queued', 'preparing'))]
            if any(job['principal'] == str(principal) for job in pending):
                raise spectra.SpectrogramError('busy', 429)
            if sum(job.get('worker_stage', 'rendering' if job['status'] == 'preparing' else 'waiting') == 'waiting' for job in pending) >= MAX_QUEUE:
                raise spectra.SpectrogramError('busy', 429)
            identifier = secrets.token_urlsafe(24)
            job = {'key': key, 'principal': str(principal), 'recording': recording,
                   'fingerprint': fingerprint, 'status': 'queued', 'created': now, 'used': now,
                   'pid': os.getpid(), 'bytes': 0, 'reserve': 0, 'readers': {},
                   'worker_active': True, 'worker_stage': 'waiting',
                   'leases': {token: {'principal': str(principal), 'until': now + LEASE_SECONDS}}}
            state['jobs'][identifier] = job
        self._start_maintenance()
        try:
            threading.Thread(target=self._prepare, args=(identifier, path, start, end, frequency, span, gain_db),
                             daemon=True, name='spectrogram-prepare').start()
        except Exception:
            with self._state() as state:
                failed = state['jobs'][identifier]
                failed['worker_active'] = False; failed['worker_stage'] = 'done'
                failed['status'] = 'failed'; failed['code'] = 'unavailable'
            raise spectra.SpectrogramError('unavailable', 503)
        return self._result(identifier, job, token)

    @contextmanager
    def _renderer(self, identifier):
        queued = time.monotonic()
        with ExitStack() as stack:
            while True:
                with self._state() as state:
                    self._active(state, identifier, queued)
                try:
                    stack.enter_context(spectra.file_lock(self.root / 'renderer.lock', wait=0))
                    break
                except spectra.SpectrogramError as error:
                    if error.code != 'busy':
                        raise
                    if time.monotonic() - queued >= QUEUE_SECONDS:
                        raise spectra.SpectrogramError('timeout', 504)
                    time.sleep(.02)
            yield

    def _active(self, state, identifier, started):
        job = state['jobs'].get(identifier)
        if not job or job['status'] not in ('queued', 'preparing') or not job['leases']:
            raise spectra.SpectrogramError('expired', 410)
        if time.monotonic() - started > PREPARE_SECONDS:
            raise spectra.SpectrogramError('timeout', 504)
        return job

    def _prepare(self, identifier, path, start, end, frequency, span, gain_db=0):
        started = time.monotonic()
        directory = self.root / identifier
        try:
            # Blocking is in this daemon only; API requests never wait on FFT.
            with self._renderer(identifier):
                with self._state() as state:
                    job = self._active(state, identifier, started)
                    job['status'] = 'preparing'; job['began'] = time.time(); job['worker_stage'] = 'rendering'
                    fingerprint = job['fingerprint']
                started = time.monotonic()
                if spectra.source_fingerprint(path) != fingerprint:
                    raise spectra.SpectrogramError('changed', 409)
                metadata = spectra.probe_spectrogram(path, frequency)
                duration, channels, sample_rate, output_rate = metadata
                if start >= duration:
                    raise spectra.SpectrogramError('bounds', 400)
                segment_start, segment_end = start, min(end, duration)
                display_end = segment_end
                display_start = start
                if segment_end - start < .25:
                    display_start = max(0., min(start - (.25 - (segment_end - start)) / 2, duration - .25))
                    display_end = min(duration, display_start + .25)
                tile_span = min(4 * span, 60.)
                count = math.ceil((display_end - display_start) / tile_span)
                if count > MAX_TILES:
                    raise spectra.SpectrogramError('limit', 413)
                directory.mkdir(exist_ok=True)
                tiles = []
                with self._state() as state:
                    self._active(state, identifier, started)['progress'] = {'done': 0, 'total': count}
                for index in range(count):
                    tile_start = display_start + index * tile_span
                    tile_end = min(display_end, display_start + (index + 1) * tile_span)
                    width = min(4096, max(1, math.ceil((tile_end - tile_start) / span * 1024 - 1e-7)))
                    with self._state() as state:
                        self._active(state, identifier, started)
                        self._reserve(state, identifier)
                        state['renderCount'] = state.get('renderCount', 0) + 1
                    remaining = PREPARE_SECONDS - (time.monotonic() - started)
                    if remaining <= 0:
                        raise spectra.SpectrogramError('timeout', 504)
                    rendered = spectra.render_tile(path, tile_start, tile_end, width, metadata,
                                                   timeout=min(15, remaining), gain_db=gain_db)
                    if spectra.source_fingerprint(path) != fingerprint:
                        raise spectra.SpectrogramError('changed', 409)
                    with self._state() as state:
                        job = self._active(state, identifier, started)
                        # Reservation includes output while in memory and before
                        # atomic file publication; readers cannot see partials.
                        temporary = directory / f'{index}.new'
                        temporary.write_bytes(rendered.png)
                        os.replace(temporary, directory / f'{index}.png')
                        job['bytes'] += len(rendered.png); job['reserve'] = 0
                        job['progress']['done'] = index + 1
                    tiles.append({'index': index, 'start': tile_start, 'end': tile_end, 'width': width,
                                  'contentStart': max(tile_start, segment_start), 'contentEnd': min(tile_end, segment_end)})
                with self._state() as state:
                    job = self._active(state, identifier, started)
                    if spectra.source_fingerprint(path) != fingerprint:
                        raise spectra.SpectrogramError('changed', 409)
                    job['manifest'] = {'start': display_start, 'end': display_end, 'duration': duration,
                                       'segmentStart': segment_start, 'segmentEnd': segment_end, 'span': span,
                                       'plotWidth': 1024, 'channels': channels, 'sampleRate': sample_rate,
                                       'maxFrequency': output_rate / 2, 'gainDb': gain_db, 'tiles': tiles}
                    if self._footprint(state) > MAX_BYTES:
                        raise spectra.SpectrogramError('limit', 413)
                    job['status'] = 'ready'; job['used'] = time.time()
        except spectra.SpectrogramError as error:
            with self._state() as state:
                job = state['jobs'].get(identifier)
                if job:
                    if job['status'] in ('queued', 'preparing'):
                        job['status'] = 'cancelled' if error.status == 410 else 'failed'
                        job['code'] = 'timeout' if error.code == 'busy' else error.code
                    job['reserve'] = 0; job['bytes'] = 0
                    shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            with self._state() as state:
                job = state['jobs'].get(identifier)
                if job:
                    if job['status'] in ('queued', 'preparing'):
                        job['status'] = 'failed'; job['code'] = 'unavailable'
                    job['reserve'] = 0; job['bytes'] = 0
                    shutil.rmtree(directory, ignore_errors=True)
        finally:
            with self._state() as state:
                job = state['jobs'].get(identifier)
                if job:
                    job['worker_active'] = False; job['worker_stage'] = 'done'
                    if job['status'] == 'cancelled' and not job['leases'] and not job['readers'] and not job['reserve']:
                        self._remove(state, identifier)

    def _get(self, state, principal, recording, identifier, token, path=None):
        job = state['jobs'].get(identifier)
        if not job or job['status'] == 'cancelled':
            raise spectra.SpectrogramError('expired', 410)
        lease = job['leases'].get(token)
        if job['recording'] != recording or not lease or lease['principal'] != str(principal):
            raise spectra.SpectrogramError('expired', 410)
        if path is not None:
            try:
                unchanged = spectra.source_fingerprint(path) == job['fingerprint']
            except spectra.SpectrogramError:
                unchanged = False
            if not unchanged:
                job['status'] = 'cancelled'; job['code'] = 'changed'
                raise spectra.SpectrogramError('changed', 410)
        job['used'] = time.time()
        return job

    def metadata(self, principal, recording, identifier, path, token):
        with self._state() as state:
            return self._result(identifier, self._get(state, principal, recording, identifier, token, path), token)

    def renew(self, principal, recording, identifier, path, token):
        with self._state() as state:
            job = self._get(state, principal, recording, identifier, token, path)
            job['leases'][token]['until'] = time.time() + LEASE_SECONDS
            return self._result(identifier, job, token)

    def release(self, principal, recording, identifier, token):
        with self._state() as state:
            job = state['jobs'].get(identifier)
            if not job:
                return {'status': 'expired'}
            lease = job['leases'].get(token)
            if job['recording'] != recording or not lease or lease['principal'] != str(principal):
                raise spectra.SpectrogramError('expired', 410)
            del job['leases'][token]
            job['used'] = time.time()
            if not job['leases'] and job['status'] in ('queued', 'preparing'):
                job['status'] = 'cancelled'
            return {'status': job['status']}

    def tile(self, principal, recording, identifier, path, token, index):
        reader = secrets.token_urlsafe(16)
        with self._state() as state:
            job = self._get(state, principal, recording, identifier, token, path)
            if job['status'] != 'ready':
                raise spectra.SpectrogramError('not_ready', 409)
            if not isinstance(index, int) or index < 0 or index >= len(job['manifest']['tiles']):
                raise spectra.SpectrogramError('missing', 404)
            job['readers'][reader] = {'pid': os.getpid()}
        try:
            try:
                result = (self.root / identifier / f'{index}.png').read_bytes()
            except FileNotFoundError:
                raise spectra.SpectrogramError('expired', 410)
            # Source can be replaced between the initial guard and file read.
            with self._state() as state:
                self._get(state, principal, recording, identifier, token, path)
            return result
        finally:
            with self._state() as state:
                if identifier in state['jobs']:
                    state['jobs'][identifier]['readers'].pop(reader, None)


spectrogram_cache = SpectrogramCache(maintenance=True)
