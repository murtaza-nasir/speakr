"""Independent private manual-range preparations and atomic profile commits.

The only segment-job reuse is the stat/process identity and OS file-lock
utilities. No SegmentJobs object, directory, payload or receipt is shared.
Profile DB lock always precedes the preparation metadata lock.
"""
import contextlib
import hashlib
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
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import current_app, has_app_context

from sqlalchemy import text
from src.database import db
from src.utils.media_job import file_lock, process_identity
from src.utils.media_job import source_signature
from src.services.manual_voice_worker import source_digest, MAX_CLIP_BYTES

_memory_engine_scopes = weakref.WeakKeyDictionary()
_memory_engine_guard = threading.Lock()


WINDOWS_PRIVATE_ACL = r'''
$ErrorActionPreference = 'Stop'
$path = $env:SPEAKR_MANUAL_JOB_ROOT
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$before = Get-Acl -LiteralPath $path
if ($before.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'owner' }
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
foreach ($principal in @($sid, $system)) {
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($principal,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $path -AclObject $acl
$check = Get-Acl -LiteralPath $path
$rules = @($check.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
if (-not $check.AreAccessRulesProtected -or $rules.Count -ne 2 -or
    $check.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'acl' }
foreach ($rule in $rules) {
    if ($rule.IdentityReference.Value -notin @($sid.Value, $system.Value) -or $rule.IsInherited -or
        $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        $rule.FileSystemRights -ne [System.Security.AccessControl.FileSystemRights]::FullControl) { throw 'acl' }
}
'''


class ManualVoiceError(Exception):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


def safe_code(code):
    return {'missing': 'access', 'forbidden': 'access', 'remote': 'unsupported',
        'speech': 'insufficient_speech', 'speakers': 'multiple_speakers',
        'embedding': 'provider', 'timestamps': 'provider', 'media': 'unsupported',
        'channels': 'unsupported', 'size': 'unsupported', 'not_ready': 'changed',
        'worker': 'provider'}.get(code, code)


def validate_bounds(start_ms, end_ms, minimum):
    if not math.isfinite(minimum) or minimum <= 0:
        raise ManualVoiceError('unavailable', 503)
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or end_ms <= start_ms or end_ms - start_ms > 300000
            or end_ms - start_ms < minimum * 1000):
        raise ManualVoiceError('bounds', 400)
    return start_ms, end_ms


def minimum_speech():
    try:
        value = float(os.environ.get('VOICE_PROFILE_MIN_SPEECH_SECONDS', '15'))
    except (TypeError, ValueError):
        raise ManualVoiceError('unavailable', 503)
    if not math.isfinite(value) or value <= 0:
        raise ManualVoiceError('unavailable', 503)
    return value


def begin_profile_transaction(speaker_id, user_id):
    """Acquire an engine-level write lock without discarding pending ORM edits."""
    from src.models import Speaker
    connection = db.session.connection()
    if connection.dialect.name == 'sqlite':
        raw = connection.connection.driver_connection
        if not raw.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        # An already-open write transaction owns SQLite's database write lock.
        # Upgrade a caller's explicit deferred read transaction, if any.
        connection.execute(text('UPDATE speaker SET id=id WHERE id=:id AND user_id=:user'),
                           {'id': speaker_id, 'user': user_id})
        with db.session.no_autoflush:
            return Speaker.query.filter_by(id=speaker_id, user_id=user_id).populate_existing().first()
    if connection.dialect.name == 'postgresql':
        with db.session.no_autoflush:
            return Speaker.query.filter_by(id=speaker_id, user_id=user_id).with_for_update().populate_existing().first()
    raise ManualVoiceError('unavailable', 503)


def lock_profile(user_id, speaker_id):
    return begin_profile_transaction(speaker_id, user_id)


