#!/usr/bin/env python3
"""Coverage tests for src/file_monitor.py.

These tests target the parts of the file monitor NOT already exercised by
tests/test_auto_process_tags.py:

  * Directory-scan filtering: hidden files, .processing files, unsupported
    extensions, instability skip, file disappearing between iterdir and probe.
  * The atomic-lock rename / process / unlock-on-error flow.
  * The full _process_file import+create+enqueue path (all heavy externals
    mocked: storage, ffprobe, convert_if_needed, job_queue).
  * Mode dispatch: admin_only (incl. no-admin-user skip), user_directories
    (valid/invalid user dir name), single_user (configured/unconfigured).
  * _extract_user_id_from_dirname patterns.
  * Module-level helpers: start/stop/status, start with feature disabled,
    invalid mode, _ensure_tag_folders_on_startup error path.

CRITICAL: we never start the real watcher thread or the polling loop. We call
the scan/process functions directly. _is_file_stable is stubbed so no real
time.sleep happens.

Shared-DB safety: every user/recording is uuid-suffixed and removed in
teardown; assertions are scoped to the rows this file creates.
"""

import os
import sys
import shutil
import tempfile
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Tag, RecordingTag
from src.file_monitor import (
    FileMonitor,
    start_file_monitor,
    stop_file_monitor,
    get_file_monitor_status,
    _ensure_tag_folders_on_startup,
)
import src.file_monitor as fm_module


def _uniq(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_monitor(temp_dir, user_id, mode='admin_only'):
    """Build a monitor with caches pre-populated and stability stubbed out."""
    monitor = FileMonitor(temp_dir, check_interval=30, mode=mode)
    monitor._admin_user_id = user_id
    monitor._valid_users = {user_id: f'u{user_id}'}
    monitor._username_to_id = {f'u{user_id}': user_id}
    # Never actually sleep / stat-poll for stability in tests.
    monitor._is_file_stable = lambda *a, **kw: True
    return monitor


def _write_file(path, data=b'\x00' * 64):
    with open(path, 'wb') as f:
        f.write(data)


# ---------------------------------------------------------------------------
# Directory scan filtering
# ---------------------------------------------------------------------------

class TestScanDirectoryFiltering(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.temp_dir = tempfile.mkdtemp()
        self.uid = 4242

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.ctx.pop()

    def _scan_collecting(self, monitor):
        processed = []
        monitor._process_file = lambda path, uid, tag_id=None: processed.append(
            {'path': str(path), 'uid': uid, 'tag_id': tag_id}
        )
        monitor._scan_directory_for_user(monitor.base_watch_directory, self.uid)
        return processed

    def test_nonexistent_directory_is_noop(self):
        monitor = _make_monitor(self.temp_dir, self.uid)
        missing = monitor.base_watch_directory / 'does-not-exist'
        monitor._process_file = MagicMock()
        # Should simply return, not raise, and process nothing.
        ret = monitor._scan_directory_for_user(missing, self.uid)
        self.assertIsNone(ret)
        monitor._process_file.assert_not_called()

    def test_hidden_and_processing_and_unsupported_files_skipped(self):
        _write_file(os.path.join(self.temp_dir, '.hidden.mp3'))
        _write_file(os.path.join(self.temp_dir, 'inprogress.mp3.processing'))
        _write_file(os.path.join(self.temp_dir, 'notes.txt'))
        _write_file(os.path.join(self.temp_dir, 'archive.zip'))
        # A subdirectory should be skipped (not a file).
        os.makedirs(os.path.join(self.temp_dir, 'subdir'))

        monitor = _make_monitor(self.temp_dir, self.uid)
        processed = self._scan_collecting(monitor)
        self.assertEqual(processed, [])

    def test_supported_file_is_locked_and_processed(self):
        _write_file(os.path.join(self.temp_dir, 'good.mp3'))
        monitor = _make_monitor(self.temp_dir, self.uid)
        processed = self._scan_collecting(monitor)

        self.assertEqual(len(processed), 1)
        # File was renamed to .processing before handing to _process_file.
        self.assertTrue(processed[0]['path'].endswith('.mp3.processing'))
        self.assertEqual(processed[0]['uid'], self.uid)
        self.assertIsNone(processed[0]['tag_id'])

    def test_unstable_file_skipped(self):
        _write_file(os.path.join(self.temp_dir, 'growing.mp3'))
        monitor = _make_monitor(self.temp_dir, self.uid)
        monitor._is_file_stable = lambda *a, **kw: False
        processed = self._scan_collecting(monitor)
        self.assertEqual(processed, [])
        # File should NOT have been renamed (still has original name).
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir, 'growing.mp3')))

    def test_file_vanishes_during_stability_check(self):
        _write_file(os.path.join(self.temp_dir, 'racey.mp3'))
        monitor = _make_monitor(self.temp_dir, self.uid)

        def boom(*a, **kw):
            raise FileNotFoundError("gone")
        monitor._is_file_stable = boom
        processed = self._scan_collecting(monitor)
        self.assertEqual(processed, [])

    def test_lock_rename_failure_is_caught(self):
        _write_file(os.path.join(self.temp_dir, 'locked.mp3'))
        monitor = _make_monitor(self.temp_dir, self.uid)
        called = {'n': 0}
        monitor._process_file = lambda *a, **kw: called.__setitem__('n', called['n'] + 1)

        # The atomic lock is Path.rename; first call (lock) raises FileNotFound.
        real_rename = Path.rename

        def fake_rename(self_path, target):
            if str(self_path).endswith('.mp3'):
                raise FileNotFoundError("claimed by other worker")
            return real_rename(self_path, target)

        with patch.object(Path, 'rename', fake_rename):
            monitor._scan_directory_for_user(monitor.base_watch_directory, self.uid)
        self.assertEqual(called['n'], 0)

    def test_lock_rename_generic_error_is_caught(self):
        _write_file(os.path.join(self.temp_dir, 'locked2.mp3'))
        monitor = _make_monitor(self.temp_dir, self.uid)
        called = {'n': 0}
        monitor._process_file = lambda *a, **kw: called.__setitem__('n', called['n'] + 1)

        real_rename = Path.rename

        def fake_rename(self_path, target):
            if str(self_path).endswith('.mp3'):
                raise PermissionError("no perms")
            return real_rename(self_path, target)

        with patch.object(Path, 'rename', fake_rename):
            monitor._scan_directory_for_user(monitor.base_watch_directory, self.uid)
        self.assertEqual(called['n'], 0)

    def test_process_error_unlocks_file(self):
        _write_file(os.path.join(self.temp_dir, 'fails.mp3'))
        monitor = _make_monitor(self.temp_dir, self.uid)

        def explode(path, uid, tag_id=None):
            raise RuntimeError("processing blew up")
        monitor._process_file = explode

        monitor._scan_directory_for_user(monitor.base_watch_directory, self.uid)
        # On error, the .processing file is renamed back to the original name.
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir, 'fails.mp3')))
        self.assertFalse(os.path.exists(os.path.join(self.temp_dir, 'fails.mp3.processing')))


