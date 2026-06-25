#!/usr/bin/env python3
"""
Coverage tests for src/services/retention.py.

This module performs data-destructive auto-deletion of recordings/audio by
retention policy, so correctness matters. All external side effects (storage
byte deletion, speaker cleanup) are mocked so tests are hermetic and offline.

Strategy:
- Create User/Recording/Tag rows directly via models.
- Patch module-level policy constants (ENABLE_AUTO_DELETION, GLOBAL_RETENTION_DAYS,
  DELETION_MODE) since retention.py reads them as module globals.
- Patch retention.get_storage_service so no real disk/storage I/O happens, and
  assert the deleter is called with the correct locator.
"""

import os
import sys
import uuid
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Tag, RecordingTag
from src.services import retention


def _make_storage_mock(exists=True):
    """Build a fake storage service whose .exists()/.delete() are observable."""
    storage = MagicMock()
    storage.exists.return_value = exists
    return storage


class RetentionTestBase(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        suffix = uuid.uuid4().hex[:8]
        self.user = User(
            username=f"ret_{suffix}",
            email=f"ret_{suffix}@local.test",
            password="fakehash",
        )
        db.session.add(self.user)
        db.session.commit()
        # process_auto_deletion() operates on ALL recordings globally, and the
        # pytest session DB is shared across test files. Wipe any recordings
        # left behind by other suites so this class's global stat-counter
        # assertions are deterministic regardless of run order.
        RecordingTag.query.delete()
        Recording.query.delete()
        db.session.commit()
        self._created_recordings = []
        self._created_tags = []

    def tearDown(self):
        db.session.rollback()
        # Remove any recordings still present (full-delete tests may already remove them)
        for rec in self._created_recordings:
            obj = db.session.get(Recording, rec) if isinstance(rec, int) else rec
            if obj is not None and db.session.get(Recording, obj.id) is not None:
                RecordingTag.query.filter_by(recording_id=obj.id).delete()
                db.session.delete(obj)
        for tag in self._created_tags:
            obj = db.session.get(Tag, tag.id)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()
        db.session.delete(self.user)
        db.session.commit()
        self.ctx.pop()

    def make_recording(self, age_days=100, status='COMPLETED', audio_path='user/a.mp3',
                       deletion_exempt=False, audio_deleted_at=None):
        rec = Recording(
            user_id=self.user.id,
            title="rec",
            status=status,
            audio_path=audio_path,
            deletion_exempt=deletion_exempt,
            audio_deleted_at=audio_deleted_at,
        )
        db.session.add(rec)
        db.session.flush()
        # created_at has a default, so override after flush to set the age
        rec.created_at = datetime.utcnow() - timedelta(days=age_days)
        db.session.commit()
        self._created_recordings.append(rec)
        return rec

    def make_tag(self, retention_days=None, protect_from_deletion=False):
        tag = Tag(
            name=f"tag_{uuid.uuid4().hex[:8]}",
            user_id=self.user.id,
            retention_days=retention_days,
            protect_from_deletion=protect_from_deletion,
        )
        db.session.add(tag)
        db.session.flush()
        self._created_tags.append(tag)
        return tag

    def tag_recording(self, recording, tag):
        assoc = RecordingTag(recording_id=recording.id, tag_id=tag.id, order=0)
        db.session.add(assoc)
        db.session.commit()


# ---------------------------------------------------------------------------
# is_recording_exempt_from_deletion
# ---------------------------------------------------------------------------

class TestExemption(RetentionTestBase):
    def test_manual_deletion_exempt_flag(self):
        rec = self.make_recording(deletion_exempt=True)
        self.assertTrue(retention.is_recording_exempt_from_deletion(rec))

    def test_protect_from_deletion_tag(self):
        rec = self.make_recording()
        tag = self.make_tag(protect_from_deletion=True)
        self.tag_recording(rec, tag)
        self.assertTrue(retention.is_recording_exempt_from_deletion(rec))

    def test_retention_days_minus_one_is_protected(self):
        rec = self.make_recording()
        tag = self.make_tag(retention_days=-1)
        self.tag_recording(rec, tag)
        self.assertTrue(retention.is_recording_exempt_from_deletion(rec))

    def test_not_exempt_by_default(self):
        rec = self.make_recording()
        self.assertFalse(retention.is_recording_exempt_from_deletion(rec))


# ---------------------------------------------------------------------------
# get_retention_days_for_recording
# ---------------------------------------------------------------------------

class TestRetentionDays(RetentionTestBase):
    def test_no_tags_no_global_returns_none(self):
        rec = self.make_recording()
        with patch.object(retention, 'GLOBAL_RETENTION_DAYS', 0):
            self.assertIsNone(retention.get_retention_days_for_recording(rec))

    def test_global_retention_used_when_no_tags(self):
        rec = self.make_recording()
        with patch.object(retention, 'GLOBAL_RETENTION_DAYS', 30):
            self.assertEqual(retention.get_retention_days_for_recording(rec), 30)

    def test_tag_retention_overrides_global(self):
        rec = self.make_recording()
        tag = self.make_tag(retention_days=7)
        self.tag_recording(rec, tag)
        with patch.object(retention, 'GLOBAL_RETENTION_DAYS', 30):
            self.assertEqual(retention.get_retention_days_for_recording(rec), 7)

    def test_shortest_tag_retention_wins(self):
        rec = self.make_recording()
        t1 = self.make_tag(retention_days=20)
        t2 = self.make_tag(retention_days=5)
        self.tag_recording(rec, t1)
        self.tag_recording(rec, t2)
        with patch.object(retention, 'GLOBAL_RETENTION_DAYS', 30):
            self.assertEqual(retention.get_retention_days_for_recording(rec), 5)

    def test_minus_one_tag_ignored_falls_to_global(self):
        # retention_days == -1 is skipped here (handled by exemption), so global applies
        rec = self.make_recording()
        tag = self.make_tag(retention_days=-1)
        self.tag_recording(rec, tag)
        with patch.object(retention, 'GLOBAL_RETENTION_DAYS', 30):
            self.assertEqual(retention.get_retention_days_for_recording(rec), 30)


# ---------------------------------------------------------------------------
# process_auto_deletion
# ---------------------------------------------------------------------------

class TestProcessAutoDeletion(RetentionTestBase):
    def _run(self, enabled=True, global_days=30, mode='full_recording', storage=None):
        if storage is None:
            storage = _make_storage_mock(exists=True)
        with patch.object(retention, 'ENABLE_AUTO_DELETION', enabled), \
             patch.object(retention, 'GLOBAL_RETENTION_DAYS', global_days), \
             patch.object(retention, 'DELETION_MODE', mode), \
             patch.object(retention, 'get_storage_service', return_value=storage), \
             patch('src.services.speaker_cleanup.cleanup_orphaned_speakers',
                   return_value={'speakers_deleted': 0, 'embeddings_removed': 0,
                                 'speakers_evaluated': 0}):
            stats = retention.process_auto_deletion()
        return stats, storage

    def test_disabled_returns_error(self):
        stats, _ = self._run(enabled=False)
        self.assertEqual(stats, {'error': 'Auto-deletion is not enabled'})

    def test_full_delete_removes_old_recording_and_audio(self):
        rec = self.make_recording(age_days=100, audio_path='user/old.mp3')
        rec_id = rec.id
        stats, storage = self._run(mode='full_recording', global_days=30)
        self.assertEqual(stats['deleted_full'], 1)
        # Row is gone
        self.assertIsNone(db.session.get(Recording, rec_id))
        # Audio bytes deletion invoked with the right locator
        storage.delete.assert_called_with('user/old.mp3', missing_ok=True)

    def test_recording_within_retention_kept(self):
        rec = self.make_recording(age_days=5, audio_path='user/new.mp3')
        rec_id = rec.id
        stats, storage = self._run(mode='full_recording', global_days=30)
        self.assertEqual(stats['deleted_full'], 0)
        self.assertIsNotNone(db.session.get(Recording, rec_id))
        storage.delete.assert_not_called()

    def test_exempt_recording_skipped(self):
        rec = self.make_recording(age_days=100, deletion_exempt=True)
        rec_id = rec.id
        stats, storage = self._run(mode='full_recording', global_days=30)
        self.assertEqual(stats['exempted'], 1)
        self.assertEqual(stats['deleted_full'], 0)
        self.assertIsNotNone(db.session.get(Recording, rec_id))
        storage.delete.assert_not_called()

    def test_no_retention_policy_skips_recording(self):
        rec = self.make_recording(age_days=100)
        rec_id = rec.id
        # global_days=0 and no tag retention => no policy applies
        stats, storage = self._run(mode='full_recording', global_days=0)
        self.assertEqual(stats['skipped_no_retention'], 1)
        self.assertEqual(stats['deleted_full'], 0)
        self.assertIsNotNone(db.session.get(Recording, rec_id))

    def test_audio_only_mode_deletes_audio_keeps_row(self):
        rec = self.make_recording(age_days=100, audio_path='user/keep.mp3')
        rec_id = rec.id
        stats, storage = self._run(mode='audio_only', global_days=30)
        self.assertEqual(stats['deleted_audio_only'], 1)
        # Row preserved
        row = db.session.get(Recording, rec_id)
        self.assertIsNotNone(row)
        # Audio marked deleted and bytes removed
        self.assertIsNotNone(row.audio_deleted_at)
        storage.delete.assert_called_with('user/keep.mp3', missing_ok=True)

    def test_audio_only_skips_already_deleted_audio(self):
        # In audio_only mode the query filters out audio_deleted_at != None,
        # so this recording is not even checked.
        rec = self.make_recording(
            age_days=100, audio_path='user/gone.mp3',
            audio_deleted_at=datetime.utcnow(),
        )
        stats, storage = self._run(mode='audio_only', global_days=30)
        self.assertEqual(stats['checked'], 0)
        self.assertEqual(stats['deleted_audio_only'], 0)
        storage.delete.assert_not_called()

    def test_audio_only_missing_file_marks_timestamp_without_delete(self):
        rec = self.make_recording(age_days=100, audio_path='user/missing.mp3')
        rec_id = rec.id
        storage = _make_storage_mock(exists=False)  # storage.exists -> False
        stats, storage = self._run(mode='audio_only', global_days=30, storage=storage)
        # Falls into the "already deleted or doesn't exist" branch
        self.assertEqual(stats['deleted_audio_only'], 0)
        row = db.session.get(Recording, rec_id)
        self.assertIsNotNone(row.audio_deleted_at)
        storage.delete.assert_not_called()

    def test_full_mode_completes_prior_audio_only_deletion(self):
        # Recording whose audio was already deleted (audio_only earlier);
        # full mode should still remove the row.
        rec = self.make_recording(
            age_days=100, audio_path='user/x.mp3',
            audio_deleted_at=datetime.utcnow(),
        )
        rec_id = rec.id
        stats, storage = self._run(mode='full_recording', global_days=30)
        self.assertEqual(stats['deleted_full'], 1)
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_no_eligible_recordings_is_noop(self):
        # Only a young recording exists; nothing deleted.
        rec = self.make_recording(age_days=1, audio_path='user/young.mp3')
        rec_id = rec.id
        stats, storage = self._run(mode='full_recording', global_days=30)
        self.assertEqual(stats['deleted_full'], 0)
        self.assertEqual(stats['deleted_audio_only'], 0)
        self.assertIsNotNone(db.session.get(Recording, rec_id))
        storage.delete.assert_not_called()

    def test_tag_retention_drives_deletion_over_global(self):
        # Global says keep 365 days, but a 1-day tag forces deletion of a 100-day recording.
        rec = self.make_recording(age_days=100, audio_path='user/tagged.mp3')
        rec_id = rec.id
        tag = self.make_tag(retention_days=1)
        self.tag_recording(rec, tag)
        stats, storage = self._run(mode='full_recording', global_days=365)
        self.assertEqual(stats['deleted_full'], 1)
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_error_during_deletion_increments_errors_and_rolls_back(self):
        rec = self.make_recording(age_days=100, audio_path='user/boom.mp3')
        rec_id = rec.id
        storage = _make_storage_mock(exists=True)
        storage.delete.side_effect = RuntimeError("storage down")
        stats, storage = self._run(mode='full_recording', global_days=30, storage=storage)
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(stats['deleted_full'], 0)
        # Recording survives because the transaction was rolled back
        self.assertIsNotNone(db.session.get(Recording, rec_id))


# ---------------------------------------------------------------------------
# process_auto_deletion: the GLOBAL_RETENTION_DAYS > 0 / has_global_retention
# flag (retention.py lines 94 & 97).
#
# `has_global_retention = GLOBAL_RETENTION_DAYS > 0` and the subsequent
# `if not has_global_retention:` only gate an informational log line. They have
# no other behavioral effect (actual deletion eligibility is computed per
# recording by get_retention_days_for_recording). So killing mutations to these
# lines requires asserting on whether the "No global retention configured" log
# message is emitted, in addition to pinning the > 0 boundary behaviorally.
# ---------------------------------------------------------------------------

class TestGlobalRetentionFlag(RetentionTestBase):
    NO_GLOBAL_MSG = "No global retention configured"

    def _run_capturing_logs(self, global_days, mode='full_recording', storage=None):
        if storage is None:
            storage = _make_storage_mock(exists=True)
        with patch.object(retention, 'ENABLE_AUTO_DELETION', True), \
             patch.object(retention, 'GLOBAL_RETENTION_DAYS', global_days), \
             patch.object(retention, 'DELETION_MODE', mode), \
             patch.object(retention, 'get_storage_service', return_value=storage), \
             patch('src.services.speaker_cleanup.cleanup_orphaned_speakers',
                   return_value={'speakers_deleted': 0, 'embeddings_removed': 0,
                                 'speakers_evaluated': 0}), \
             patch.object(app.logger, 'info') as mock_info:
            stats = retention.process_auto_deletion()
        logged_no_global = any(
            c.args and self.NO_GLOBAL_MSG in str(c.args[0])
            for c in mock_info.call_args_list
        )
        return stats, storage, logged_no_global

    def test_global_days_zero_is_no_policy_old_recording_kept_and_logs_no_global(self):
        # Boundary: GLOBAL_RETENTION_DAYS == 0 means "no global policy", NOT
        # "delete everything older than 0 days". An old, untagged recording must
        # survive, and the "No global retention configured" branch must run.
        rec = self.make_recording(age_days=100, audio_path='user/zero.mp3')
        rec_id = rec.id
        stats, storage, logged_no_global = self._run_capturing_logs(global_days=0)
        # Behavioral: no policy => skipped, nothing deleted, row + bytes intact.
        self.assertEqual(stats['skipped_no_retention'], 1)
        self.assertEqual(stats['deleted_full'], 0)
        self.assertIsNotNone(db.session.get(Recording, rec_id))
        storage.delete.assert_not_called()
        # has_global_retention must be False at GLOBAL==0, so the
        # `if not has_global_retention:` log fires. Inverting line 97 suppresses
        # this log, breaking the assertion.
        self.assertTrue(
            logged_no_global,
            "Expected 'No global retention configured' log when GLOBAL_RETENTION_DAYS == 0",
        )

    def test_global_days_positive_makes_old_recording_eligible_and_no_global_log(self):
        # GLOBAL_RETENTION_DAYS > 0 => a global policy applies. An old recording
        # is eligible and gets deleted, and the "No global retention configured"
        # branch must NOT run.
        rec = self.make_recording(age_days=100, audio_path='user/pos.mp3')
        rec_id = rec.id
        stats, storage, logged_no_global = self._run_capturing_logs(global_days=30)
        # Behavioral: global policy applies => old recording deleted.
        self.assertEqual(stats['deleted_full'], 1)
        self.assertEqual(stats['skipped_no_retention'], 0)
        self.assertIsNone(db.session.get(Recording, rec_id))
        storage.delete.assert_called_with('user/pos.mp3', missing_ok=True)
        # has_global_retention must be True at GLOBAL==30, so the no-global log
        # must NOT fire. Mutating line 94 (> 0 -> < 0) makes it False (log fires);
        # mutating line 97 (if not -> if) makes it log too. Either breaks this.
        self.assertFalse(
            logged_no_global,
            "Did not expect 'No global retention configured' log when "
            "GLOBAL_RETENTION_DAYS > 0",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
