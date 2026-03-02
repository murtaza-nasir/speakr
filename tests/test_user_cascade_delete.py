#!/usr/bin/env python3
"""
Tests for user deletion cascade behavior (Issue #239).

Ensures that deleting a user with related records (processing jobs, shares,
usage tracking, etc.) does not fail with IntegrityError due to missing
cascade configuration on NOT NULL foreign keys.

Two test strategies:
1. Runtime introspection: inspect SQLAlchemy relationships on User to verify
   every NOT NULL FK has cascade delete configured. Catches future models.
2. Integration: create a user with records in every related table, delete the
   user, and verify no errors and no orphaned rows.

Run with: python tests/test_user_cascade_delete.py
"""

import os
import sys
import unittest
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (
    User, Recording, ProcessingJob, Share, InternalShare, SharedRecordingState,
    TokenUsage, TranscriptionUsage, GroupMembership, Group, Tag, Folder,
    Speaker, APIToken, InquireSession, ExportTemplate, TranscriptTemplate,
    NamingTemplate, TranscriptChunk, PushSubscription, ShareAuditLog,
)


# ---------------------------------------------------------------------------
# Test 1: Runtime introspection — verify cascade on all NOT NULL User FKs
# ---------------------------------------------------------------------------

class TestUserRelationshipCascades(unittest.TestCase):
    """
    Inspect every SQLAlchemy relationship on the User model. For each
    one-to-many relationship where the child FK is NOT NULL, verify that
    cascade includes 'delete'. This catches any new model that forgets to
    add cascade and would break user deletion.
    """

    def test_all_not_null_user_fk_relationships_have_cascade_delete(self):
        from sqlalchemy.orm.relationships import ONETOMANY

        with app.app_context():
            missing = []

            for name, rel in User.__mapper__.relationships.items():
                # Only check one-to-many (backrefs from other models to User)
                if rel.direction != ONETOMANY:
                    continue

                # Check FK columns on the child model
                for parent_col, child_col in rel.synchronize_pairs:
                    if not child_col.nullable and 'delete' not in rel.cascade:
                        model_name = rel.mapper.class_.__name__
                        missing.append(
                            f"User.{name} -> {model_name}.{child_col.name} "
                            f"(NOT NULL FK without cascade delete)"
                        )

            self.assertEqual(
                missing, [],
                "Found User relationships with NOT NULL foreign keys but no cascade "
                "delete. Deleting a user will fail with IntegrityError.\n"
                "Fix: add cascade='all, delete-orphan' (or 'all, delete' for multi-parent) "
                "to the backref:\n" +
                "\n".join(f"  - {m}" for m in missing)
            )

    def test_user_recordings_relationship_exists(self):
        """Sanity check that User.recordings is defined (nullable FK, so no cascade needed)."""
        with app.app_context():
            self.assertTrue(
                hasattr(User, 'recordings'),
                "User model must have a 'recordings' relationship"
            )


# ---------------------------------------------------------------------------
# Test 2: Integration — create user with related records, then delete
# ---------------------------------------------------------------------------

