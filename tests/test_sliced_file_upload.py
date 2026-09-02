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


def _open_session(client, filename="talk.mp4", mime_type="video/mp4"):
    response = client.post("/upload/session", json={"filename": filename, "mime_type": mime_type})
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

        body = _open_session(client)
        assert body["upload_filename"] == "talk.mp4"
        assert db.session.get(RecordingSession, body["session_id"]).upload_filename == "talk.mp4"

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
        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
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

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
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


def test_finalize_upload_rejects_a_slice_count_that_disagrees_with_disk():
    """A slice lost between the write and the bookkeeping commit would
    otherwise be silently ingested as a truncated file."""
    upload_folder = _tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _mk_user("slice_short")
        client = app.test_client()
        _login(client, user)

        session_id = _open_session(client, filename="sample.mp3", mime_type="audio/mpeg")["session_id"]
        _send_slices(client, session_id, _payload(SLICE_BYTES * 2))
        os.remove(os.path.join(upload_folder, "_sessions", session_id, "chunk-000002.bin"))

        response = client.post(
            f"/upload/session/{session_id}/finalize-upload",
            data={},
            content_type="multipart/form-data",
        )
        assert response.status_code == 409
        assert response.get_json()["expected_chunk_index"] == 2

        client.delete(f"/upload/session/{session_id}")
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


def test_a_replayed_finalize_upload_does_not_ingest_the_file_twice():
    """Finalize is synchronous, so a client that retries it after a lost
    response would otherwise land the same file in the library twice."""
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
        assert second.status_code == 409
        assert Recording.query.filter_by(user_id=user.id).count() == 1

        _cleanup(db.session.get(Recording, first.get_json()["id"]), user)
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
