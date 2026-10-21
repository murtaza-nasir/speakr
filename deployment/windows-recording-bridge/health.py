"""Bounded loopback probes and a heartbeat independent from the SQLite writer."""
import ctypes
import json
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request
import uuid

from common import atomic_json, maintenance_paused


def probe_http(url, timeout=3):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Health probes must use loopback HTTP')
        from speakr_api import LocalRedirect
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalRedirect(url))
        with opener.open(url, timeout=timeout) as r:
            if urllib.parse.urlsplit(r.url)[:2] != parsed[:2]:
                raise ValueError('External redirect refused')
            data = r.read(1_000_001)
            if len(data) > 1_000_000:
                raise ValueError('Health reply too large')
            return {'ready': True, 'data': json.loads(data) if 'json' in r.headers.get('Content-Type', '') else None}
    except Exception as exc:
        return {'ready': False, 'error': type(exc).__name__}


def probe_llm(cfg):
    result = probe_http(cfg['llm_url'].rstrip('/') + '/api/tags')
    if result['ready']:
        names = [m.get('name') for m in (result['data'] or {}).get('models', [])]
        result['ready'] = cfg['llm_model'] in names
        if not result['ready']:
            result['error'] = 'ModelMissing'
    result.pop('data', None)
    return result


def process_start_marker():
    if os.name != 'nt':
        return str(uuid.uuid4())
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(v) for v in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)


class Heartbeat:
    def __init__(self, root):
        self.root = Path(root)
        self.stop_event = threading.Event()
        self.marker = process_start_marker()
        self.operation = 'idle'
        self.deadline = None
        self.cycle_completed_at = time.time()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def emit(self):
        atomic_json(self.root / 'heartbeat.json', {
            'pid': os.getpid(), 'start_marker': self.marker, 'updated': time.time(),
            'operation': self.operation, 'deadline': self.deadline,
            'cycle_completed_at': self.cycle_completed_at,
            'quiesced': maintenance_paused(self.root) and self.operation == 'idle'})

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.emit()
            except OSError:
                pass  # A missing heartbeat makes supervision conservative, not a crash loop.
            self.stop_event.wait(5)

    def start(self):
        self.emit()
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=6)

    def begin(self, operation, timeout):
        self.operation, self.deadline = operation, time.time() + timeout

    def finish(self):
        self.operation, self.deadline = 'idle', None
        self.cycle_completed_at = time.time()
