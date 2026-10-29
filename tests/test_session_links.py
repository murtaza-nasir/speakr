"""Recording-session links to deleted recordings (#505).

SQLite does not apply ON DELETE SET NULL, and it reuses the highest id. A
session left pointing at a deleted recording could therefore point at a later
recording of another user, and the stalled-session watchdog would stitch into
it. Deleting a recording now clears the link, the watchdog and the stitch
worker check ownership, and a one-time migration clears old links.
"""

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, RecordingSession


def _user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"{prefix[:11]}_{suffix}", email=f"{prefix}_{suffix}@local.test", password="x")
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user, status="STITCHING"):
    rec = Recording(user_id=user.id, title="r", status=status,
                    processing_source="recording_session", mime_type="audio/webm")
    db.session.add(rec)
    db.session.commit()
    return rec


def _session(user, status, recording_id=None, last_seen=None):
    s = RecordingSession(user_id=user.id, mime_type="audio/webm", status=status,
                         chunk_count=2, bytes_received=256, finalized_recording_id=recording_id)
    db.session.add(s)
    db.session.flush()
    if last_seen is not None:
        s.last_seen_at = last_seen
    db.session.commit()
    return s


def _sqlite_only():
    if db.engine.name != "sqlite":
        pytest.skip("PostgreSQL enforces the foreign key, so a stale link cannot exist")


def _stale_link(session, missing_id):
    db.session.execute(db.text("UPDATE recording_session SET finalized_recording_id = :r WHERE id = :s"),
                       {"r": missing_id, "s": session.id})
    db.session.commit()
    db.session.expire_all()


def test_deleting_a_recording_clears_its_session_link_only():
    from src.services.recording_deletion import delete_recording_completely
    with app.app_context():
        user = _user("link_del")
        doomed, kept = _recording(user, "COMPLETED"), _recording(user, "COMPLETED")
        s_doomed = _session(user, "finalized", doomed.id)
        s_kept = _session(user, "finalized", kept.id)
        doomed_id = doomed.id

        with patch("src.services.storage.get_storage_service", return_value=MagicMock()):
            delete_recording_completely(doomed)
        db.session.expire_all()

        assert db.session.get(Recording, doomed_id) is None
        assert db.session.get(RecordingSession, s_doomed.id).finalized_recording_id is None
        assert db.session.get(RecordingSession, s_doomed.id).status == "finalized"
        assert db.session.get(RecordingSession, s_kept.id).finalized_recording_id == kept.id


def _run_cleanup():
    from src.api.recording_sessions import cleanup_expired_sessions
    from src.services.job_queue import job_queue
    enqueued = []
    with patch.object(job_queue, "enqueue", lambda **kw: enqueued.append(kw) or 1):
        cleanup_expired_sessions(app=app)
    db.session.expire_all()
    return enqueued


def test_watchdog_expires_a_stalled_session_whose_recording_is_another_users():
    old = datetime.utcnow() - timedelta(hours=48)
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp(prefix="speakr-test-505-")
        owner, other = _user("link_own"), _user("link_oth")
        others_rec = _recording(other, "COMPLETED")
        stalled = _session(owner, "finalizing", others_rec.id, last_seen=old)

        enqueued = _run_cleanup()

        assert not any(c.get("recording_id") == others_rec.id for c in enqueued)
        assert db.session.get(RecordingSession, stalled.id).status == "expired"
        assert db.session.get(Recording, others_rec.id).status == "COMPLETED"


def test_watchdog_expires_a_stalled_session_whose_recording_is_gone():
    old = datetime.utcnow() - timedelta(hours=48)
    with app.app_context():
        _sqlite_only()
        app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp(prefix="speakr-test-505-")
        owner = _user("link_gone")
        stalled = _session(owner, "finalizing", None, last_seen=old)
        _stale_link(stalled, 10_000_000 + int(uuid.uuid4().int % 1000))

        enqueued = _run_cleanup()

        assert not any(c.get("params", {}).get("session_id") == stalled.id for c in enqueued)
        assert db.session.get(RecordingSession, stalled.id).status == "expired"


def test_watchdog_still_rekicks_the_users_own_stalled_recording():
    old = datetime.utcnow() - timedelta(hours=48)
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp(prefix="speakr-test-505-")
        owner = _user("link_mine")
        rec = _recording(owner)
        stalled = _session(owner, "finalizing", rec.id, last_seen=old)

        enqueued = _run_cleanup()

        assert any(c.get("job_type") == "stitch" and c.get("recording_id") == rec.id for c in enqueued)
        assert db.session.get(RecordingSession, stalled.id).status == "finalizing"


def test_stitch_job_for_a_recording_that_is_not_the_sessions_leaves_it_untouched():
    from src.services.job_queue import job_queue
    with app.app_context():
        owner, other = _user("link_st1"), _user("link_st2")
        own_rec = _recording(owner)
        others_rec = _recording(other, "COMPLETED")
        s = _session(owner, "finalizing", own_rec.id)

        with patch("src.services.recording_stitch.stitch_recording_session") as stitch:
            job_queue._run_stitch(SimpleNamespace(id=1), others_rec, {"session_id": s.id})
        stitch.assert_not_called()
        db.session.expire_all()
        assert db.session.get(Recording, others_rec.id).status == "COMPLETED"


def test_stitch_job_for_the_sessions_own_recording_still_runs():
    from src.services.job_queue import job_queue
    with app.app_context():
        owner = _user("link_st3")
        rec = _recording(owner)
        s = _session(owner, "finalizing", rec.id)

        with patch("src.services.recording_stitch.stitch_recording_session",
                   return_value=(rec.id, "/x", {})) as stitch, \
             patch("src.services.recording_stitch.kickoff_transcription_for_stitched") as kickoff:
            job_queue._run_stitch(SimpleNamespace(id=1), rec, {"session_id": s.id})
        stitch.assert_called_once_with(s.id)
        kickoff.assert_called_once()


def test_migration_clears_only_links_to_missing_recordings():
    from src.init_db import _clear_stale_session_links
    with app.app_context():
        _sqlite_only()
        user = _user("link_mig")
        rec = _recording(user, "COMPLETED")
        valid = _session(user, "finalized", rec.id)
        stale = _session(user, "finalized", None)
        _stale_link(stale, 20_000_000 + int(uuid.uuid4().int % 1000))

        _clear_stale_session_links(db.engine, app)
        db.session.expire_all()

        assert db.session.get(RecordingSession, stale.id).finalized_recording_id is None
        assert db.session.get(RecordingSession, valid.id).finalized_recording_id == rec.id


def test_migration_runs_on_any_database():
    from src.init_db import _clear_stale_session_links
    with app.app_context():
        user = _user("link_any")
        rec = _recording(user, "COMPLETED")
        valid = _session(user, "finalized", rec.id)
        _clear_stale_session_links(db.engine, app)
        db.session.expire_all()
        assert db.session.get(RecordingSession, valid.id).finalized_recording_id == rec.id