def purge_expired_receipts(user_id=None, limit=200):
    """Bounded retention maintenance on verified owners, never active receipts."""
    from src.models import ManualVoiceSampleReceipt as Receipt, User
    now = datetime.utcnow()
    query = db.session.query(Receipt.preparation_id).join(User, User.id == Receipt.user_id).filter(Receipt.expires_at <= now)
    if user_id is not None:
        query = query.filter(Receipt.user_id == user_id)
    ids = [row[0] for row in query.order_by(Receipt.expires_at).limit(limit).all()]
    if ids:
        query = Receipt.query.filter(Receipt.preparation_id.in_(ids), Receipt.expires_at <= now)
        if user_id is not None:
            query = query.filter(Receipt.user_id == user_id)
        query.delete(synchronize_session=False)
    return len(ids)


def space_snapshot():
    """Read the existing reference only; never probe/upload/register a space."""
    from src.models import VoiceEmbeddingSpace
    from src.services import voice_embedding_check as check, voice_profiles as vp
    from src.services.transcription import get_registry, TranscriptionCapability as Cap
    try:
        connector = get_registry().get_active_connector()
        if not all(connector.supports(cap) for cap in (Cap.SPEAKER_EMBEDDINGS, Cap.DIARIZATION, Cap.TIMESTAMPS)):
            raise ManualVoiceError('unsupported', 422)
        reference = check.load_reference() or {}
        dimension = reference.get('dimension')
        checked = datetime.fromisoformat(reference.get('checked_at', '').replace('Z', '+00:00'))
        if checked.tzinfo is not None:
            checked = checked.astimezone(timezone.utc).replace(tzinfo=None)
        space_id = vp.current_space_id()
        space = db.session.get(VoiceEmbeddingSpace, space_id) if space_id is not None else None
        fingerprint = check.backend_fingerprint()
        age = (datetime.utcnow() - checked).total_seconds()
        if (reference.get('status') != check.STATUS_OK or reference.get('clip_version') != check.CANARY_CLIP_VERSION
                or type(dimension) is not int or dimension <= 0 or not 0 <= age <= 86400
                or reference.get('backend_fingerprint') != fingerprint or space is None
                or space.dimension != dimension or space.backend_fingerprint != fingerprint):
            raise ManualVoiceError('unavailable', 503)
        canonical = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        return {'space_id': space_id, 'dimension': dimension, 'fingerprint': fingerprint,
                'clip_version': check.CANARY_CLIP_VERSION, 'reference_hash': canonical(reference),
                'connector_name': get_registry().get_active_connector_name(),
                'model': check.default_transcription_model() or getattr(connector, 'model', None),
                'config_fingerprint': canonical(connector.config)}
    except ManualVoiceError:
        raise
    except Exception:
        raise ManualVoiceError('unavailable', 503)


def check_space(expected):
    actual = space_snapshot()
    if actual != expected:
        raise ManualVoiceError('space', 409)


def default_job_scope():
    """Workers share one installation namespace; other databases never do.

    Resolve the actual configured engine, including SQLite's absolute path,
    without connecting. The secret and DB URL only enter a private hash.
    """
    installation = str(Path(__file__).resolve().parents[2])
    def database_target(url):
        # Query strings may also carry passwords or TLS credential material.
        # Keep only address/resource selectors needed to identify the database.
        selectors = {'host', 'port', 'dbname', 'database', 'service', 'unix_socket', 'socket', 'instance', 'uri', 'mode', 'cache'}
        query = {key: value for key, value in url.query.items() if key in selectors}
        return url._replace(drivername=url.get_backend_name(), username=None, password=None, query=query).render_as_string(hide_password=False)
    memory_scope = None
    if has_app_context():
        engine = db.engine
        database = engine.url.render_as_string(hide_password=False)
        if engine.dialect.name == 'sqlite' and (engine.url.database in (None, '', ':memory:')
                or engine.url.query.get('mode') == 'memory'):
            # Equal URLs identify distinct transient databases when their
            # engine instances own separate memory connections. Process-local
            # UUIDs also prevent coincident engine addresses across workers.
            with _memory_engine_guard:
                if engine not in _memory_engine_scopes:
                    _memory_engine_scopes[engine] = uuid.uuid4().hex
                memory_scope = _memory_engine_scopes[engine]
        secret = current_app.config.get('SECRET_KEY') or ''
        instance = current_app.instance_path
        target = database_target(engine.url)
    else:
        database = os.environ.get('SQLALCHEMY_DATABASE_URI', '')
        secret = os.environ.get('SECRET_KEY', '')
        instance = installation
        from sqlalchemy.engine import make_url
        target = database_target(make_url(database)) if database else ''
    family = hashlib.sha256(json.dumps([installation, instance, target], separators=(',', ':')).encode()).hexdigest()[:32]
    parts = [installation, instance, database, str(secret)]
    if memory_scope is not None:
        parts.append(memory_scope)
    identity = json.dumps(parts, separators=(',', ':')).encode()
    suffix = hashlib.sha256(identity).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / ('speakr-manual-voice-ranges-v1-' + family + '-' + suffix), family


