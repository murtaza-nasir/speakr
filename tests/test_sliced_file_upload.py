"""End-to-end tests for sliced uploads of files already on disk.

A sliced upload reuses the recording-session endpoints to carry a file in
fixed-size byte slices past a reverse proxy whose body limit is below the
file size, then finalizes through /finalize-upload, which byte-joins the
slices and runs them through the same ingestion pipeline POST /upload
uses.

The external effects of that pipeline (codec probe, conversion, storage,
job queue) are mocked with the harness the /upload tests already use, so
these tests assert the parts that are specific to slicing: the session is
recognisable as a file upload, the reassembled bytes are the original
bytes, the recorder's stitch path is kept away from them, and an
abandoned one is expired rather than assembled into a truncated
recording.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, RecordingSession
from src.api.recording_sessions import cleanup_expired_sessions

from tests.test_cov_recordings_write import _cleanup, _mk_user, _upload_mocks
from tests.test_recording_sessions import _login

app.config["WTF_CSRF_ENABLED"] = False

SLICE_BYTES = 4096


def _tmp_upload_folder():
    return tempfile.mkdtemp(prefix="speakr-test-sliced-")


def _payload(size):
    return bytes(range(256)) * (size // 256) + b"\xab" * (size % 256)


def _open_session(client, filename="talk.mp4", mime_type="video/mp4", total_bytes=SLICE_BYTES):
    response = client.post("/upload/session", json={
        "filename": filename,
        "mime_type": mime_type,
        "total_bytes": total_bytes,
    })
    assert response.status_code == 201, response.data
    return response.get_json()


def _send_slices(client, session_id, payload, slice_bytes=SLICE_BYTES):
    for index, start in enumerate(range(0, len(payload), slice_bytes), start=1):
        response = client.post(
            f"/upload/session/{session_id}/chunks/{index}",
            data=payload[start:start + slice_bytes],
            content_type="application/octet-stream",
        )
        assert response.status_code == 204, (index, response.data)


def test_create_session_with_filename_marks_it_a_file_upload():
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_create")
        client = app.test_client()
        _login(client, user)

        body = _open_session(client, total_bytes=4096)
        assert body["kind"] == "sliced_upload"
        assert body["upload_filename"] == "talk.mp4"
        session = db.session.get(RecordingSession, body["session_id"])
        assert session.is_sliced_upload
        assert session.upload_filename == "talk.mp4"
        assert session.upload_total_bytes == 4096

        client.delete(f"/upload/session/{body['session_id']}")
        _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_create_session_with_filename_skips_the_recorder_mime_whitelist():
    """A user's own file can be any container ffmpeg reads, so the whitelist
    that guards the recorder's stitch path must not gate these sessions."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_mime")
        client = app.test_client()
        _login(client, user)

        rejected = client.post("/upload/session", json={"mime_type": "video/x-matroska"})
        assert rejected.status_code == 400

        accepted = _open_session(client, filename="lecture.mkv", mime_type="video/x-matroska")
        assert accepted["mime_type"] == "video/x-matroska"

        no_size = client.post("/upload/session", json={"filename": "lecture.mkv"})
        assert no_size.status_code == 400

        client.delete(f"/upload/session/{accepted['session_id']}")
        _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_upload_ingests_the_reassembled_original_bytes():
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_ingest")
        client = app.test_client()
        _login(client, user)

        payload = _payload(SLICE_BYTES * 3 + 17)
        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg",
                                   total_bytes=len(payload))["session_id"]
        _send_slices(client, session_id, payload)

        staging = os.path.join(upload_folder, f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            response = client.post(
                f"/upload/session/{session_id}/finalize-upload",
                data={"title": "Sliced", "notes": "n"},
                content_type="multipart/form-data",
            )

        assert response.status_code == 202, response.data
        recording = db.session.get(Recording, response.get_json()["id"])
        assert recording.title == "Sliced"
        assert recording.original_filename == "sample.mp3"
        assert recording.file_hash == hashlib.sha256(payload).hexdigest()
        assert enqueue.called

        session = db.session.get(RecordingSession, session_id)
        assert session.status == "finalized"
        assert session.finalized_recording_id == recording.id
        assert not os.path.isdir(os.path.join(upload_folder, "_sessions", session_id))

        _cleanup(recording, user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_upload_carries_the_upload_form_through_ingestion():
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_form")
        client = app.test_client()
        _login(client, user)

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg",
                                   total_bytes=SLICE_BYTES * 2)["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES * 2))

        staging = os.path.join(upload_folder, f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            response = client.post(
                f"/upload/session/{session_id}/finalize-upload",
                data={"language": "fr", "min_speakers": "2", "max_speakers": "4"},
                content_type="multipart/form-data",
            )

        assert response.status_code == 202, response.data
        params = enqueue.call_args.kwargs["params"]
        assert params["language"] == "fr"
        assert params["min_speakers"] == 2
        assert params["max_speakers"] == 4

        _cleanup(db.session.get(Recording, response.get_json()["id"]), user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_upload_rejects_slices_that_do_not_weigh_the_declared_size():
    """A missing or short slice would otherwise be ingested as a truncated
    file, which ffprobe accepts for most containers, so the user would get
    a silently broken recording instead of an error."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        client = app.test_client()

        for label, damage in (
            ("missing", lambda path: os.remove(path)),
            ("truncated", lambda path: open(path, "wb").write(b"short")),
        ):
            user = _mk_user(f"slice_{label}")
            _login(client, user)
            session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg",
                                       total_bytes=SLICE_BYTES * 2)["session_id"]
            _send_slices(client, session_id, _payload(SLICE_BYTES * 2))
            damage(os.path.join(upload_folder, "_sessions", session_id, "chunk-000002.bin"))

            response = client.post(
                f"/upload/session/{session_id}/finalize-upload",
                data={},
                content_type="multipart/form-data",
            )
            assert response.status_code == 409, (label, response.data)
            assert response.get_json()["expected_bytes"] == SLICE_BYTES * 2
            assert Recording.query.filter_by(user_id=user.id).count() == 0
            _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_a_failed_finalize_upload_reclaims_the_slices_it_cannot_use():
    """Finalize only accepts a session in 'recording' and no sweep collects
    a terminal one, so slices left behind here leak the whole file until an
    operator notices, the common cause being the disk that just filled."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_reclaim")
        client = app.test_client()
        _login(client, user)

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES))

        with patch("src.api.recording_sessions.byte_join",
                   side_effect=OSError("No space left on device")):
            response = client.post(
                f"/upload/session/{session_id}/finalize-upload",
                data={}, content_type="multipart/form-data")

        assert response.status_code == 500
        assert db.session.get(RecordingSession, session_id).status == "failed"
        assert not os.path.isdir(os.path.join(upload_folder, "_sessions", session_id))

        _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_a_finalize_upload_cannot_be_aborted_while_it_is_ingesting():
    """The slices are being read in-request, so honouring the abort would
    delete the ingesting file out from under the request."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_abort")
        client = app.test_client()
        _login(client, user)

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES))
        session = db.session.get(RecordingSession, session_id)
        session.status = "finalizing"
        db.session.commit()

        response = client.delete(f"/upload/session/{session_id}")
        assert response.status_code == 409
        assert os.path.isdir(os.path.join(upload_folder, "_sessions", session_id))

        session.status = "aborted"
        db.session.commit()
        _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_the_two_finalize_routes_reject_each_others_sessions():
    """The recorder's finalize remuxes and sniffs container headers per
    chunk, which would rewrite or mis-split a byte-sliced file; the sliced
    finalize skips the stitch a paused recording needs."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_routes")
        client = app.test_client()
        _login(client, user)

        file_session = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, file_session, _payload(SLICE_BYTES))
        stitch_refusal = client.post(f"/upload/session/{file_session}/finalize", json={})
        assert stitch_refusal.status_code == 409
        assert "finalize-upload" in stitch_refusal.get_json()["error"]

        recorder_session = client.post(
            "/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        client.post(f"/upload/session/{recorder_session}/chunks/1",
                    data=b"\x00" * 64, content_type="application/octet-stream")
        upload_refusal = client.post(
            f"/upload/session/{recorder_session}/finalize-upload",
            data={}, content_type="multipart/form-data")
        assert upload_refusal.status_code == 409
        assert "/finalize" in upload_refusal.get_json()["error"]

        client.delete(f"/upload/session/{file_session}")
        client.delete(f"/upload/session/{recorder_session}")
        _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_cleanup_expires_an_abandoned_sliced_upload_instead_of_assembling_it():
    """Abandoned recorder sessions are auto-finalized because the browser
    holds the only copy of that audio. A sliced upload's original is still
    on the user's disk, and its slices are a truncated container, so
    assembling one would land a broken recording in the library."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_expire")
        client = app.test_client()
        _login(client, user)

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES))

        session = db.session.get(RecordingSession, session_id)
        session.last_seen_at = datetime.utcnow() - timedelta(hours=48)
        db.session.commit()

        cleanup_expired_sessions(app)

        db.session.expire_all()
        assert db.session.get(RecordingSession, session_id).status == "expired"
        assert not os.path.isdir(os.path.join(upload_folder, "_sessions", session_id))
        assert Recording.query.filter_by(user_id=user.id).count() == 0

        _cleanup(user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_a_replayed_finalize_upload_returns_the_first_call_s_recording():
    """Finalize is synchronous, so a client that retries it after a lost
    response would otherwise be told 409 and re-upload the whole file it
    had already delivered."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_replay")
        client = app.test_client()
        _login(client, user)

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES))

        staging = os.path.join(upload_folder, f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            first = client.post(f"/upload/session/{session_id}/finalize-upload",
                                data={}, content_type="multipart/form-data")
            second = client.post(f"/upload/session/{session_id}/finalize-upload",
                                 data={}, content_type="multipart/form-data")

        assert first.status_code == 202, first.data
        assert second.status_code == 202, second.data
        assert second.get_json()["id"] == first.get_json()["id"]
        assert second.get_json()["idempotent_replay"] is True
        assert Recording.query.filter_by(user_id=user.id).count() == 1

        _cleanup(db.session.get(Recording, first.get_json()["id"]), user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_an_interrupted_sliced_upload_resumes_from_the_slices_already_sent():
    """What the client needs to resume: the status endpoint has to say the
    session is a sliced upload of this exact file and how many slices it
    already holds, and the next slice has to pick up from there."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_resume")
        client = app.test_client()
        _login(client, user)

        payload = _payload(SLICE_BYTES * 3)
        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg",
                                   total_bytes=len(payload))["session_id"]
        _send_slices(client, session_id, payload[:SLICE_BYTES])

        status = client.get(f"/upload/session/{session_id}").get_json()
        assert status["kind"] == "sliced_upload"
        assert status["upload_total_bytes"] == len(payload)
        assert status["status"] == "recording"
        assert status["chunk_count"] == 1

        for index in (2, 3):
            start = (index - 1) * SLICE_BYTES
            resumed = client.post(
                f"/upload/session/{session_id}/chunks/{index}",
                data=payload[start:start + SLICE_BYTES],
                content_type="application/octet-stream",
            )
            assert resumed.status_code == 204, (index, resumed.data)

        staging = os.path.join(upload_folder, f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            response = client.post(f"/upload/session/{session_id}/finalize-upload",
                                   data={}, content_type="multipart/form-data")

        assert response.status_code == 202, response.data
        recording = db.session.get(Recording, response.get_json()["id"])
        assert recording.file_hash == hashlib.sha256(payload).hexdigest()

        _cleanup(recording, user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_a_finalize_abandoned_by_a_dead_worker_can_be_taken_over():
    """Recreating the container mid-ingest leaves the session finalizing
    with nobody ingesting. Without a takeover the user's retry waits for
    a worker that no longer exists until the TTL sweep, a day later."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_takeover")
        client = app.test_client()
        _login(client, user)

        payload = _payload(SLICE_BYTES)
        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg",
                                   total_bytes=len(payload))["session_id"]
        _send_slices(client, session_id, payload)

        session = db.session.get(RecordingSession, session_id)
        session.status = "finalizing"
        session.last_seen_at = datetime.utcnow()
        db.session.commit()

        fresh_claim = client.post(f"/upload/session/{session_id}/finalize-upload",
                                  data={}, content_type="multipart/form-data")
        assert fresh_claim.status_code == 409
        assert "already being finalized" in fresh_claim.get_json()["error"]

        session.last_seen_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()

        staging = os.path.join(upload_folder, f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            taken_over = client.post(f"/upload/session/{session_id}/finalize-upload",
                                     data={}, content_type="multipart/form-data")

        assert taken_over.status_code == 202, taken_over.data
        recording = db.session.get(Recording, taken_over.get_json()["id"])
        assert recording.file_hash == hashlib.sha256(payload).hexdigest()

        _cleanup(recording, user)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_another_user_cannot_finalize_someone_elses_sliced_upload():
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        owner = _mk_user("slice_owner")
        intruder = _mk_user("slice_intruder")
        client = app.test_client()
        _login(client, owner)
        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES))

        other = app.test_client()
        _login(other, intruder)
        response = other.post(
            f"/upload/session/{session_id}/finalize-upload",
            data={}, content_type="multipart/form-data")
        assert response.status_code == 404

        client.delete(f"/upload/session/{session_id}")
        _cleanup(owner, intruder)
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_an_existing_recorder_session_survives_the_migration_as_a_recorder_session():
    """The migration adds the discriminator to a table that already holds
    live recorder sessions. If they came out the other side looking like
    sliced uploads, /finalize would refuse them and the cleanup sweep would
    stop rescuing abandoned recordings, which is the one thing that policy
    exists to prevent."""
    import sqlite3
    from flask import Flask
    from sqlalchemy import inspect
    from src.init_db import initialize_database

    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "schemas", "v0.10.3-alpha.sql")
    db_dir = tempfile.mkdtemp(prefix="speakr-test-upgrade-")
    db_path = os.path.join(db_dir, "old.db")

    con = sqlite3.connect(db_path)
    with open(fixture) as f:
        con.executescript(f.read())
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, "u", "u@local.test", "h")')
    con.execute(
        "INSERT INTO recording_session"
        " (id, user_id, mime_type, status, chunk_count, bytes_received, created_at, last_seen_at)"
        " VALUES ('sess-old', 1, 'audio/webm', 'recording', 7, 4096, '2026-01-01', '2026-01-01')"
    )
    con.commit()
    con.close()

    upgrading = Flask("upgrade_sliced_upload")
    upgrading.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    upgrading.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    upgrading.config["UPLOAD_FOLDER"] = db_dir
    db.init_app(upgrading)
    with upgrading.app_context():
        initialize_database(upgrading)
        columns = {c["name"] for c in inspect(db.engine).get_columns("recording_session")}
        survivor = db.session.get(RecordingSession, "sess-old")

        assert {"kind", "upload_filename", "upload_total_bytes"} <= columns
        assert survivor.chunk_count == 7
        assert survivor.bytes_received == 4096
        assert survivor.kind == "recorder"
        assert survivor.is_sliced_upload is False

    shutil.rmtree(db_dir, ignore_errors=True)