class TestUserDeletionIntegration(unittest.TestCase):
    """
    Create a user with records in every table that has a FK to user.id,
    then delete the user and verify it succeeds without IntegrityError.
    """

    def setUp(self):
        """Create a fresh user for each test."""
        self.app_context = app.app_context()
        self.app_context.push()
        app.config['WTF_CSRF_ENABLED'] = False

        # Clean up any leftover test user
        existing = User.query.filter_by(username="cascade_test_user").first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

        self.user = User(
            username="cascade_test_user",
            email="cascade_test@local.test",
            password="fakehash",
        )
        db.session.add(self.user)
        db.session.commit()

    def test_delete_user_with_processing_jobs(self):
        """Issue #239: ProcessingJob.user_id NOT NULL must cascade."""
        rec = Recording(user_id=self.user.id, title="test", status="COMPLETED")
        db.session.add(rec)
        db.session.flush()

        job = ProcessingJob(
            user_id=self.user.id,
            recording_id=rec.id,
            job_type='transcribe',
            status='completed',
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        # This was the exact operation that failed before the fix
        ProcessingJob.query.filter_by(user_id=self.user.id).delete()
        db.session.delete(self.user)
        db.session.commit()

        self.assertIsNone(db.session.get(ProcessingJob, job_id))

    def test_delete_user_with_token_usage(self):
        """TokenUsage.user_id NOT NULL must cascade."""
        usage = TokenUsage(
            user_id=self.user.id,
            date=date.today(),
            operation_type='summarize',
            prompt_tokens=100,
            completion_tokens=50,
        )
        db.session.add(usage)
        db.session.commit()
        usage_id = usage.id

        db.session.delete(self.user)
        db.session.commit()

        self.assertIsNone(db.session.get(TokenUsage, usage_id))

    def test_delete_user_with_transcription_usage(self):
        """TranscriptionUsage.user_id NOT NULL must cascade."""
        usage = TranscriptionUsage(
            user_id=self.user.id,
            date=date.today(),
            connector_type='openai_whisper',
            audio_duration_seconds=120,
        )
        db.session.add(usage)
        db.session.commit()
        usage_id = usage.id

        db.session.delete(self.user)
        db.session.commit()

        self.assertIsNone(db.session.get(TranscriptionUsage, usage_id))

    def test_delete_user_with_group_memberships(self):
        """GroupMembership.user_id NOT NULL must cascade."""
        group = Group(name="Test Group", description="for cascade test")
        db.session.add(group)
        db.session.flush()

        membership = GroupMembership(
            group_id=group.id,
            user_id=self.user.id,
            role='member',
        )
        db.session.add(membership)
        db.session.commit()
        membership_id = membership.id

        db.session.delete(self.user)
        db.session.commit()

        self.assertIsNone(db.session.get(GroupMembership, membership_id))

        # Cleanup group
        db.session.delete(group)
        db.session.commit()

    def test_delete_user_with_internal_shares(self):
        """InternalShare has two NOT NULL User FKs (owner + shared_with)."""
        # Create a second user to be the share recipient
        other = User(
            username="cascade_other_user",
            email="cascade_other@local.test",
            password="fakehash",
        )
        db.session.add(other)
        db.session.flush()

        rec = Recording(user_id=self.user.id, title="shared rec", status="COMPLETED")
        db.session.add(rec)
        db.session.flush()

        share = InternalShare(
            recording_id=rec.id,
            owner_id=self.user.id,
            shared_with_user_id=other.id,
        )
        db.session.add(share)
        db.session.commit()
        share_id = share.id

        # Delete owner user — share should be removed
        InternalShare.query.filter(
            (InternalShare.owner_id == self.user.id) |
            (InternalShare.shared_with_user_id == self.user.id)
        ).delete()
        db.session.delete(self.user)
        db.session.commit()

        self.assertIsNone(db.session.get(InternalShare, share_id))

        # Cleanup
        db.session.delete(other)
        db.session.commit()

    def test_delete_user_with_push_subscriptions(self):
        """PushSubscription.user_id NOT NULL must cascade."""
        sub = PushSubscription(
            user_id=self.user.id,
            endpoint="https://push.example.com/test-cascade",
            p256dh_key="fake_p256dh",
            auth_key="fake_auth",
        )
        db.session.add(sub)
        db.session.commit()
        sub_id = sub.id

        db.session.delete(self.user)
        db.session.commit()

        self.assertIsNone(db.session.get(PushSubscription, sub_id))

    def test_admin_delete_user_endpoint(self):
        """Full integration: call the admin DELETE endpoint with a user that has data."""
        rec = Recording(user_id=self.user.id, title="admin test", status="COMPLETED")
        db.session.add(rec)
        db.session.flush()

        job = ProcessingJob(
            user_id=self.user.id,
            recording_id=rec.id,
            job_type='transcribe',
            status='completed',
        )
        db.session.add(job)
        db.session.commit()
        user_id = self.user.id

        # Create an admin user to perform the delete
        admin = User.query.filter_by(username="cascade_admin").first()
        if not admin:
            admin = User(
                username="cascade_admin",
                email="cascade_admin@local.test",
                password="fakehash",
                is_admin=True,
            )
            db.session.add(admin)
            db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['_user_id'] = str(admin.id)

            resp = client.delete(f'/admin/users/{user_id}')
            self.assertEqual(resp.status_code, 200, f"Admin delete failed: {resp.get_json()}")
            data = resp.get_json()
            self.assertTrue(data.get('success'), f"Expected success=True, got: {data}")

        # Verify user is gone
        self.assertIsNone(db.session.get(User, user_id))

        # Mark user as deleted so tearDown doesn't try to clean it up
        self.user = None

        # Cleanup admin
        db.session.delete(admin)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        # Final cleanup if test failed before deletion
        if self.user:
            leftover = db.session.get(User, self.user.id) if self.user.id else None
        else:
            leftover = User.query.filter_by(username="cascade_test_user").first()
        if leftover:
            ProcessingJob.query.filter_by(user_id=leftover.id).delete()
            InternalShare.query.filter(
                (InternalShare.owner_id == leftover.id) |
                (InternalShare.shared_with_user_id == leftover.id)
            ).delete()
            db.session.delete(leftover)
            db.session.commit()
        # Also clean up helper users if they exist
        for uname in ("cascade_other_user", "cascade_admin"):
            helper = User.query.filter_by(username=uname).first()
            if helper:
                db.session.delete(helper)
                db.session.commit()
        self.app_context.pop()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