def default_job_root():
    return default_job_scope()[0]


def unsafe_path(path):
    try:
        info = os.lstat(path)
        return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def private_lock(path, blocking=True):
    if unsafe_path(path):
        raise ManualVoiceError('unavailable', 503)
    with file_lock(path, blocking=blocking):
        if unsafe_path(path):
            raise ManualVoiceError('unavailable', 503)
        yield


class ManualJobs:
    def __init__(self, root=None, deadline=300, ttl=900, worker_command=None, family=None):
        if root is None:
            root, family = default_job_scope()
        self.root, self.family = Path(root), family
        self.deadline, self.ttl = deadline, ttl
        self.worker_command = worker_command or [sys.executable, '-m', 'src.services.manual_voice_worker']
        self._janitor_started = False
        self._janitor_guard = threading.Lock()
        self._sweep_cursor = 0
        self._job_cursors = {}

    def _marker(self):
        return {'version': 1, 'family': self.family,
            'root': hashlib.sha256(str(self.root.resolve()).encode()).hexdigest(),
            'owner': str(os.getuid()) if hasattr(os, 'getuid') else os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')}

    def _owned(self):
        try:
            if unsafe_path(self.root) or not self.root.is_dir():
                return False
            if hasattr(os, 'getuid') and self.root.stat().st_uid != os.getuid():
                return False
            marker = self.root / '.owner.json'
            if unsafe_path(marker) or marker.stat().st_size > 4096:
                return False
            return json.loads(marker.read_text(encoding='utf-8')) == self._marker()
        except (OSError, ValueError):
            return False

    def _prepare(self):
        if unsafe_path(self.root):
            raise ManualVoiceError('unavailable', 503)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if hasattr(os, 'getuid') and self.root.stat().st_uid != os.getuid():
            raise ManualVoiceError('unavailable', 503)
        os.chmod(self.root, 0o700)
        if os.name == 'nt':
            environment = os.environ.copy()
            environment['SPEAKR_MANUAL_JOB_ROOT'] = str(self.root)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', WINDOWS_PRIVATE_ACL],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ManualVoiceError('unavailable', 503)
        with private_lock(self.root / '.ownership.lock'):
            marker = self.root / '.owner.json'
            if marker.exists() or unsafe_path(marker):
                if not self._owned():
                    raise ManualVoiceError('unavailable', 503)
            else:
                temporary = self.root / ('.owner-' + uuid.uuid4().hex + '.tmp')
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    json.dump(self._marker(), output)
                os.replace(temporary, marker)

    def directory(self, job_id):
        try:
            if str(uuid.UUID(job_id)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ManualVoiceError('missing', 404)
        directory = self.root / job_id
        if unsafe_path(directory) or (directory.exists() and hasattr(os, 'getuid') and directory.stat().st_uid != os.getuid()):
            raise ManualVoiceError('missing', 404)
        return directory

    @contextlib.contextmanager
    def locked(self, job_id):
        self._prepare()
        directory = self.directory(job_id)
        with private_lock(self.root / '.metadata.lock'):
            yield directory, self._read(directory)

    def _read(self, directory):
        try:
            source = directory / 'meta.json'
            if unsafe_path(source) or source.stat().st_size > 192 * 1024:
                raise ManualVoiceError('missing', 404)
            metadata = json.loads(source.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ManualVoiceError('missing', 404)
        if metadata['state'] == 'preparing' and process_identity(metadata['parent_pid']) != metadata['parent_birth']:
            now = time.time()
            finished = min(now, metadata.get('created', now) + metadata.get('deadline', 300))
            metadata.setdefault('ttl', 900)  # Original v1 contract when an old job predates persisted TTL.
            metadata.update(state='failed', code='worker', finished=finished)
            self._write(directory, metadata)
        if metadata['state'] != 'preparing' and time.time() >= metadata.get('expires_at', metadata['finished'] + 900):
            raise ManualVoiceError('expired', 410)
        return metadata

    def _write(self, directory, metadata):
        if unsafe_path(directory) or unsafe_path(directory / 'meta.json'):
            raise ManualVoiceError('missing', 404)
        metadata.setdefault('ttl', self.ttl)
        metadata.setdefault('deadline', self.deadline)
        metadata.setdefault('created', time.time())
        if metadata['state'] != 'preparing':
            metadata.setdefault('expires_at', metadata['finished'] + metadata['ttl'])
        temporary = directory / ('.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(metadata, stream)
        os.replace(temporary, directory / 'meta.json')

    def _cleanup(self, limit=200, cursor=0):
        directories = sorted((path for path in self.root.iterdir() if not path.name.startswith('.')), key=lambda path: path.name)
        if not directories:
            return 0
        selected = (directories[cursor % len(directories):] + directories[:cursor % len(directories)])[:limit]
        for directory in selected:
            try:
                directory = self.directory(directory.name)
                self._read(directory)
            except ManualVoiceError as error:
                if error.code == 'expired' and not unsafe_path(directory) and directory.parent == self.root:
                    shutil.rmtree(directory)
            except (OSError, KeyError, TypeError, ValueError):
                continue
        return (cursor + len(selected)) % len(directories)

    def sweep_owned(self, root_limit=32, job_limit=200):
        """Only verified owned namespaces in this installation/DB family."""
        if self.family is None:
            candidates = [self.root]
        else:
            prefix = 'speakr-manual-voice-ranges-v1-' + self.family + '-'
            candidates = sorted((path for path in self.root.parent.glob(prefix + '*')
                if len(path.name) == len(prefix) + 32
                and all(char in '0123456789abcdef' for char in path.name[len(prefix):])), key=lambda path: path.name)
        if not candidates:
            return
        start = self._sweep_cursor % len(candidates)
        selected = (candidates[start:] + candidates[:start])[:root_limit]
        self._sweep_cursor = (start + len(selected)) % len(candidates)
        for root in selected:
            other = self if root == self.root else ManualJobs(root=root, family=self.family)
            if not other._owned():
                continue
            try:
                # _prepare revalidates the actual Windows owner and protected
                # ACL; no foreign marker/root is modified by discovery.
                other._prepare()
                with private_lock(root / '.metadata.lock', blocking=False):
                    key = str(root)
                    self._job_cursors[key] = other._cleanup(job_limit, self._job_cursors.get(key, 0))
            except (OSError, ManualVoiceError, subprocess.SubprocessError):
                continue

    def start_maintenance(self, app):
        self._app = app
        try:
            self.sweep_owned()
        except (OSError, ManualVoiceError, subprocess.SubprocessError):
            pass
        with self._janitor_guard:
            if not self._janitor_started:
                threading.Thread(target=self._janitor, daemon=True, name='manual-voice-expiry').start()
                self._janitor_started = True

    def _janitor(self):
        while True:
            time.sleep(min(30, max(1, self.ttl / 2)))
            try:
                self.sweep_owned()
                # DB maintenance is outside every job lock. Profile commit and
                # lifecycle always acquire their DB lock before metadata.
                if getattr(self, '_app', None) is not None:
                    with self._app.app_context():
                        purge_expired_receipts()
                        db.session.commit()
            except OSError:
                pass
            except Exception:
                pass  # Retry bounded housekeeping on its next scheduled pass.

    def start(self, app, user_id, recording_id, speaker, path, start_ms, end_ms, connector, config, params, space):
        self._app = app
        minimum = minimum_speech()
        validate_bounds(start_ms, end_ms, minimum)
        self._prepare()
        directory = None
        with private_lock(self.root / '.metadata.lock'):
            self._cleanup()
            for existing in self.root.iterdir():
                try:
                    meta = self._read(self.directory(existing.name))
                except ManualVoiceError:
                    continue
                if meta['user_id'] == user_id and meta['state'] == 'preparing':
                    raise ManualVoiceError('busy', 429)
            job_id = str(uuid.uuid4())
            directory = self.directory(job_id)
            directory.mkdir(mode=0o700)
            work = directory / 'work'
            work.mkdir(mode=0o700)
            meta = {'job_id': job_id, 'user_id': user_id, 'recording_id': recording_id,
                'speaker_id': speaker.id, 'speaker_created_at': speaker.created_at.isoformat(),
                'start_ms': start_ms, 'end_ms': end_ms, 'path': str(path), 'signature': source_signature(path),
                'space': space, 'state': 'preparing', 'parent_pid': os.getpid(),
                'parent_birth': process_identity(os.getpid()), 'created': time.time()}
            payload = {**meta, 'work': str(work), 'deadline': self.deadline, 'minimum': minimum,
                'dimension': space['dimension'], 'connector': connector, 'config': config, 'params': params}
            try:
                encoded = json.dumps(payload).encode()
                self._write(directory, meta)
                threading.Thread(target=self._supervise, args=(app, directory, encoded), daemon=True,
                                 name='manual-voice-range').start()
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise ManualVoiceError('unavailable', 503)
        self.start_maintenance(app)
        return self.public(meta)

    def _kill(self, process):
        if os.name == 'nt':
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)

    def _supervise(self, app, directory, encoded):
        process, result = None, {'code': 'unavailable'}
        try:
            process = subprocess.Popen(self.worker_command, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {}))
            deadline, first = time.monotonic() + self.deadline, True
            while True:
                if (directory / 'cancel').exists():
                    result = {'code': 'cancelled'}
                    break
                if time.monotonic() >= deadline:
                    result = {'code': 'timeout'}
                    break
                if (directory / 'work' / 'upload.wait').exists() and not (directory / 'work' / 'upload.go').exists():
                    with app.app_context():
                        check_space(json.loads(encoded)['space'])
                    (directory / 'work' / 'upload.go').touch(mode=0o600)
                try:
                    process.communicate(input=encoded if first else None, timeout=.2)
                    result_path = directory / 'work' / 'result.json'
                    if process.returncode == 0 and result_path.stat().st_size <= 96 * 1024:
                        result = json.loads(result_path.read_text(encoding='utf-8'))
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if 'vector' in result:
                # No job lock is held during DB reads; lifecycle acquires DB then job lock.
                with app.app_context():
                    check_space(json.loads(encoded)['space'])
        except ManualVoiceError as error:
            result = {'code': error.code}
        except Exception:
            result = {'code': 'provider'}
        finally:
            try:
                if process is not None:
                    self._kill(process)
            finally:
                with private_lock(self.root / '.metadata.lock'):
                    try:
                        meta = self._read(directory)
                    except ManualVoiceError:
                        return
                    if meta['state'] != 'preparing' or (directory / 'cancel').exists():
                        result = {'code': 'cancelled'}
                    if 'vector' in result:
                        meta.update(state='ready', result=result, finished=time.time())
                    elif meta['state'] == 'preparing':
                        code = result.get('code', 'provider')
                        allowed = {'unavailable', 'provider', 'missing', 'media', 'bounds', 'channels', 'size',
                                   'changed', 'space', 'speakers', 'embedding', 'timestamps', 'speech',
                                   'timeout', 'cancelled', 'unsupported'}
                        meta.update(state='cancelled' if code == 'cancelled' else 'failed',
                                    code=code if code in allowed else 'provider', finished=time.time())
                    self._write(directory, meta)
                    (directory / 'work' / 'result.json').unlink(missing_ok=True)
                    if meta['state'] != 'ready':
                        shutil.rmtree(directory / 'work', ignore_errors=True)

    def public(self, meta):
        result = {'job_id': meta['job_id'], 'state': meta['state'],
                  'range': {'start_ms': meta['start_ms'], 'end_ms': meta['end_ms']}}
        if meta.get('code'):
            result['code'] = safe_code(meta['code'])
        if meta.get('finished'):
            result['expires_at'] = datetime.utcfromtimestamp(meta.get('expires_at', meta['finished'] + 900)).isoformat() + 'Z'
        if meta['state'] == 'ready':
            result.update(speech_ms=meta['result']['speech_ms'], space_id=meta['space']['space_id'])
        return result

    def _cancel(self, directory, meta):
        running = meta['state'] == 'preparing'
        (directory / 'cancel').touch(mode=0o600)
        # Preserve terminal TTL, including consumed receipts.
        meta.update(state='cancelled', code='cancelled')
        meta.setdefault('finished', time.time())
        meta.pop('result', None)
        self._write(directory, meta)
        if not running:
            # Active worker owns work cleanup; removal happens on its completion.
            with contextlib.suppress(OSError):
                (directory / 'work' / 'clip.wav').unlink(missing_ok=True)

    def invalidate(self, user_id=None, speaker_id=None, recording_id=None):
        self._prepare()
        with private_lock(self.root / '.metadata.lock'):
            for directory in self.root.iterdir():
                try:
                    directory = self.directory(directory.name)
                    meta = self._read(directory)
                except ManualVoiceError:
                    continue
                if ((user_id is None or meta['user_id'] == user_id)
                        and (speaker_id is None or meta['speaker_id'] == speaker_id)
                        and (recording_id is None or meta['recording_id'] == recording_id)
                        and meta['state'] in ('preparing', 'ready')):
                    self._cancel(directory, meta)


class InstallationJobs:
    """Select an immutable manager per app/DB, including same-process apps.

    A worker's bound ManualJobs instance never changes its root while another
    request enters a different Flask application context.
    """
    def __init__(self):
        self._instances = {}
        self._guard = threading.Lock()

    def manager(self):
        root, family = default_job_scope()
        key = str(root)
        with self._guard:
            if key not in self._instances:
                self._instances[key] = ManualJobs(root=root, family=family)
            return self._instances[key]

    def __getattr__(self, name):
        return getattr(self.manager(), name)


manual_jobs = InstallationJobs()


def invalidate_profile(user_id, speaker_id):
    manual_jobs.invalidate(user_id=user_id, speaker_id=speaker_id)


def invalidate_recording(recording_id):
    # Gather identities without holding a job lock during DB access. Source
    # deletion then serializes against commits for every affected profile.
    manual_jobs._prepare()
    identities = set()
    with private_lock(manual_jobs.root / '.metadata.lock'):
        for directory in manual_jobs.root.iterdir():
            try:
                meta = manual_jobs._read(manual_jobs.directory(directory.name))
            except ManualVoiceError:
                continue
            if meta['recording_id'] == recording_id:
                identities.add((meta['user_id'], meta['speaker_id']))
    for user_id, speaker_id in sorted(identities):
        begin_profile_transaction(speaker_id, user_id)
    if db.session.connection().dialect.name == 'postgresql':
        from src.models import Recording
        Recording.query.filter_by(id=recording_id).with_for_update().first()
    elif not identities:
        # Lock SQLite even when no preparation existed at the first scan.
        begin_profile_transaction(-1, -1)
    for user_id, speaker_id in sorted(identities):
        manual_jobs.invalidate(user_id=user_id, speaker_id=speaker_id, recording_id=recording_id)
    # Re-scan while the source row/write lock is held. A preparation created
    # between discovery and lock acquisition must also receive a tombstone.
    manual_jobs.invalidate(recording_id=recording_id)


@contextlib.contextmanager
def commit_job_lock(job_id):
    directory = manual_jobs.directory(job_id)  # Invalid/noncanonical identities retain 404.
    try:
        with manual_jobs.locked(job_id) as locked:
            yield locked
    except ManualVoiceError as error:
        if error.code == 'missing' and not directory.exists():
            # Expired private metadata is physically removed. A canonical UUID
            # with no remaining job is unavailable for commit, never recreated.
            raise ManualVoiceError('expired', 410)
        raise


def commit_sample(user_id, speaker_id, job_id, authorize):
    """authorize(meta) rechecks source/edit/profile on every attempt, including receipts."""
    from src.models import ManualVoiceSample, ManualVoiceSampleReceipt
    from src.services import voice_profiles as vp
    speaker = begin_profile_transaction(speaker_id, user_id)
    if speaker is None:
        raise ManualVoiceError('missing', 404)
    with commit_job_lock(job_id) as (_, snapshot):
        if snapshot['user_id'] != user_id or snapshot['speaker_id'] != speaker_id:
            raise ManualVoiceError('missing', 404)
    path = authorize(snapshot)  # Source DB lock also precedes job metadata lock.
    with commit_job_lock(job_id) as (directory, meta):
        if (meta['user_id'] != user_id or meta['speaker_id'] != speaker_id
                or meta['speaker_created_at'] != speaker.created_at.isoformat()):
            raise ManualVoiceError('missing', 404)
        receipt = db.session.get(ManualVoiceSampleReceipt, job_id)
        if receipt is not None:
            if (receipt.user_id != user_id or receipt.initial_speaker_id != speaker_id
                    or receipt.initial_speaker_created_at != speaker.created_at
                    or datetime.utcnow() >= receipt.expires_at):
                raise ManualVoiceError('consumed', 410)
            sample = db.session.get(ManualVoiceSample, receipt.sample_id)
            if sample is None or sample.user_id != user_id or sample.speaker_id != speaker_id:
                raise ManualVoiceError('consumed', 410)
            check_space(meta['space'])
            if source_digest(path) != (sample.source_audio_sha256, meta['signature'][0]):
                raise ManualVoiceError('changed', 409)
            db.session.commit()
            return sample, False
        if meta['state'] != 'ready':
            raise ManualVoiceError(meta.get('code', 'not_ready'), 410 if meta['state'] == 'cancelled' else 409)
        check_space(meta['space'])
        result = meta['result']
        if source_digest(path) != (result['source_audio_sha256'], result['source_size']):
            manual_jobs._cancel(directory, meta)
            raise ManualVoiceError('changed', 409)
        if ManualVoiceSample.query.filter_by(user_id=user_id, speaker_id=speaker_id,
                                             space_id=meta['space']['space_id']).count() >= 20:
            raise ManualVoiceError('limit', 409)
        vector = vp.normalize(result['vector'])
        if vector is None or len(vector) != meta['space']['dimension']:
            raise ManualVoiceError('embedding', 422)
        ManualVoiceSampleReceipt.query.filter(ManualVoiceSampleReceipt.user_id == user_id,
            ManualVoiceSampleReceipt.expires_at <= datetime.utcnow()).delete(synchronize_session=False)
        vp._materialize_legacy(speaker)
        sample = ManualVoiceSample(id=str(uuid.uuid4()), user_id=user_id, speaker_id=speaker_id,
            recording_id=meta['recording_id'], start_ms=meta['start_ms'], end_ms=meta['end_ms'],
            source_audio_sha256=result['source_audio_sha256'], preparation_id=job_id,
            space_id=meta['space']['space_id'], embedding=vp.to_bytes(vector), dimension=len(vector),
            speech_ms=result['speech_ms'])
        db.session.add(sample)
        db.session.add(ManualVoiceSampleReceipt(preparation_id=job_id, user_id=user_id,
            initial_speaker_id=speaker_id, initial_speaker_created_at=speaker.created_at,
            sample_id=sample.id, consumed_at=datetime.utcnow(),
            expires_at=datetime.utcfromtimestamp(meta.get('expires_at', meta['finished'] + 900))))
        db.session.flush()
        vp.refresh_speaker_summary(speaker)
        db.session.commit()
        vp._calibration_cache.clear()
        return sample, True
