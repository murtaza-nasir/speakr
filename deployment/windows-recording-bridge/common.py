"""Local-only IO shared by the watcher and the Explorer entry point."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent

def migration_guard(root=ROOT):
    """Run before configuration reads or any runtime constructor writes."""
    path = Path(root) / 'adoption-state.json'
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8-sig'))
        if state.get('phase') != 'installed':
            raise RuntimeError('Interrupted adoption; use adopt Rollback before starting runtime')
        import sqlite3
        db=sqlite3.connect((Path(root)/'state.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
        try:
            actual=sorted(db.execute('SELECT job_id,recording_id,migration_id FROM recovery_holds').fetchall())
            expected=sorted(tuple(x) for x in state['expected_holds'])
            if actual!=expected:
                raise RuntimeError('Historical recovery holds drift; runtime remains stopped')
        finally: db.close()

def recovery_held(db, job):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_holds' AND type='table'").fetchone():
        return False
    return bool(db.execute('SELECT 1 FROM recovery_holds WHERE job_id=? OR (recording_id IS NOT NULL AND recording_id=?)',
                           (job['id'], job['recording_id'])).fetchone())
def model_key(name):
    return name if ':' in name.rsplit('/',1)[-1] else name+':latest'
MEDIA = {'.m4a', '.mp3', '.wav', '.flac', '.ogg', '.opus', '.aac', '.wma',
         '.mp4', '.mkv', '.webm', '.mov', '.avi', '.mpeg', '.mpg', '.m4v'}
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def maintenance_paused(root=ROOT):
    path = Path(root) / 'maintenance.json'
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding='utf-8-sig')).get('paused', True))
    except (OSError, ValueError):
        return True  # Malformed maintenance state never authorizes side effects.


def config(path=None):
    migration_guard(Path(path).parent if path else ROOT)
    return json.loads(Path(path or ROOT / 'config.json').read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def signature(path):
    s = Path(path).stat()
    return f'{s.st_size}:{s.st_mtime_ns}'


def is_reparse(path):
    s = Path(path).lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)


def media_paths(path):
    """Yield (path, skip reason); never traverse a junction or symlink."""
    path = Path(os.path.abspath(path))
    try:
        if is_reparse(path):
            yield path, 'reparse_point'
        elif path.is_dir():
            with os.scandir(path) as entries:
                for e in entries:
                    if e.name.startswith('.'):
                        continue
                    yield from media_paths(Path(e.path))
        elif path.is_file():
            yield path, None if path.suffix.lower() in MEDIA else 'unsupported_type'
        else:
            yield path, 'not_a_file'
    except OSError as exc:
        yield path, 'unavailable:' + type(exc).__name__


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(args, timeout=60):
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, check=True)

def powershell_env():
    # Windows PowerShell must resolve its own modules when launched via Python
    # from pwsh 7; inherited Core modules cannot load in Desktop PowerShell.
    return {k:v for k,v in os.environ.items() if k.lower()!='psmodulepath'}


def enqueue(paths, root=ROOT):
    root = Path(root)
    paths = list(dict.fromkeys(os.path.abspath(os.fspath(p)) for p in paths))
    if not paths:
        raise ValueError('Не выбраны файлы или папки')
    request_id = uuid.uuid4().hex
    request_path = root / 'requests' / (request_id + '.json')
    atomic_json(request_path, {'id': request_id, 'origin': 'manual', 'paths': paths,
                               'created': time.time()})
    return request_id, root / 'reports' / (request_id + '.html')


def enqueue_control(action, job_id, root=ROOT):
    if action not in ('retry', 're-export') or not re.fullmatch('[0-9a-f]{32}', job_id):
        raise ValueError('Expected action and a 32-character job id')
    control_id = uuid.uuid4().hex
    atomic_json(Path(root) / 'controls' / (control_id + '.json'),
                {'id': control_id, 'action': action, 'job_id': job_id, 'created': time.time()})
    return control_id


def safe_name(value, length=65):
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .') or 'Запись')[:length]


def start_worker(cfg, root=ROOT):
    # The worker owns a kernel-backed lock. Concurrent launches are harmless.
    return subprocess.Popen([cfg['pythonw'], str(Path(root) / 'bridge.py'), '--loop'],
                            cwd=root, creationflags=CREATE_NO_WINDOW,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
