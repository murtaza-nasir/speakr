#!/usr/bin/env python3
"""
Tests for auto-process tag folders feature (Issue #242).

Covers:
- Model fields and database schema
- Tag API create/update/delete with auto-process
- File monitor tag subdirectory scanning
- Edge cases (sanitization, multiple tags, feature disabled)

Run with: python tests/test_auto_process_tags.py
"""

import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Tag, RecordingTag
import src.api.tags as tags_module


def _enable_auto_processing():
    """Context manager-like helper to patch ENABLE_AUTO_PROCESSING at module level."""
    return patch.object(tags_module, 'ENABLE_AUTO_PROCESSING', True)


def _disable_auto_processing():
    return patch.object(tags_module, 'ENABLE_AUTO_PROCESSING', False)


# ---------------------------------------------------------------------------
# Layer 1: Model & Database
# ---------------------------------------------------------------------------

class TestTagAutoProcessModel(unittest.TestCase):
    """Test Tag model auto-process fields."""

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_tag_is_auto_process_defaults_to_false(self):
        """New tags should default is_auto_process to False."""
        tag = Tag(name='test', user_id=1)
        self.assertFalse(tag.is_auto_process)

    def test_tag_auto_process_folder_name_persists(self):
        """auto_process_folder_name should be settable."""
        tag = Tag(name='test', user_id=1, auto_process_folder_name='my-folder')
        self.assertEqual(tag.auto_process_folder_name, 'my-folder')

    def test_tag_to_dict_includes_auto_process_fields(self):
        """to_dict() should include is_auto_process and auto_process_folder_name."""
        tag = Tag(name='test', user_id=1, is_auto_process=True, auto_process_folder_name='test-folder')
        tag.group = None
        tag.naming_template = None
        tag.export_template = None
        tag.recording_associations = []
        d = tag.to_dict()
        self.assertIn('is_auto_process', d)
        self.assertTrue(d['is_auto_process'])
        self.assertIn('auto_process_folder_name', d)
        self.assertEqual(d['auto_process_folder_name'], 'test-folder')

    def test_tag_auto_process_column_exists(self):
        """Verify the columns exist in the database schema."""
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('tag')]
        self.assertIn('is_auto_process', columns)
        self.assertIn('auto_process_folder_name', columns)


# ---------------------------------------------------------------------------
# Layer 2: API Tests
# ---------------------------------------------------------------------------