# ---------------------------------------------------------------------------
# _extract_user_id_from_dirname
# ---------------------------------------------------------------------------

class TestExtractUserId(unittest.TestCase):
    def setUp(self):
        self.monitor = FileMonitor(tempfile.mkdtemp(), check_interval=30)

    def tearDown(self):
        shutil.rmtree(str(self.monitor.base_watch_directory), ignore_errors=True)

    def test_user_prefix_format(self):
        self.assertEqual(self.monitor._extract_user_id_from_dirname('user123'), 123)

    def test_bare_number_format(self):
        self.assertEqual(self.monitor._extract_user_id_from_dirname('456'), 456)

    def test_case_insensitive_prefix(self):
        self.assertEqual(self.monitor._extract_user_id_from_dirname('USER7'), 7)

    def test_non_user_dirname_returns_none(self):
        self.assertIsNone(self.monitor._extract_user_id_from_dirname('meetings'))
        self.assertIsNone(self.monitor._extract_user_id_from_dirname('user_abc'))
        self.assertIsNone(self.monitor._extract_user_id_from_dirname(''))


# ---------------------------------------------------------------------------
# _is_file_stable (real implementation, stability_time=0 so no real sleep wait)
# ---------------------------------------------------------------------------

class TestIsFileStable(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stable_file_returns_true(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30)
        p = Path(self.temp_dir) / 'a.mp3'
        _write_file(str(p))
        with patch.object(fm_module.time, 'sleep', lambda *_: None):
            self.assertTrue(monitor._is_file_stable(p, stability_time=0))

    def test_missing_file_returns_false(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30)
        p = Path(self.temp_dir) / 'gone.mp3'
        with patch.object(fm_module.time, 'sleep', lambda *_: None):
            self.assertFalse(monitor._is_file_stable(p, stability_time=0))

    def test_growing_file_returns_false(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30)
        p = Path(self.temp_dir) / 'grow.mp3'
        _write_file(str(p), b'\x00' * 10)

        def grow(*_):
            _write_file(str(p), b'\x00' * 9999)
        with patch.object(fm_module.time, 'sleep', grow):
            self.assertFalse(monitor._is_file_stable(p, stability_time=0))


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------

class TestModeDispatch(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.temp_dir = tempfile.mkdtemp()
        self.username = _uniq('cov_fm_dispatch')
        self.user = User(
            username=self.username,
            email=f'{self.username}@test.com',
            password='fakehash',
            is_admin=True,
        )
        db.session.add(self.user)
        db.session.commit()
        self.uid = self.user.id

    def tearDown(self):
        Tag.query.filter_by(user_id=self.uid).delete()
        db.session.commit()
        u = db.session.get(User, self.uid)
        if u:
            db.session.delete(u)
            db.session.commit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.ctx.pop()

    def test_admin_only_no_admin_user_skips(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30, mode='admin_only')
        monitor._admin_user_id = None
        scanned = []
        monitor._scan_directory_for_user = lambda *a, **kw: scanned.append(a)
        monitor._scan_admin_directory()
        self.assertEqual(scanned, [])

    def test_admin_only_scans_root_and_tags(self):
        monitor = _make_monitor(self.temp_dir, self.uid, mode='admin_only')
        calls = {'dir': 0, 'tags': 0}
        monitor._scan_directory_for_user = lambda *a, **kw: calls.__setitem__('dir', calls['dir'] + 1)
        monitor._scan_tag_subdirectories = lambda *a, **kw: calls.__setitem__('tags', calls['tags'] + 1)
        monitor._scan_admin_directory()
        self.assertEqual(calls['dir'], 1)
        self.assertEqual(calls['tags'], 1)

    def test_user_directories_valid_and_invalid(self):
        # Valid: dir named user<id> for the real user.
        os.makedirs(os.path.join(self.temp_dir, f'user{self.uid}'))
        # Invalid: user-shaped dir with an id that isn't in _valid_users.
        os.makedirs(os.path.join(self.temp_dir, 'user99999999'))
        # A plain file at the top level should be ignored by this mode.
        _write_file(os.path.join(self.temp_dir, 'ignore.mp3'))

        monitor = _make_monitor(self.temp_dir, self.uid, mode='user_directories')
        scanned = []
        monitor._scan_directory_for_user = lambda d, u, **kw: scanned.append(u)
        monitor._scan_tag_subdirectories = lambda *a, **kw: None
        monitor._scan_user_directories()
        # Only the valid user's directory got scanned.
        self.assertEqual(scanned, [self.uid])

    def test_user_directories_missing_base_dir(self):
        monitor = _make_monitor(os.path.join(self.temp_dir, 'nope'), self.uid,
                                mode='user_directories')
        # base dir was created by __init__, so remove it to hit the not-exists path.
        shutil.rmtree(str(monitor.base_watch_directory), ignore_errors=True)
        monitor._scan_directory_for_user = MagicMock()
        ret = monitor._scan_user_directories()  # returns silently
        self.assertIsNone(ret)
        # Missing base dir -> nothing should be scanned.
        monitor._scan_directory_for_user.assert_not_called()

    def test_single_user_configured(self):
        monitor = _make_monitor(self.temp_dir, self.uid, mode='single_user')
        monitor._username_to_id = {self.username: self.uid}
        scanned = []
        monitor._scan_directory_for_user = lambda d, u, **kw: scanned.append(u)
        monitor._scan_tag_subdirectories = lambda *a, **kw: None
        with patch.dict(os.environ, {'AUTO_PROCESS_DEFAULT_USERNAME': self.username}):
            monitor._scan_single_user_directory()
        self.assertEqual(scanned, [self.uid])

    def test_single_user_not_configured(self):
        monitor = _make_monitor(self.temp_dir, self.uid, mode='single_user')
        monitor._scan_directory_for_user = MagicMock()
        env = {k: v for k, v in os.environ.items() if k != 'AUTO_PROCESS_DEFAULT_USERNAME'}
        with patch.dict(os.environ, env, clear=True):
            ret = monitor._scan_single_user_directory()
        self.assertIsNone(ret)
        # No default username configured -> nothing should be scanned.
        monitor._scan_directory_for_user.assert_not_called()

    def test_single_user_invalid_username(self):
        monitor = _make_monitor(self.temp_dir, self.uid, mode='single_user')
        monitor._username_to_id = {}
        monitor._scan_directory_for_user = MagicMock()
        with patch.dict(os.environ, {'AUTO_PROCESS_DEFAULT_USERNAME': 'ghost_user'}):
            ret = monitor._scan_single_user_directory()
        self.assertIsNone(ret)
        # Configured username isn't a valid user -> nothing should be scanned.
        monitor._scan_directory_for_user.assert_not_called()


# ---------------------------------------------------------------------------
# _update_user_cache
# ---------------------------------------------------------------------------

class TestUpdateUserCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cache_skipped_when_recent(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30)
        import time as _t
        monitor._last_user_cache_update = _t.time()  # very recent
        # If it tried to query, it would import src.app inside an app context;
        # the early return means _valid_users stays empty.
        monitor._update_user_cache()
        self.assertEqual(monitor._valid_users, {})

    def test_cache_populates_admin_and_users(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30)
        monitor._last_user_cache_update = 0  # force refresh
        monitor._update_user_cache()
        # There may be other users in the shared DB; just assert the structure.
        self.assertIsInstance(monitor._valid_users, dict)
        self.assertIsInstance(monitor._username_to_id, dict)
        self.assertGreater(monitor._last_user_cache_update, 0)


# ---------------------------------------------------------------------------
# Admin user selection in _update_user_cache (line 101: is_admin=True)
# ---------------------------------------------------------------------------

class TestAdminUserSelection(unittest.TestCase):
    """Guards the admin-directory scan against picking the wrong user.

    _update_user_cache caches the admin via User.query.filter_by(is_admin=True).
    _scan_admin_directory then processes that cached user's directory. If the
    query flips to is_admin=False it would cache a NON-admin user, so every
    assertion below checks that the user driving the admin scan is_admin=True.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.temp_dir = tempfile.mkdtemp()
        self.admin_username = _uniq('cov_fm_admin')
        self.normal_username = _uniq('cov_fm_normal')
        self.admin = User(
            username=self.admin_username,
            email=f'{self.admin_username}@test.com',
            password='fakehash',
            is_admin=True,
        )
        self.normal = User(
            username=self.normal_username,
            email=f'{self.normal_username}@test.com',
            password='fakehash',
            is_admin=False,
        )
        db.session.add_all([self.admin, self.normal])
        db.session.commit()
        self.admin_id = self.admin.id
        self.normal_id = self.normal.id

    def tearDown(self):
        for uid in (self.admin_id, self.normal_id):
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
                db.session.commit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.ctx.pop()

    def test_admin_scan_targets_an_admin_user_not_a_normal_user(self):
        monitor = FileMonitor(self.temp_dir, check_interval=30, mode='admin_only')
        monitor._last_user_cache_update = 0  # force a real refresh

        monitor._update_user_cache()

        # The cached admin id must resolve to a user flagged is_admin=True.
        # (Shared DB: there may be other admins; we don't assume *which* admin
        # is picked, only that it IS an admin. The is_admin=True->False mutant
        # would cache a non-admin user, failing this.)
        self.assertIsNotNone(monitor._admin_user_id)
        selected = db.session.get(User, monitor._admin_user_id)
        self.assertIsNotNone(selected)
        self.assertTrue(selected.is_admin)
        # The normal user we created must never be the one selected.
        self.assertNotEqual(monitor._admin_user_id, self.normal_id)

        # And the admin directory scan drives _process_file with that admin's id.
        scanned = []
        monitor._scan_directory_for_user = lambda d, u, **kw: scanned.append(u)
        monitor._scan_tag_subdirectories = lambda *a, **kw: None
        monitor._scan_admin_directory()

        self.assertEqual(scanned, [monitor._admin_user_id])
        scanned_user = db.session.get(User, scanned[0])
        self.assertTrue(scanned_user.is_admin)
        self.assertNotEqual(scanned[0], self.normal_id)


# ---------------------------------------------------------------------------
# Full _process_file flow (heavy externals mocked)
# ---------------------------------------------------------------------------

class TestProcessFile(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.temp_dir = tempfile.mkdtemp()
        self.staging_dir = tempfile.mkdtemp()
        self.username = _uniq('cov_fm_proc')
        self.user = User(
            username=self.username,
            email=f'{self.username}@test.com',
            password='fakehash',
            is_admin=True,
        )
        db.session.add(self.user)
        db.session.commit()
        self.uid = self.user.id
        self.monitor = _make_monitor(self.temp_dir, self.uid)

    def tearDown(self):
        rec_ids = [r.id for r in Recording.query.filter_by(user_id=self.uid).all()]
        if rec_ids:
            RecordingTag.query.filter(
                RecordingTag.recording_id.in_(rec_ids)
            ).delete(synchronize_session=False)
        Recording.query.filter_by(user_id=self.uid).delete()
        Tag.query.filter_by(user_id=self.uid).delete()
        db.session.commit()
        u = db.session.get(User, self.uid)
        if u:
            db.session.delete(u)
            db.session.commit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        shutil.rmtree(self.staging_dir, ignore_errors=True)
        self.ctx.pop()

    def _make_processing_file(self, name='audio.mp3'):
        p = Path(self.temp_dir) / (name + '.processing')
        _write_file(str(p))
        return p

    def _build_storage_mock(self):
        storage = MagicMock()
        storage.get_staging_dir.return_value = self.staging_dir
        storage.build_recording_key.return_value = 'recordings/test/key.mp3'
        stored = MagicMock()
        stored.locator = 'local:recordings/test/key.mp3'
        storage.upload_local_file.return_value = stored
        return storage

    def _patches(self, storage, enqueue_capture, convert_passthrough=True):
        """Patch the externals _process_file imports. convert_if_needed and the
        ffprobe helpers are module-level imports on src.file_monitor; storage,
        job_queue, file_hash, registry, transcribe_audio_task are imported
        inside the function so we patch them at their origin module."""
        import contextlib

        def fake_convert(src_path, **kw):
            res = MagicMock()
            res.output_path = src_path  # leave the staged copy in place
            res.was_converted = False
            res.was_compressed = False
            return res

        job_queue_mock = MagicMock()
        job_queue_mock.enqueue.side_effect = lambda **kw: enqueue_capture.append(kw)

        registry_mock = MagicMock()
        registry_mock.get_active_connector.return_value = None

        cm = contextlib.ExitStack()
        cm.enter_context(patch.object(fm_module, 'convert_if_needed', fake_convert))
        cm.enter_context(patch.object(fm_module, 'get_codec_info',
                                      lambda *a, **kw: {'audio_codec': 'mp3', 'has_video': False}))
        cm.enter_context(patch.object(fm_module, 'get_creation_date', lambda *a, **kw: None))
        cm.enter_context(patch.object(fm_module, 'get_duration', lambda *a, **kw: 12.5))
        cm.enter_context(patch('src.services.storage.get_storage_service',
                               lambda: storage))
        cm.enter_context(patch('src.services.job_queue.job_queue', job_queue_mock))
        cm.enter_context(patch('src.services.transcription.get_registry',
                               lambda: registry_mock))
        cm.enter_context(patch('src.utils.file_hash.compute_file_sha256',
                               lambda *a, **kw: 'deadbeef' * 8))
        return cm

    def test_process_file_creates_recording_and_enqueues(self):
        p = self._make_processing_file()
        storage = self._build_storage_mock()
        enqueued = []
        with self._patches(storage, enqueued):
            self.monitor._process_file(p, self.uid)

        recs = Recording.query.filter_by(user_id=self.uid).all()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.status, 'PENDING')
        self.assertTrue(rec.is_inbox)
        self.assertEqual(rec.processing_source, 'auto_process')
        self.assertEqual(rec.audio_duration_seconds, 12.5)
        self.assertEqual(rec.audio_path, 'local:recordings/test/key.mp3')
        self.assertEqual(rec.file_hash, 'deadbeef' * 8)
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0]['recording_id'], rec.id)
        self.assertEqual(enqueued[0]['job_type'], 'transcribe')
        # The locked file is removed from the watch dir.
        self.assertFalse(p.exists())

    def test_process_file_applies_tag_and_job_params(self):
        p = self._make_processing_file('tagged.mp3')
        tag = Tag(
            name=_uniq('proc_tag'),
            user_id=self.uid,
            is_auto_process=True,
            auto_process_folder_name='proc-folder',
            default_hotwords='Foo, Bar',
            default_initial_prompt='a prompt',
            default_language='es',
            default_min_speakers=2,
            default_max_speakers=4,
            custom_prompt='summarize this',
        )
        db.session.add(tag)
        db.session.commit()

        storage = self._build_storage_mock()
        enqueued = []
        with self._patches(storage, enqueued):
            self.monitor._process_file(p, self.uid, tag_id=tag.id)

        rec = Recording.query.filter_by(user_id=self.uid).first()
        self.assertIsNotNone(rec)
        assoc = RecordingTag.query.filter_by(recording_id=rec.id, tag_id=tag.id).first()
        self.assertIsNotNone(assoc)

        params = enqueued[0]['params']
        self.assertEqual(params['hotwords'], 'Foo, Bar')
        self.assertEqual(params['initial_prompt'], 'a prompt')
        self.assertEqual(params['language'], 'es')
        self.assertEqual(params['min_speakers'], 2)
        self.assertEqual(params['max_speakers'], 4)
        self.assertEqual(params['custom_prompt'], 'summarize this')
        self.assertEqual(params['tag_id'], tag.id)

    def test_process_file_logs_conversion_and_compression(self):
        # convert_if_needed reporting was_converted/was_compressed exercises the
        # logging branches (411, 413).
        p = self._make_processing_file('converted.mp3')
        storage = self._build_storage_mock()
        enqueued = []

        import contextlib
        from datetime import datetime as _dt

        def fake_convert(src_path, **kw):
            res = MagicMock()
            res.output_path = src_path
            res.was_converted = True
            res.original_codec = 'amr'
            res.final_codec = 'mp3'
            res.was_compressed = True
            res.size_reduction_percent = 42.0
            return res

        with self._patches(storage, enqueued):
            with patch.object(fm_module, 'convert_if_needed', fake_convert):
                self.monitor._process_file(p, self.uid)
        self.assertEqual(len(enqueued), 1)

    def test_process_file_video_passthrough_skips_conversion(self):
        p = self._make_processing_file('movie.mp4')
        storage = self._build_storage_mock()
        enqueued = []

        def convert_should_not_run(*a, **kw):
            self.fail("convert_if_needed should be skipped for video passthrough")

        with self._patches(storage, enqueued):
            with patch.object(fm_module, 'VIDEO_PASSTHROUGH_ASR', True), \
                 patch.object(fm_module, 'get_codec_info',
                              lambda *a, **kw: {'audio_codec': 'aac', 'has_video': True}), \
                 patch.object(fm_module, 'convert_if_needed', convert_should_not_run):
                self.monitor._process_file(p, self.uid)
        self.assertEqual(len(enqueued), 1)

    def test_process_file_ffmpeg_error_propagates(self):
        from src.utils.ffmpeg_utils import FFmpegError
        p = self._make_processing_file('badconvert.mp3')
        storage = self._build_storage_mock()
        enqueued = []

        def boom(*a, **kw):
            raise FFmpegError("conversion failed")

        with self._patches(storage, enqueued):
            with patch.object(fm_module, 'convert_if_needed', boom):
                with self.assertRaises(FFmpegError):
                    self.monitor._process_file(p, self.uid)
        self.assertEqual(enqueued, [])

    def test_process_file_unknown_user_raises(self):
        p = self._make_processing_file('orphan.mp3')
        storage = self._build_storage_mock()
        enqueued = []
        with self._patches(storage, enqueued):
            with self.assertRaises(ValueError):
                self.monitor._process_file(p, 999999999)
        # Nothing enqueued, no recording created for the bad id.
        self.assertEqual(enqueued, [])

    def test_process_file_duplicate_hash_and_metadata_date(self):
        # Pre-create a recording with the same hash to hit the duplicate branch,
        # and return a creation date from metadata to hit that branch too.
        from datetime import datetime as _dt
        existing = Recording(
            title='dup',
            audio_path='local:x',
            original_filename='x.mp3',
            status='COMPLETED',
            user_id=self.uid,
            file_hash='deadbeef' * 8,
        )
        db.session.add(existing)
        db.session.commit()

        p = self._make_processing_file('dup_incoming.mp3')
        storage = self._build_storage_mock()
        enqueued = []
        with self._patches(storage, enqueued):
            with patch.object(fm_module, 'get_creation_date',
                              lambda *a, **kw: _dt(2020, 1, 2, 3, 4, 5)):
                self.monitor._process_file(p, self.uid)

        recs = Recording.query.filter_by(user_id=self.uid).all()
        # Both the pre-existing and the new (processed anyway) recording exist.
        self.assertEqual(len(recs), 2)
        new_rec = [r for r in recs if r.id != existing.id][0]
        self.assertEqual(new_rec.meeting_date, _dt(2020, 1, 2, 3, 4, 5))
        self.assertEqual(len(enqueued), 1)

    def test_process_file_scan_tag_subdir_routes_tag_id(self):
        # Exercise _scan_tag_subdirectories end-to-end: matching folder -> tag_id.
        tag = Tag(
            name=_uniq('subdir_tag'),
            user_id=self.uid,
            is_auto_process=True,
            auto_process_folder_name='subdir-folder',
        )
        db.session.add(tag)
        db.session.commit()

        folder = Path(self.temp_dir) / 'subdir-folder'
        folder.mkdir()
        _write_file(str(folder / 'clip.mp3'))
        # Also a hidden dir and a user-shaped dir, both must be skipped.
        (Path(self.temp_dir) / '.hidden').mkdir()
        (Path(self.temp_dir) / 'user5').mkdir()

        captured = []
        self.monitor._scan_directory_for_user = (
            lambda d, u, tag_id=None: captured.append((str(d), u, tag_id))
        )
        self.monitor._scan_tag_subdirectories(self.monitor.base_watch_directory, self.uid)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][2], tag.id)

    def test_process_file_falls_back_to_user_transcription_settings(self):
        # Give the user transcription defaults; no tag, so these should flow
        # into job params.
        self.user.transcription_language = 'de'
        self.user.transcription_hotwords = 'Alpha'
        self.user.transcription_initial_prompt = 'user prompt'
        db.session.commit()

        p = self._make_processing_file('userdefaults.mp3')
        storage = self._build_storage_mock()
        enqueued = []
        with self._patches(storage, enqueued):
            self.monitor._process_file(p, self.uid)

        params = enqueued[0]['params']
        self.assertEqual(params['language'], 'de')
        self.assertEqual(params['hotwords'], 'Alpha')
        self.assertEqual(params['initial_prompt'], 'user prompt')


# ---------------------------------------------------------------------------
# Module-level helpers: start / stop / status / ensure folders
# ---------------------------------------------------------------------------

class TestModuleHelpers(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._orig_monitor = fm_module.file_monitor

    def tearDown(self):
        # Restore the module global; never leave a started monitor behind.
        if fm_module.file_monitor is not None and fm_module.file_monitor is not self._orig_monitor:
            try:
                fm_module.file_monitor.running = False
            except Exception:
                pass
        fm_module.file_monitor = self._orig_monitor
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_status_when_not_running(self):
        fm_module.file_monitor = None
        self.assertEqual(get_file_monitor_status(), {'running': False})

    def test_status_when_running(self):
        m = FileMonitor(self.temp_dir, check_interval=17, mode='single_user')
        m.running = True  # pretend running WITHOUT starting a thread
        fm_module.file_monitor = m
        status = get_file_monitor_status()
        self.assertTrue(status['running'])
        self.assertEqual(status['mode'], 'single_user')
        self.assertEqual(status['check_interval'], 17)
        self.assertEqual(status['watch_directory'], str(m.base_watch_directory))

    def test_stop_file_monitor_clears_global(self):
        m = FileMonitor(self.temp_dir, check_interval=30)
        m.running = True
        m.thread = None  # stop() will skip join when thread is None
        fm_module.file_monitor = m
        stop_file_monitor()
        self.assertIsNone(fm_module.file_monitor)
        self.assertFalse(m.running)

    def test_start_disabled_does_not_create_monitor(self):
        fm_module.file_monitor = None
        env = {
            'ENABLE_AUTO_PROCESSING': 'false',
            'AUTO_PROCESS_WATCH_DIR': self.temp_dir,
            'AUTO_PROCESS_MODE': 'admin_only',
        }
        with patch.dict(os.environ, env):
            start_file_monitor()
        self.assertIsNone(fm_module.file_monitor)

    def test_start_invalid_mode_returns_early(self):
        fm_module.file_monitor = None
        env = {
            'ENABLE_AUTO_PROCESSING': 'true',
            'AUTO_PROCESS_WATCH_DIR': self.temp_dir,
            'AUTO_PROCESS_MODE': 'bogus_mode',
        }
        with patch.dict(os.environ, env):
            start_file_monitor()
        self.assertIsNone(fm_module.file_monitor)

    def test_start_already_running_is_noop(self):
        m = FileMonitor(self.temp_dir, check_interval=30)
        m.running = True
        fm_module.file_monitor = m
        # Should return immediately without touching env / creating a new one.
        start_file_monitor()
        self.assertIs(fm_module.file_monitor, m)

    def test_start_enabled_starts_monitor_without_real_thread(self):
        fm_module.file_monitor = None
        env = {
            'ENABLE_AUTO_PROCESSING': 'true',
            'AUTO_PROCESS_WATCH_DIR': self.temp_dir,
            'AUTO_PROCESS_MODE': 'admin_only',
            'AUTO_PROCESS_CHECK_INTERVAL': '5',
        }
        # Patch FileMonitor.start so no real watcher thread / loop is launched,
        # and stub the tag-folder startup helper.
        with patch.dict(os.environ, env), \
             patch.object(FileMonitor, 'start', lambda self: setattr(self, 'running', True)), \
             patch.object(fm_module, '_ensure_tag_folders_on_startup', lambda *a, **kw: None):
            start_file_monitor()
        self.assertIsNotNone(fm_module.file_monitor)
        self.assertEqual(fm_module.file_monitor.mode, 'admin_only')
        self.assertEqual(fm_module.file_monitor.check_interval, 5)

    def test_ensure_tag_folders_user_directories_mode(self):
        # Happy path through the tag loop, user_directories branch (folder under
        # user<id>), plus a tag whose user no longer exists (skip) and one with
        # no folder name (skip).
        with app.app_context():
            username = _uniq('cov_fm_ensure')
            user = User(username=username, email=f'{username}@t.com',
                        password='x', is_admin=True)
            db.session.add(user)
            db.session.commit()
            uid = user.id
            t1 = Tag(name=_uniq('t1'), user_id=uid, is_auto_process=True,
                     auto_process_folder_name='folder-one')
            t2 = Tag(name=_uniq('t2'), user_id=uid, is_auto_process=True,
                     auto_process_folder_name=None)
            db.session.add_all([t1, t2])
            db.session.commit()
            try:
                _ensure_tag_folders_on_startup(app, self.temp_dir, 'user_directories')
                expected = os.path.join(self.temp_dir, f'user{uid}', 'folder-one')
                self.assertTrue(os.path.isdir(expected))
            finally:
                Tag.query.filter_by(user_id=uid).delete()
                db.session.commit()
                u = db.session.get(User, uid)
                if u:
                    db.session.delete(u)
                    db.session.commit()

    def test_ensure_tag_folders_no_tags_returns_early(self):
        # Force the query to return an empty list -> early return (line 598-599).
        with patch('src.models.Tag') as TagMock:
            TagMock.query.filter_by.return_value.all.return_value = []
            ret = _ensure_tag_folders_on_startup(app, self.temp_dir, 'admin_only')
        self.assertIsNone(ret)
        # No auto-process tags -> early return, so no folders were created.
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_ensure_tag_folders_handles_query_error(self):
        # Force the inner Tag query to raise; the helper swallows and logs.
        with patch('src.models.Tag') as TagMock:
            TagMock.query.filter_by.side_effect = RuntimeError("db down")
            # Should swallow the error (not raise) and create no folders.
            result = _ensure_tag_folders_on_startup(app, self.temp_dir, 'admin_only')
        assert result is None
        assert os.listdir(self.temp_dir) == []


if __name__ == '__main__':
    unittest.main()
