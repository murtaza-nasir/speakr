"""Text of the user in recording sessions.

A session row holds the title and notes typed when a browser recording was
finished (finalize_metadata) or the file name of an upload in slices
(upload_filename). The text is cleared when the session ends, the row of an
ended session is deleted with its recording, and a one-time cleanup (ledger id
0010) clears the rows of earlier releases.

SHARED-DB: users, recordings and sessions are removed afterwards.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, RecordingSession, User

app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True

ROOT = Path(__file__).resolve().parent.parent
TYPED = json.dumps({"title": "SENTINEL typed title", "notes": "SENTINEL typed notes"})
FILE_NAME = "SENTINEL jane doe.mp4"


@pytest.fixture
def users():
    with app.app_context():
        made = []

        def make(prefix="stc"):
            suffix = uuid.uuid4().hex[:8]
            user = User(username=f"{prefix[:9]}_{suffix}", email=f"{prefix}_{suffix}@local.test", password="x")
            db.session.add(user)
            db.session.commit()
            made.append(user.id)
            return user

        yield make
        db.session.rollback()
        for user_id in made:
            RecordingSession.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            ProcessingJob.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            Recording.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            db.session.delete(db.session.get(User, user_id))
        db.session.commit()


def _recording(user, status="COMPLETED"):
    rec = Recording(user_id=user.id, title="r", status=status,
                    processing_source="recording_session", mime_type="audio/webm")
    db.session.add(rec)
    db.session.commit()
    return rec


def _session(user, status, recording_id=None, kind="recorder", **fields):
    values = dict(user_id=user.id, mime_type="audio/webm", status=status, kind=kind, chunk_count=2,
                  bytes_received=256, finalized_recording_id=recording_id,
                  finalize_metadata=TYPED if kind == "recorder" else None,
                  upload_filename=FILE_NAME if kind == "sliced_upload" else None,
                  upload_total_bytes=256 if kind == "sliced_upload" else None)
    values.update(fields)
    last_seen = values.pop("last_seen_at", None)
    row = RecordingSession(**values)
    db.session.add(row)
    db.session.flush()
    if last_seen is not None:
        row.last_seen_at = last_seen
    db.session.commit()
    return row


def _client(user):
    from flask import g, has_app_context
    if has_app_context():
        g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return client


def _text(session_id):
    db.session.expire_all()
    row = db.session.get(RecordingSession, session_id)
    return None if row is None else (row.finalize_metadata, row.upload_filename)


# --- deletion of the recording ---------------------------------------------------

def test_deleting_a_recording_deletes_its_ended_sessions(users):
    from src.services.recording_deletion import delete_recording_completely
    user = users()
    doomed, kept = _recording(user), _recording(user)
    ended = [_session(user, "finalized", doomed.id).id,
             _session(user, "failed", doomed.id).id,
             _session(user, "finalized", doomed.id, kind="sliced_upload").id]
    other = _session(user, "finalized", kept.id, finalize_metadata=None).id
    unlinked = _session(user, "recording").id
    with patch("src.services.storage.get_storage_service", return_value=MagicMock()):
        delete_recording_completely(doomed)
    db.session.expire_all()
    assert [db.session.get(RecordingSession, sid) for sid in ended] == [None, None, None]
    assert db.session.get(RecordingSession, other).finalized_recording_id == kept.id
    assert _text(unlinked) == (TYPED, None)
    dump = str(db.session.execute(text(
        "SELECT finalize_metadata, upload_filename FROM recording_session WHERE user_id = :u AND id != :keep"),
        {"u": user.id, "keep": unlinked}).fetchall())
    assert "SENTINEL" not in dump


def test_a_session_still_at_work_keeps_its_row_without_the_text_and_the_link(users):
    """Its chunks are on disk and a stitch job may be reading them: the
    hourly cleanup expires it and removes its folder."""
    from src.services.recording_deletion import delete_recording_completely
    from src.api.recording_sessions import cleanup_expired_sessions
    from src.services.job_queue import job_queue
    user = users()
    doomed = _recording(user, "STITCHING")
    old = datetime.utcnow() - timedelta(hours=48)
    working = _session(user, "finalizing", doomed.id, last_seen_at=old).id
    with patch("src.services.storage.get_storage_service", return_value=MagicMock()):
        delete_recording_completely(doomed)
    db.session.expire_all()
    row = db.session.get(RecordingSession, working)
    assert (row.status, row.finalized_recording_id, row.finalize_metadata, row.upload_filename) == \
        ("finalizing", None, None, None)
    assert (row.chunk_count, row.bytes_received, row.mime_type) == (2, 256, "audio/webm")

    app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp(prefix="speakr-test-stc-")
    enqueued = []
    with patch.object(job_queue, "enqueue", lambda **kw: enqueued.append(kw) or 1):
        cleanup_expired_sessions(app=app)
    db.session.expire_all()
    assert db.session.get(RecordingSession, working).status == "expired"
    assert not any(c.get("params", {}).get("session_id") == working for c in enqueued)


def test_dismissing_the_failed_job_of_a_browser_recording_deletes_its_session(users):
    """DELETE /api/recordings/jobs/<id> removes a failed new recording without
    the deletion service."""
    user = users()
    rec = _recording(user, "FAILED")
    session_id = _session(user, "failed", rec.id).id
    job = ProcessingJob(user_id=user.id, recording_id=rec.id, job_type="stitch", status="failed",
                        is_new_upload=True, params=json.dumps({"session_id": session_id}))
    db.session.add(job)
    db.session.commit()
    rec_id = rec.id
    with patch("src.api.recordings._cancel_remote_asr_jobs"):
        response = _client(user).delete(f"/api/recordings/jobs/{job.id}")
    assert response.status_code == 200, response.data
    db.session.expire_all()
    assert db.session.get(Recording, rec_id) is None
    assert db.session.get(RecordingSession, session_id) is None


# --- the session ends --------------------------------------------------------------

def test_abort_clears_the_file_name(users):
    user = users()
    app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp(prefix="speakr-test-stc-")
    client = _client(user)
    created = client.post("/upload/session", json={"filename": FILE_NAME, "mime_type": "video/mp4",
                                                   "total_bytes": 4096})
    assert created.status_code == 201, created.data
    session_id = created.get_json()["session_id"]
    assert _text(session_id) == (None, FILE_NAME)
    assert client.delete(f"/upload/session/{session_id}").status_code == 204
    assert _text(session_id) == (None, None)
    assert db.session.get(RecordingSession, session_id).status == "aborted"


def test_expiry_clears_the_text(users):
    from src.api.recording_sessions import cleanup_expired_sessions
    user = users()
    app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp(prefix="speakr-test-stc-")
    old = datetime.utcnow() - timedelta(hours=48)
    upload = _session(user, "recording", kind="sliced_upload", last_seen_at=old).id
    stalled = _session(user, "finalizing", last_seen_at=old).id        # finalizing without a recording
    fresh = _session(user, "recording", kind="sliced_upload").id
    cleanup_expired_sessions(app=app)
    assert _text(upload) == (None, None) and _text(stalled) == (None, None)
    assert db.session.get(RecordingSession, upload).status == "expired"
    assert db.session.get(RecordingSession, stalled).status == "expired"
    assert _text(fresh) == (None, FILE_NAME)


def test_a_failed_recorder_session_keeps_its_metadata_for_the_retry(users):
    """The stitch job is retried, and each run reads the metadata."""
    from types import SimpleNamespace
    from src.services.job_queue import job_queue
    from src.services.recording_stitch import StitchError
    user = users()
    rec = _recording(user, "STITCHING")
    session_id = _session(user, "finalizing", rec.id).id
    with patch("src.services.recording_stitch.stitch_recording_session", side_effect=StitchError("no chunks")), \
            pytest.raises(StitchError):
        job_queue._run_stitch(SimpleNamespace(id=1), rec, {"session_id": session_id})
    assert db.session.get(RecordingSession, session_id).status == "failed"
    assert _text(session_id) == (TYPED, None)


# --- the one-time cleanup (0010) ---------------------------------------------------

def _matrix(user):
    """session id -> whether the cleanup clears it."""
    rec = _recording(user)
    gone = 30_000_000 + int(uuid.uuid4().int % 1000)
    rows = {
        _session(user, "finalized", rec.id).id: True,
        _session(user, "finalized", rec.id, kind="sliced_upload").id: True,
        _session(user, "finalized").id: True,                               # its recording was deleted
        _session(user, "aborted", kind="sliced_upload").id: True,
        _session(user, "expired").id: True,
        _session(user, "expired", kind="sliced_upload").id: True,
        _session(user, "failed", kind="sliced_upload").id: True,
        _session(user, "failed").id: True,                                  # failed, recording deleted
        _session(user, "failed", rec.id).id: False,                         # the retry reads it
        _session(user, "recording").id: False,
        _session(user, "recording", kind="sliced_upload").id: False,
        _session(user, "finalizing", rec.id).id: False,
        _session(user, "finalizing", kind="sliced_upload").id: False,
    }
    if db.engine.name == "sqlite":
        # A link to a recording that no longer exists (SQLite before #505).
        stale = _session(user, "failed").id
        db.session.execute(text("UPDATE recording_session SET finalized_recording_id = :r WHERE id = :s"),
                           {"r": gone, "s": stale})
        db.session.commit()
        rows[stale] = True
    return rows, rec


def _snapshot(user):
    db.session.expire_all()
    return {row.id: (row.status, row.kind, row.mime_type, row.chunk_count, row.bytes_received,
                     row.upload_total_bytes, row.finalized_recording_id, row.error_message,
                     row.created_at, row.last_seen_at, row.finalized_at)
            for row in RecordingSession.query.filter_by(user_id=user.id)}


def test_cleanup_clears_ended_sessions_and_nothing_else(users):
    from src.init_db import _clear_ended_session_text
    user = users()
    rows, rec = _matrix(user)
    before = _snapshot(user)
    for _ in range(2):                       # a second run changes nothing
        _clear_ended_session_text(db.engine, app)
        for session_id, cleared in rows.items():
            row_text = _text(session_id)
            if cleared:
                assert row_text == (None, None), db.session.get(RecordingSession, session_id)
            else:
                assert "SENTINEL" in str(row_text), db.session.get(RecordingSession, session_id)
        # Every other column of every row, and the recording, are as they were.
        assert _snapshot(user) == before
        assert db.session.get(Recording, rec.id).title == "r"


def test_cleanup_is_registered_once_in_the_ledger():
    source = (ROOT / "src" / "init_db.py").read_text(encoding="utf-8")
    assert "run_once(engine, '0010_clear_ended_session_text'" in source
    assert source.count("'0010_") == 1
    with app.app_context():
        assert db.session.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = "
            "'0010_clear_ended_session_text'")).scalar() == 1


def test_run_once_applies_the_cleanup_a_single_time(users):
    from src.init_db import _clear_ended_session_text
    from src.utils.database import run_once
    user = users()
    first = _session(user, "finalized").id
    migration_id = f"test_{uuid.uuid4().hex[:12]}_clear_ended_session_text"
    try:
        assert run_once(db.engine, migration_id, lambda eng: _clear_ended_session_text(eng, app)) is True
        assert _text(first) == (None, None)
        later = _session(user, "finalized").id
        assert run_once(db.engine, migration_id, lambda eng: _clear_ended_session_text(eng, app)) is False
        assert _text(later) == (TYPED, None)
    finally:
        db.session.execute(text("DELETE FROM schema_migrations WHERE migration_id = :m"), {"m": migration_id})
        db.session.commit()


sqlite_only = pytest.mark.skipif(
    not os.environ.get("SQLALCHEMY_DATABASE_URI", "").startswith("sqlite"),
    reason="boots three processes on a database file of its own")


@sqlite_only
def test_three_concurrent_boots_clear_the_text_once(tmp_path):
    """Three processes run initialize_database() at the same time on a
    database that holds ended sessions and no ledger entry for 0010."""
    database = tmp_path / "boot.db"
    env = dict(os.environ, SQLALCHEMY_DATABASE_URI=f"sqlite:///{database}",
               UPLOAD_FOLDER=str(tmp_path / "uploads"), SPEAKR_SKIP_BACKGROUND_TASKS="true",
               PYTHONPATH=str(ROOT))
    env.pop("PYTEST_DATABASE_URI", None)
    seed = textwrap.dedent('''
        from src.app import app, db
        from src.init_db import initialize_database
        from src.models import User, Recording, RecordingSession
        from sqlalchemy import text
        with app.app_context():
            initialize_database(app)
            a = User(username='own', email='own@local.test', password='x')
            db.session.add(a); db.session.commit()
            r = Recording(user_id=a.id, title='t', status='COMPLETED'); db.session.add(r); db.session.commit()
            for i in range(150):
                db.session.add(RecordingSession(
                    user_id=a.id, status='finalized' if i % 3 else 'recording', chunk_count=i,
                    finalized_recording_id=r.id if i % 2 else None,
                    kind='sliced_upload' if i % 5 == 0 else 'recorder',
                    finalize_metadata='{"title": "SENTINEL"}', upload_filename='SENTINEL.mp4'))
            db.session.commit()
            db.session.execute(text("DELETE FROM schema_migrations WHERE migration_id LIKE '0010_%'"))
            db.session.commit()
    ''')
    boot = textwrap.dedent('''
        from src.app import app
        from src.init_db import initialize_database
        with app.app_context():
            initialize_database(app)
    ''')
    done = subprocess.run([sys.executable, "-c", seed], cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=100)
    assert done.returncode == 0, done.stderr[-3000:]
    workers = [subprocess.Popen([sys.executable, "-c", boot], cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for worker in workers:
        _out, err = worker.communicate(timeout=100)
        assert worker.returncode == 0, err[-3000:]
    import sqlite3
    conn = sqlite3.connect(str(database))
    try:
        count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        assert count("SELECT COUNT(*) FROM schema_migrations WHERE migration_id LIKE '0010_%'") == 1
        assert count("SELECT COUNT(*) FROM recording_session") == 150
        assert count("SELECT COUNT(*) FROM recording_session WHERE status = 'finalized' AND "
                     "(finalize_metadata IS NOT NULL OR upload_filename IS NOT NULL)") == 0
        # Sessions in progress keep both values.
        assert count("SELECT COUNT(*) FROM recording_session WHERE status = 'recording' AND "
                     "finalize_metadata IS NOT NULL AND upload_filename IS NOT NULL") == 50
        assert count("SELECT COUNT(*) FROM recording_session WHERE finalized_recording_id IS NOT NULL") == 75
        assert count("SELECT SUM(chunk_count) FROM recording_session") == sum(range(150))
    finally:
        conn.close()