class TestTagAutoProcessAPI(unittest.TestCase):
    """Test Tag API endpoints with auto-process."""

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        app.config['WTF_CSRF_ENABLED'] = False
        self.client = app.test_client()
        self.temp_dir = tempfile.mkdtemp()

        # Create test user
        self.user = User.query.filter_by(username='autotest_user').first()
        if not self.user:
            self.user = User(
                username='autotest_user',
                email='autotest@test.com',
                password='fakehash',
                is_admin=True
            )
            db.session.add(self.user)
            db.session.commit()

        # Log in
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.user.id)

    def tearDown(self):
        Tag.query.filter_by(user_id=self.user.id).delete()
        db.session.commit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.app_context.pop()

    def test_create_tag_with_auto_process_creates_folder(self):
        """Creating a tag with is_auto_process=true should create a folder."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.post('/api/tags', json={
                'name': 'Auto Test Tag',
                'is_auto_process': True
            }, content_type='application/json')

        self.assertIn(resp.status_code, [200, 201])
        data = resp.get_json()
        self.assertTrue(data.get('is_auto_process'))
        self.assertEqual(data.get('auto_process_folder_name'), 'auto-test-tag')
        self.assertTrue(os.path.isdir(os.path.join(self.temp_dir, 'auto-test-tag')))

    def test_create_tag_default_no_auto_process(self):
        """Creating a tag without is_auto_process should not enable it."""
        resp = self.client.post('/api/tags', json={
            'name': 'Normal Tag'
        }, content_type='application/json')

        self.assertIn(resp.status_code, [200, 201])
        data = resp.get_json()
        self.assertFalse(data.get('is_auto_process'))
        self.assertIsNone(data.get('auto_process_folder_name'))

    def test_update_tag_enable_auto_process(self):
        """Enabling auto-process on an existing tag should create folder."""
        # Create tag without auto-process first
        resp = self.client.post('/api/tags', json={
            'name': 'Update Test'
        }, content_type='application/json')
        self.assertIn(resp.status_code, [200, 201])
        tag_id = resp.get_json()['id']

        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.put(f'/api/tags/{tag_id}', json={
                'is_auto_process': True
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['is_auto_process'])
        self.assertTrue(os.path.isdir(os.path.join(self.temp_dir, 'update-test')))

    def test_update_tag_disable_auto_process_removes_empty_folder(self):
        """Disabling auto-process should remove the empty folder."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.post('/api/tags', json={
                'name': 'Disable Test',
                'is_auto_process': True
            }, content_type='application/json')
            self.assertIn(resp.status_code, [200, 201])
            tag_id = resp.get_json()['id']
            folder_path = os.path.join(self.temp_dir, 'disable-test')
            self.assertTrue(os.path.isdir(folder_path))

            resp = self.client.put(f'/api/tags/{tag_id}', json={
                'is_auto_process': False
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['is_auto_process'])
        self.assertFalse(os.path.exists(folder_path))

    def test_auto_process_rejected_for_group_tags(self):
        """Group tags should not be allowed to have auto-process."""
        from src.models import Group, GroupMembership
        group = Group.query.filter_by(name='autotest_group').first()
        if not group:
            group = Group(name='autotest_group')
            db.session.add(group)
            db.session.commit()
        membership = GroupMembership.query.filter_by(group_id=group.id, user_id=self.user.id).first()
        if not membership:
            membership = GroupMembership(group_id=group.id, user_id=self.user.id, role='admin')
            db.session.add(membership)
            db.session.commit()

        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.post('/api/tags', json={
                'name': 'Group Auto Tag',
                'group_id': group.id,
                'is_auto_process': True
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('not available for group tags', resp.get_json()['error'])

        # Clean up
        db.session.delete(membership)
        db.session.delete(group)
        db.session.commit()

    def test_auto_process_rejected_when_feature_disabled(self):
        """Auto-process should be rejected when ENABLE_AUTO_PROCESSING is false."""
        with _disable_auto_processing():
            resp = self.client.post('/api/tags', json={
                'name': 'Disabled Feature Tag',
                'is_auto_process': True
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('not enabled', resp.get_json()['error'])

    def test_delete_tag_removes_auto_process_folder(self):
        """Deleting an auto-process tag should remove its empty folder."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.post('/api/tags', json={
                'name': 'Delete Test',
                'is_auto_process': True
            }, content_type='application/json')
            self.assertIn(resp.status_code, [200, 201])
            tag_id = resp.get_json()['id']
            folder_path = os.path.join(self.temp_dir, 'delete-test')
            self.assertTrue(os.path.isdir(folder_path))

            resp = self.client.delete(f'/api/tags/{tag_id}')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(folder_path))

    def test_folder_name_unchanged_on_tag_rename(self):
        """Renaming a tag should not change its auto_process_folder_name."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.post('/api/tags', json={
                'name': 'Original Name',
                'is_auto_process': True
            }, content_type='application/json')
            self.assertIn(resp.status_code, [200, 201])
            tag_id = resp.get_json()['id']
            original_folder = resp.get_json()['auto_process_folder_name']

            resp = self.client.put(f'/api/tags/{tag_id}', json={
                'name': 'Renamed Tag'
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['auto_process_folder_name'], original_folder)

    def test_trigger_endpoint_requires_admin(self):
        """Non-admin users should get 403 from trigger endpoint."""
        non_admin = User.query.filter_by(username='autotest_nonadmin').first()
        if not non_admin:
            non_admin = User(
                username='autotest_nonadmin',
                email='autotest_nonadmin@test.com',
                password='fakehash',
                is_admin=False
            )
            db.session.add(non_admin)
            db.session.commit()

        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(non_admin.id)

        resp = self.client.post('/admin/auto-process/trigger',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 403)

        db.session.delete(non_admin)
        db.session.commit()

    def test_trigger_endpoint_returns_400_when_not_running(self):
        """Trigger should return 400 when file monitor is not running."""
        from src import file_monitor as fm_module
        original = fm_module.file_monitor
        fm_module.file_monitor = None
        try:
            resp = self.client.post('/admin/auto-process/trigger',
                                    content_type='application/json')
            self.assertEqual(resp.status_code, 400)
        finally:
            fm_module.file_monitor = original


# ---------------------------------------------------------------------------
# Layer 3: File Monitor Integration
# ---------------------------------------------------------------------------

class TestFileMonitorTagSubdirs(unittest.TestCase):
    """Test file monitor tag subdirectory scanning."""

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        self.temp_dir = tempfile.mkdtemp()

        self.user = User.query.filter_by(username='autotest_monitor_user').first()
        if not self.user:
            self.user = User(
                username='autotest_monitor_user',
                email='autotest_monitor@test.com',
                password='fakehash',
                is_admin=True
            )
            db.session.add(self.user)
            db.session.commit()

    def tearDown(self):
        Tag.query.filter_by(user_id=self.user.id).delete()
        db.session.commit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.app_context.pop()

    def _create_auto_process_tag(self, name, folder_name=None):
        """Helper to create an auto-process tag."""
        from src.api.tags import _sanitize_folder_name
        if not folder_name:
            folder_name = _sanitize_folder_name(name)
        tag = Tag(
            name=name,
            user_id=self.user.id,
            is_auto_process=True,
            auto_process_folder_name=folder_name
        )
        db.session.add(tag)
        db.session.commit()
        return tag

    def test_scan_detects_file_in_tag_subdirectory(self):
        """Files in tag subdirectories should be detected by scanner."""
        from src.file_monitor import FileMonitor

        tag = self._create_auto_process_tag('Meetings')
        tag_dir = os.path.join(self.temp_dir, tag.auto_process_folder_name)
        os.makedirs(tag_dir)

        test_file = os.path.join(tag_dir, 'test.mp3')
        with open(test_file, 'wb') as f:
            f.write(b'\x00' * 1024)

        monitor = FileMonitor(self.temp_dir, check_interval=30, mode='admin_only')
        monitor._admin_user_id = self.user.id
        monitor._valid_users = {self.user.id: self.user.username}

        processed = []
        def mock_process(path, uid, tag_id=None):
            processed.append({'path': str(path), 'user_id': uid, 'tag_id': tag_id})
        monitor._process_file = mock_process
        monitor._is_file_stable = lambda *a, **kw: True

        monitor._scan_tag_subdirectories(monitor.base_watch_directory, self.user.id)

        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]['tag_id'], tag.id)

    def test_scan_applies_tag_to_recording(self):
        """Files processed from tag dirs should get the tag applied."""
        tag = self._create_auto_process_tag('Interviews')

        recording = Recording(
            title='Test Recording',
            audio_path='/tmp/test.mp3',
            original_filename='test.mp3',
            status='PENDING',
            user_id=self.user.id
        )
        db.session.add(recording)
        db.session.commit()

        rt = RecordingTag(recording_id=recording.id, tag_id=tag.id, order=0)
        db.session.add(rt)
        db.session.commit()

        associations = RecordingTag.query.filter_by(recording_id=recording.id).all()
        self.assertEqual(len(associations), 1)
        self.assertEqual(associations[0].tag_id, tag.id)

        db.session.delete(rt)
        db.session.delete(recording)
        db.session.commit()

    def test_scan_passes_tag_settings_to_job_params(self):
        """Tag settings (hotwords, prompt, language) should be passed to job params."""
        tag = self._create_auto_process_tag('Technical')
        tag.default_hotwords = 'Kubernetes, Docker'
        tag.default_initial_prompt = 'Technical discussion'
        tag.default_language = 'en'
        db.session.commit()

        job_params = {}
        if tag.default_hotwords:
            job_params['hotwords'] = tag.default_hotwords
        if tag.default_initial_prompt:
            job_params['initial_prompt'] = tag.default_initial_prompt
        if tag.default_language:
            job_params['language'] = tag.default_language
        job_params['tag_id'] = tag.id

        self.assertEqual(job_params['hotwords'], 'Kubernetes, Docker')
        self.assertEqual(job_params['initial_prompt'], 'Technical discussion')
        self.assertEqual(job_params['language'], 'en')
        self.assertEqual(job_params['tag_id'], tag.id)

    def test_root_directory_files_have_no_tag(self):
        """Files in the root watch directory should not get a tag."""
        from src.file_monitor import FileMonitor

        monitor = FileMonitor(self.temp_dir, check_interval=30, mode='admin_only')
        monitor._admin_user_id = self.user.id

        test_file = os.path.join(self.temp_dir, 'root_test.mp3')
        with open(test_file, 'wb') as f:
            f.write(b'\x00' * 1024)

        processed = []
        def mock_process(path, uid, tag_id=None):
            processed.append({'tag_id': tag_id})
        monitor._process_file = mock_process
        monitor._is_file_stable = lambda *a, **kw: True

        monitor._scan_directory_for_user(monitor.base_watch_directory, self.user.id)

        self.assertEqual(len(processed), 1)
        self.assertIsNone(processed[0]['tag_id'])

    def test_startup_ensures_tag_folders_exist(self):
        """start_file_monitor should create folders for existing auto-process tags."""
        tag = self._create_auto_process_tag('Startup Test')
        folder_path = os.path.join(self.temp_dir, tag.auto_process_folder_name)
        self.assertFalse(os.path.exists(folder_path))

        from src.file_monitor import _ensure_tag_folders_on_startup
        _ensure_tag_folders_on_startup(app, self.temp_dir, 'admin_only')

        self.assertTrue(os.path.isdir(folder_path))

    def test_folder_recreated_if_externally_deleted(self):
        """If a tag folder is deleted externally, startup should recreate it."""
        tag = self._create_auto_process_tag('Recreate Test')
        folder_path = os.path.join(self.temp_dir, tag.auto_process_folder_name)
        os.makedirs(folder_path, exist_ok=True)
        self.assertTrue(os.path.isdir(folder_path))

        os.rmdir(folder_path)
        self.assertFalse(os.path.exists(folder_path))

        from src.file_monitor import _ensure_tag_folders_on_startup
        _ensure_tag_folders_on_startup(app, self.temp_dir, 'admin_only')

        self.assertTrue(os.path.isdir(folder_path))

    def test_unmatched_subdirectory_ignored(self):
        """Subdirectories without matching tags should be skipped."""
        from src.file_monitor import FileMonitor

        random_dir = os.path.join(self.temp_dir, 'random-folder')
        os.makedirs(random_dir)
        test_file = os.path.join(random_dir, 'test.mp3')
        with open(test_file, 'wb') as f:
            f.write(b'\x00' * 1024)

        monitor = FileMonitor(self.temp_dir, check_interval=30, mode='admin_only')
        monitor._admin_user_id = self.user.id
        monitor._valid_users = {self.user.id: self.user.username}

        processed = []
        def mock_process(path, uid, tag_id=None):
            processed.append({'tag_id': tag_id})
        monitor._process_file = mock_process
        monitor._is_file_stable = lambda *a, **kw: True

        monitor._scan_tag_subdirectories(monitor.base_watch_directory, self.user.id)

        self.assertEqual(len(processed), 0)


# ---------------------------------------------------------------------------
# Layer 4: Edge Cases
# ---------------------------------------------------------------------------

class TestAutoProcessEdgeCases(unittest.TestCase):
    """Edge case tests for auto-process features."""

    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        app.config['WTF_CSRF_ENABLED'] = False
        self.temp_dir = tempfile.mkdtemp()

        self.user = User.query.filter_by(username='autotest_edge_user').first()
        if not self.user:
            self.user = User(
                username='autotest_edge_user',
                email='autotest_edge@test.com',
                password='fakehash',
                is_admin=True
            )
            db.session.add(self.user)
            db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.user.id)

    def tearDown(self):
        Tag.query.filter_by(user_id=self.user.id).delete()
        db.session.commit()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.app_context.pop()

    def test_sanitize_folder_name_special_characters(self):
        """Folder name sanitization should handle special characters."""
        from src.api.tags import _sanitize_folder_name

        self.assertEqual(_sanitize_folder_name('Hello World'), 'hello-world')
        self.assertEqual(_sanitize_folder_name('Test  Tag'), 'test-tag')
        self.assertEqual(_sanitize_folder_name('Special!@#$%'), 'special')
        self.assertEqual(_sanitize_folder_name('under_score'), 'under-score')
        self.assertEqual(_sanitize_folder_name('  Spaces  '), 'spaces')
        self.assertEqual(_sanitize_folder_name('MiXeD CaSe'), 'mixed-case')
        self.assertEqual(_sanitize_folder_name('dots.and-dashes'), 'dots.and-dashes')
        self.assertEqual(_sanitize_folder_name('!@#$%'), 'tag')

    def test_multiple_tags_different_folders_route_correctly(self):
        """Multiple auto-process tags should each have their own folder."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp1 = self.client.post('/api/tags', json={
                'name': 'Tag Alpha',
                'is_auto_process': True
            }, content_type='application/json')
            resp2 = self.client.post('/api/tags', json={
                'name': 'Tag Beta',
                'is_auto_process': True
            }, content_type='application/json')

        self.assertEqual(resp1.status_code, 201)
        self.assertEqual(resp2.status_code, 201)

        folder1 = resp1.get_json()['auto_process_folder_name']
        folder2 = resp2.get_json()['auto_process_folder_name']

        self.assertNotEqual(folder1, folder2)
        self.assertTrue(os.path.isdir(os.path.join(self.temp_dir, folder1)))
        self.assertTrue(os.path.isdir(os.path.join(self.temp_dir, folder2)))

    def test_user_directories_mode_tag_folders(self):
        """In user_directories mode, tag folders should be under user dir."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'user_directories'}):
            resp = self.client.post('/api/tags', json={
                'name': 'User Dir Tag',
                'is_auto_process': True
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        expected_path = os.path.join(self.temp_dir, f'user{self.user.id}', data['auto_process_folder_name'])
        self.assertTrue(os.path.isdir(expected_path))

    def test_disable_auto_process_folder_not_empty_leaves_folder(self):
        """Disabling auto-process should leave the folder if it has files."""
        with _enable_auto_processing(), \
             patch.dict(os.environ, {'AUTO_PROCESS_WATCH_DIR': self.temp_dir, 'AUTO_PROCESS_MODE': 'admin_only'}):
            resp = self.client.post('/api/tags', json={
                'name': 'Nonempty Test',
                'is_auto_process': True
            }, content_type='application/json')
            self.assertIn(resp.status_code, [200, 201])
            tag_id = resp.get_json()['id']
            folder_name = resp.get_json()['auto_process_folder_name']
            folder_path = os.path.join(self.temp_dir, folder_name)

            # Put a file in the folder
            with open(os.path.join(folder_path, 'important.mp3'), 'wb') as f:
                f.write(b'\x00' * 100)

            resp = self.client.put(f'/api/tags/{tag_id}', json={
                'is_auto_process': False
            }, content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.isdir(folder_path))


if __name__ == '__main__':
    unittest.main()
