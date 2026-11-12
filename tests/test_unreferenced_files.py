"""Finding and removing media files that no row refers to.

scripts/clean_unreferenced_files.py lists files in the uploads folder (and
objects in the S3 bucket) without a recording, upload part or session row,
and deletes or moves them only when asked. These tests run the scan and the
command against a temporary uploads folder with a real StorageService, so
locators are resolved exactly as the app resolves them.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, RecordingSession, User
from src.models.upload_join import UploadJoinPart
from src.services import unreferenced_files as uf
from src.services.storage.factory import StorageSettings
from src.services.storage.service import StorageService

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT_DIR, 'scripts', 'clean_unreferenced_files.py')

_spec = importlib.util.spec_from_file_location('clean_unreferenced_files', SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

LATER = timedelta(days=3)  # "now" for scans that must see fresh files as old


def _settings(root, **kw):
    base = dict(backend='local', local_root=root, key_prefix='recordings',
                staging_dir=os.path.join(root, '_staging'),
                presign_ttl_seconds=60, presign_public_ttl_seconds=60)
    base.update(kw)
    return StorageSettings(**base)


def _touch(path, data=b'x' * 10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return os.path.realpath(path)


def _user():
    suffix = uuid.uuid4().hex[:8]
    u = User(username=f"unref_{suffix}", email=f"unref_{suffix}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _rec(user, audio_path, **kw):
    rec = Recording(user_id=user.id, title='t', status='COMPLETED', audio_path=audio_path,
                    original_filename='x.webm', **kw)
    db.session.add(rec)
    db.session.commit()
    return rec.id


def _name(ext='.webm'):
    return f"{uuid.uuid4().hex}{ext}"


@pytest.fixture
def env():
    """A temporary uploads folder with a real local StorageService."""
    with app.app_context(), tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, 'uploads')
        os.makedirs(root)
        storage = StorageService(_settings(root))
        yield root, storage, tmp
        db.session.rollback()


@pytest.fixture
def layout(env):
    """Referenced files in every form, strays, and protected files."""
    root, storage, tmp = env
    user = _user()
    month = os.path.join(root, 'recordings', '2026', '11')
    f = {}
    f['locator'] = _touch(os.path.join(month, '7', _name()))
    f['legacy_abs'] = _touch(os.path.join(root, _name('.mp3')))
    f['legacy_rel'] = _touch(os.path.join(root, _name('.m4a')))
    f['legacy_rel_prefix'] = _touch(os.path.join(month, _name('.wav')))
    f['join_part'] = _touch(os.path.join(month, 'join', _name()))
    f['session_name'] = _touch(os.path.join(root, _name('.ogg')))
    f['stray'] = _touch(os.path.join(month, '9', _name()))
    f['legacy_stray'] = _touch(os.path.join(root, _name('.mp3')))
    f['session_chunk'] = _touch(os.path.join(root, '_sessions', 'abc', 'chunk-000001.bin'))
    f['staging'] = _touch(os.path.join(root, '_staging', _name()))
    f['debug'] = _touch(os.path.join(root, 'debug', _name()))
    f['prefix_tmp'] = _touch(os.path.join(month, '_tmp', _name()))
    f['dotfile'] = _touch(os.path.join(month, '.partial'))

    rel = os.path.relpath(f['locator'], root).replace(os.sep, '/')
    _rec(user, f'local://{rel}')
    _rec(user, f['legacy_abs'])
    _rec(user, os.path.basename(f['legacy_rel']))
    _rec(user, os.path.relpath(f['legacy_rel_prefix'], root).replace(os.sep, '/'))
    db.session.add(UploadJoinPart(
        user_id=user.id, group_id=uuid.uuid4().hex, part_index=0, part_count=2,
        audio_path='local://' + os.path.relpath(f['join_part'], root).replace(os.sep, '/')))
    db.session.add(RecordingSession(user_id=user.id, kind='sliced_upload',
                                    upload_filename=os.path.basename(f['session_name'])))
    db.session.commit()
    return root, storage, tmp, user, f


def _snapshot(root):
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            out[p] = os.path.getsize(p)
    return out


def _run(argv, storage, answer=None):
    buf = io.StringIO()
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer if answer is not None else ''
    with redirect_stdout(buf):
        code = cli.main(argv, storage=storage, input_fn=ask)
    return code, buf.getvalue(), asked


# --- references ----------------------------------------------------------

def test_every_reference_form_keeps_its_file(layout):
    root, storage, _, _, f = layout
    result = uf.scan(storage, now=datetime.utcnow() + LATER)
    found = {c.path for c in result.candidates}
    assert found == {f['stray'], f['legacy_stray']}
    assert result.referenced >= 6


def test_protected_and_hidden_files_are_never_listed(layout):
    root, storage, _, _, f = layout
    result = uf.scan(storage, now=datetime.utcnow() + LATER)
    found = {c.path for c in result.candidates}
    for key in ('session_chunk', 'staging', 'debug', 'prefix_tmp', 'dotfile'):
        assert f[key] not in found
    assert set(result.not_scanned) >= {'_sessions', '_staging', 'debug'}


def test_dry_run_changes_nothing(layout):
    root, storage, _, _, f = layout
    before = _snapshot(root)
    code, out, asked = _run(['--min-age-hours', '0', '--list'], storage)
    assert code == 0
    assert _snapshot(root) == before
    assert asked == []
    assert 'Unreferenced: 2 file(s), 20 B' in out
    assert f['stray'] in out and f['legacy_stray'] in out
    assert 'Dry run: nothing was changed.' in out


# --- deleting ------------------------------------------------------------

def test_delete_needs_the_flag_and_removes_only_strays(layout):
    root, storage, _, _, f = layout
    code, out, _ = _run(['--min-age-hours', '0', '--delete', '--yes'], storage)
    assert code == 0, out
    assert 'Deleted: 2 file(s), 20 B' in out
    assert not os.path.exists(f['stray']) and not os.path.exists(f['legacy_stray'])
    for key, path in f.items():
        if key not in ('stray', 'legacy_stray'):
            assert os.path.exists(path), key


def test_delete_asks_and_a_no_changes_nothing(layout):
    root, storage, _, _, f = layout
    before = _snapshot(root)
    code, out, asked = _run(['--min-age-hours', '0', '--delete'], storage, answer='n')
    assert code == 1
    assert len(asked) == 1 and 'Delete 2 file(s)' in asked[0]
    assert 'Cancelled' in out
    assert _snapshot(root) == before

    code, out, _ = _run(['--min-age-hours', '0', '--delete'], storage, answer='yes')
    assert code == 0
    assert not os.path.exists(f['stray'])


def test_min_age_is_respected(env):
    root, storage, _ = env
    stray = _touch(os.path.join(root, 'recordings', '2026', '11', _name()))
    now = datetime.utcnow()
    soon = uf.scan(storage, now=now + timedelta(hours=1))
    assert stray not in {c.path for c in soon.candidates}
    assert soon.recent == 1 and soon.recent_bytes == 10
    assert stray in {c.path for c in uf.scan(storage, now=now + timedelta(hours=49)).candidates}
    assert stray in {c.path for c in uf.scan(storage, now=now + timedelta(hours=2),
                                             min_age=timedelta(hours=1)).candidates}

    # Fresh file, default 48 h: the command leaves it alone.
    code, out, _ = _run(['--delete', '--yes'], storage)
    assert code == 0 and 'Nothing to do.' in out
    assert os.path.exists(stray)


def test_old_mtime_does_not_make_a_fresh_copy_old(env):
    """A file copied with copy2 keeps its old mtime; its ctime is still new."""
    root, storage, _ = env
    stray = _touch(os.path.join(root, 'recordings', _name()))
    old = (datetime.utcnow() - timedelta(days=400)).timestamp()
    os.utime(stray, (old, old))
    result = uf.scan(storage)
    assert result.candidates == [] and result.recent == 1


def test_file_referenced_after_the_scan_is_kept(env):
    root, storage, _ = env
    stray = _touch(os.path.join(root, 'recordings', _name()))
    later = datetime.utcnow() + LATER
    result = uf.scan(storage, now=later)
    assert [c.path for c in result.candidates] == [stray]
    _rec(_user(), 'local://' + os.path.relpath(stray, root).replace(os.sep, '/'))
    outcome = uf.apply(result.candidates, storage, now=later)
    assert outcome.removed == 0 and 'now referenced' in outcome.skipped[0]
    assert os.path.exists(stray)


# --- paths outside the uploads folder ------------------------------------

def test_symlink_to_a_file_outside_is_skipped(env):
    root, storage, tmp = env
    outside = _touch(os.path.join(tmp, 'outside', 'precious.mp3'))
    link_dir = os.path.join(root, 'recordings', '2026')
    os.makedirs(link_dir)
    os.symlink(outside, os.path.join(link_dir, 'link.mp3'))
    os.symlink(os.path.dirname(outside), os.path.join(link_dir, 'linked_dir'))
    os.symlink(outside, os.path.join(root, 'top_link.mp3'))

    result = uf.scan(storage, now=datetime.utcnow() + LATER)
    assert result.candidates == []
    assert len(result.skipped_links) == 3
    code, out, _ = _run(['--min-age-hours', '0', '--delete', '--yes'], storage)
    assert os.path.exists(outside)


def test_candidate_outside_the_root_is_refused(env):
    root, storage, tmp = env
    outside = _touch(os.path.join(tmp, 'outside', 'precious.mp3'))
    forged = uf.Candidate('local', outside, 10, datetime(2020, 1, 1))
    outcome = uf.apply([forged], storage, now=datetime.utcnow() + LATER)
    assert outcome.removed == 0 and 'outside the uploads folder' in outcome.skipped[0]
    assert os.path.exists(outside)


def test_key_prefix_escaping_the_root_is_refused(env):
    root, _, tmp = env
    _touch(os.path.join(tmp, 'elsewhere', 'precious.mp3'))
    storage = StorageService(_settings(root, key_prefix='../elsewhere'))
    with pytest.raises(ValueError):
        uf.scan(storage)
    code, out, _ = _run(['--min-age-hours', '0', '--delete', '--yes'], storage)
    assert code == 2
    assert os.path.exists(os.path.join(tmp, 'elsewhere', 'precious.mp3'))

    storage = StorageService(_settings(root, key_prefix='_sessions'))
    with pytest.raises(ValueError):
        uf.scan(storage)


# --- moving --------------------------------------------------------------

def test_move_to_keeps_relative_paths(layout):
    root, storage, tmp, _, f = layout
    quarantine = os.path.join(tmp, 'quarantine')
    code, out, _ = _run(['--min-age-hours', '0', '--move-to', quarantine, '--yes'], storage)
    assert code == 0, out
    assert 'Moved: 2 file(s), 20 B' in out
    for key in ('stray', 'legacy_stray'):
        assert not os.path.exists(f[key])
        assert os.path.isfile(os.path.join(quarantine, os.path.relpath(f[key], root)))
    assert os.path.exists(f['locator'])


def test_move_to_inside_the_scanned_area_is_refused(layout):
    root, storage, _, _, f = layout
    before = _snapshot(root)
    code, _, _ = _run(['--min-age-hours', '0', '--move-to', os.path.join(root, 'recordings', 'q'),
                       '--yes'], storage)
    assert code == 2
    code, _, _ = _run(['--min-age-hours', '0', '--move-to', root, '--yes'], storage)
    assert code == 2
    assert _snapshot(root) == before


def test_delete_and_move_to_are_exclusive(env):
    _, storage, tmp = env
    with pytest.raises(SystemExit):
        _run(['--delete', '--move-to', tmp], storage)


# --- rows without a file --------------------------------------------------

def test_rows_whose_file_is_missing_are_reported_and_not_changed(env):
    root, storage, _ = env
    user = _user()
    gone = f"local://recordings/2026/11/{_name()}"
    rid = _rec(user, gone)
    purged = _rec(user, f"local://recordings/2026/11/{_name()}", audio_deleted_at=datetime.utcnow())
    code, out, _ = _run(['--min-age-hours', '0', '--list'], storage)
    assert code == 0
    assert f"recording {rid}: {gone}" in out
    assert f"recording {purged}:" not in out  # audio deleted on purpose
    db.session.expire_all()
    assert db.session.get(Recording, rid).audio_path == gone


def test_file_named_like_a_missing_row_is_kept(env):
    """A legacy path from before UPLOAD_FOLDER moved: same name, other folder."""
    root, storage, _ = env
    name = _name('.mp3')
    _rec(_user(), f"/old/data/uploads/{name}")
    moved = _touch(os.path.join(root, name))
    result = uf.scan(storage, now=datetime.utcnow() + LATER)
    assert result.candidates == []
    assert result.kept_name_matches == [moved]


# --- S3 ------------------------------------------------------------------

class _FakeS3Client:
    def __init__(self, objects):
        self.objects = objects  # key -> (size, last_modified)
        self.deleted = []

    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        client = self

        class _P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in client.objects if k.startswith(Prefix))
                for i in range(0, len(keys), 2):  # two per page
                    yield {'Contents': [{'Key': k, 'Size': client.objects[k][0],
                                         'LastModified': client.objects[k][1]} for k in keys[i:i + 2]]}
        return _P()

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))


def _s3_storage(root, objects):
    storage = StorageService(_settings(root, backend='s3', s3_bucket_name='bkt', s3_region='us-east-1'))
    client = _FakeS3Client(objects)
    storage.s3._client = client
    return storage, client


def test_s3_objects_are_listed_and_deleted_like_files(env):
    root, _, tmp = env
    old = datetime.now(timezone.utc) - timedelta(days=10)
    new = datetime.now(timezone.utc)
    kept, stray, fresh = (f"recordings/2026/11/{_name()}" for _ in range(3))
    storage, client = _s3_storage(root, {kept: (5, old), stray: (7, old), fresh: (9, new),
                                         'recordings/_tmp/x.webm': (1, old),
                                         'other/stray.webm': (1, old)})
    _rec(_user(), f"s3://bkt/{kept}")

    code, out, _ = _run(['--list'], storage)
    assert code == 0
    assert 'Unreferenced: 1 file(s), 7 B' in out and f"s3://bkt/{stray}" in out
    assert 'Scanned: s3://bkt/recordings/' in out
    assert client.deleted == []

    code, out, _ = _run(['--move-to', os.path.join(tmp, 'q'), '--yes'], storage)
    assert code == 2 and client.deleted == []

    code, out, _ = _run(['--delete', '--yes'], storage)
    assert code == 0, out
    assert client.deleted == [('bkt', stray)]


# --- importing the app starts nothing --------------------------------------

def test_skip_flag_bypasses_every_election(monkeypatch):
    from src.config import startup
    calls = []
    monkeypatch.setenv('SPEAKR_SKIP_BACKGROUND_TASKS', 'true')
    monkeypatch.setattr(startup, 'start_single_instance', lambda *a, **k: calls.append(a[1]))
    startup.run_startup_tasks(app)
    assert calls == []
    monkeypatch.delenv('SPEAKR_SKIP_BACKGROUND_TASKS')
    startup.run_startup_tasks(app)
    assert startup.JOB_QUEUE_OWNER_LOCK in calls


def test_command_import_takes_no_lock_and_starts_no_thread():
    """A real process, as in the container: import the app the way the script does."""
    code = (
        "import importlib.util, json, sys, threading;"
        f"spec = importlib.util.spec_from_file_location('c', {SCRIPT!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "m._import_app();"
        "from src.utils import database as d;"
        "import os;"
        "print(json.dumps({'locks': list(d._held_singleton_locks),"
        " 'threads': [t.name for t in threading.enumerate() if t is not threading.main_thread()],"
        " 'flag_left': 'SPEAKR_SKIP_BACKGROUND_TASKS' in os.environ}))"
    )
    env = dict(os.environ)
    env.pop('SPEAKR_SKIP_BACKGROUND_TASKS', None)
    # Other tests change these in os.environ; the app refuses to start without them.
    env.update(TRANSCRIPTION_API_KEY='test-key', TRANSCRIPTION_BASE_URL='https://api.openai.com/v1',
               USE_ASR_ENDPOINT='false', SECRET_KEY='pytest-secret-key')
    with tempfile.TemporaryDirectory() as tmp:
        env.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{os.path.join(tmp, 'cli.db')}",
                   UPLOAD_FOLDER=os.path.join(tmp, 'uploads'))
        proc = subprocess.run([sys.executable, '-c', code], cwd=ROOT_DIR, env=env,
                              capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report == {'locks': [], 'threads': [], 'flag_left': False}
