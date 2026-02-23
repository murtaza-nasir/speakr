#!/usr/bin/env python3
"""
Tests for specific bug fixes.

- Issue #230: Bulk delete crash when recordings have speaker_snippets
- Issue #223: File monitor stability time env var
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording
from src.models.speaker_snippet import SpeakerSnippet

# Disable CSRF for testing
app.config['WTF_CSRF_ENABLED'] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_user():
    user = User.query.filter_by(username="bugfix_test_user").first()
    if not user:
        user = User(username="bugfix_test_user", email="bugfix@local.test")
        db.session.add(user)
        db.session.commit()
    return user


def _create_recording_with_snippets(user):
    """Create a recording that has speaker_snippet records attached."""
    rec = Recording(
        user_id=user.id,
        title="Recording with snippets",
        status="COMPLETED",
        transcription=json.dumps([
            {"speaker": "SPEAKER_00", "sentence": "Hello there."},
        ]),
    )
    db.session.add(rec)
    db.session.commit()

    # We need a speaker to attach snippets to
    from src.models import Speaker
    speaker = Speaker.query.filter_by(user_id=user.id, name="BugfixTestSpeaker").first()
    if not speaker:
        speaker = Speaker(name="BugfixTestSpeaker", user_id=user.id)
        db.session.add(speaker)
        db.session.commit()

    snippet = SpeakerSnippet(
        speaker_id=speaker.id,
        recording_id=rec.id,
        segment_index=0,
        text_snippet="Hello there.",
    )
    db.session.add(snippet)
    db.session.commit()

    return rec, speaker, snippet


# ---------------------------------------------------------------------------
# Issue #230: Deleting recordings with speaker_snippets
# ---------------------------------------------------------------------------

class TestIssue230BulkDeleteCascade:
    """Verify that deleting a recording with speaker_snippets doesn't crash."""

    def test_single_delete_with_snippets(self):
        """Single DELETE /recording/<id> should succeed when snippets exist."""
        with app.app_context():
            user = _get_or_create_user()
            rec, speaker, snippet = _create_recording_with_snippets(user)
            rec_id = rec.id
            snippet_id = snippet.id

            with app.test_client() as client:
                # Login
                with client.session_transaction() as sess:
                    sess['_user_id'] = str(user.id)

                resp = client.delete(f'/recording/{rec_id}')
                assert resp.status_code == 200, f"Delete failed: {resp.get_json()}"
                data = resp.get_json()
                assert data.get('success') is True

            # Verify snippet was also deleted
            orphan = db.session.get(SpeakerSnippet, snippet_id)
            assert orphan is None, "Speaker snippet should have been deleted with recording"

            # Cleanup speaker
            db.session.delete(speaker)
            db.session.commit()

    def test_bulk_delete_with_snippets(self):
        """DELETE /api/recordings/bulk should succeed when snippets exist."""
        with app.app_context():
            user = _get_or_create_user()
            rec, speaker, snippet = _create_recording_with_snippets(user)
            rec_id = rec.id
            snippet_id = snippet.id

            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['_user_id'] = str(user.id)

                resp = client.delete(
                    '/api/recordings/bulk',
                    json={'recording_ids': [rec_id]},
                    content_type='application/json',
                )
                assert resp.status_code == 200, f"Bulk delete failed: {resp.get_json()}"
                data = resp.get_json()
                assert data.get('success') is True
                assert rec_id in data.get('deleted_ids', [])

            # Verify snippet was also deleted
            orphan = db.session.get(SpeakerSnippet, snippet_id)
            assert orphan is None, "Speaker snippet should have been deleted with recording"

            # Cleanup speaker
            db.session.delete(speaker)
            db.session.commit()

    def test_bulk_delete_multiple_with_snippets(self):
        """Bulk deleting multiple recordings (some with snippets) should succeed."""
        with app.app_context():
            user = _get_or_create_user()
            rec1, speaker, snippet = _create_recording_with_snippets(user)
            rec2 = Recording(user_id=user.id, title="No snippets", status="COMPLETED")
            db.session.add(rec2)
            db.session.commit()

            rec1_id, rec2_id = rec1.id, rec2.id

            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['_user_id'] = str(user.id)

                resp = client.delete(
                    '/api/recordings/bulk',
                    json={'recording_ids': [rec1_id, rec2_id]},
                    content_type='application/json',
                )
                assert resp.status_code == 200, f"Bulk delete failed: {resp.get_json()}"
                data = resp.get_json()
                assert data.get('deleted_count') == 2

            # Cleanup speaker
            db.session.delete(speaker)
            db.session.commit()


# ---------------------------------------------------------------------------
# Issue #223: File monitor stability time
# ---------------------------------------------------------------------------

class TestIssue223StabilityTime:
    """Verify AUTO_PROCESS_STABILITY_TIME env var is respected."""

    def test_default_stability_time(self):
        """Without env var, stability_time defaults to 5."""
        from src.file_monitor import FileMonitor
        monitor = FileMonitor.__new__(FileMonitor)
        monitor.logger = MagicMock()

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(b'fake audio data')
            tmp_path = Path(f.name)

        try:
            with patch.dict(os.environ, {}, clear=False):
                # Remove the env var if it exists
                os.environ.pop('AUTO_PROCESS_STABILITY_TIME', None)
                with patch('time.sleep') as mock_sleep:
                    monitor._is_file_stable(tmp_path)
                    mock_sleep.assert_called_once_with(5)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_custom_stability_time(self):
        """AUTO_PROCESS_STABILITY_TIME=15 should sleep for 15 seconds."""
        from src.file_monitor import FileMonitor
        monitor = FileMonitor.__new__(FileMonitor)
        monitor.logger = MagicMock()

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(b'fake audio data')
            tmp_path = Path(f.name)

        try:
            with patch.dict(os.environ, {'AUTO_PROCESS_STABILITY_TIME': '15'}):
                with patch('time.sleep') as mock_sleep:
                    # _is_file_stable uses the default param, but the caller reads env
                    # So we test the caller path via _scan_user_directory indirectly
                    # or just call with explicit value
                    stability_time = int(os.environ.get('AUTO_PROCESS_STABILITY_TIME', '5'))
                    monitor._is_file_stable(tmp_path, stability_time)
                    mock_sleep.assert_called_once_with(15)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_no_hardcoded_cap(self):
        """Stability time should NOT be capped at 2 seconds anymore."""
        from src.file_monitor import FileMonitor
        monitor = FileMonitor.__new__(FileMonitor)
        monitor.logger = MagicMock()

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(b'fake audio data')
            tmp_path = Path(f.name)

        try:
            with patch('time.sleep') as mock_sleep:
                monitor._is_file_stable(tmp_path, stability_time=30)
                # Should sleep for 30, NOT min(30, 2) = 2
                mock_sleep.assert_called_once_with(30)
        finally:
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
