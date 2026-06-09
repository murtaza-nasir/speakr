"""End-to-end tests for the recording-session API (#287 c/d).

Exercises:

- POST /upload/session             create
- POST /upload/session/<id>/chunks/<n>  upload (in-order, out-of-order, oversize)
- GET  /upload/session/<id>        status
- POST /upload/session/<id>/finalize  enqueue stitch (mocked)
- DELETE /upload/session/<id>      abort + dir cleanup
- ownership boundary (one user cannot touch another's session)
- cleanup_expired_sessions reaps stale sessions and removes dirs

The ffmpeg concat path is exercised indirectly: the finalize test mocks
the stitch job_queue.enqueue so we don't need ffmpeg in the test env; a
companion test of the stitch module itself goes in test_recording_stitch.
"""

import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, RecordingSession


app.config["WTF_CSRF_ENABLED"] = False


def _setup_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    """Swap the test_client session to a given user.

    Tests that switch users between requests need to invalidate
    Flask-Login's per-request cache on ``g``, which leaks across
    test_client calls when the outer test wraps in ``with
    app.app_context()`` (the cache is bound to the app context, not the
    request context). Pop ``_login_user`` so the next request re-runs
    the user_loader against the freshly-written session.
    """
    from flask import g
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    try:
        g.pop("_login_user", None)
    except RuntimeError:
        # Outside of an app context; not in a switching scenario.
        pass


def _make_tmp_upload_folder():
    """Spin up a unique upload folder so tests don't trample one another."""
    tmp = tempfile.mkdtemp(prefix="speakr-test-sessions-")
    return tmp


def test_create_session_returns_session_id_and_makes_dir():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_create")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/upload/session", json={"mime_type": "audio/webm"})
        assert resp.status_code == 201, resp.data
        body = resp.get_json()
        assert "session_id" in body
        assert body["mime_type"] == "audio/webm"
        assert body["status"] == "recording"
        assert "expires_at" in body
        assert body["max_chunk_bytes"] > 0

        # Dir exists on disk
        session_dir = os.path.join(upload_folder, "_sessions", body["session_id"])
        assert os.path.isdir(session_dir)
        # Manifest exists
        assert os.path.exists(os.path.join(session_dir, "session.json"))

        db.session.delete(db.session.get(RecordingSession, body["session_id"]))
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_create_session_rejects_unsupported_mime_type():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_bad_mime")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/upload/session", json={"mime_type": "video/h264"})
        assert resp.status_code == 400
        assert "allowed" in resp.get_json()
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_chunk_upload_in_order_succeeds_and_advances_counter():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_chunks")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/upload/session", json={"mime_type": "audio/webm"})
        sid = resp.get_json()["session_id"]

        # Three chunks, in order
        for i in range(1, 4):
            r = client.post(
                f"/upload/session/{sid}/chunks/{i}",
                data=b"x" * 64,
                content_type="application/octet-stream",
            )
            assert r.status_code == 204, (i, r.data)

        # Status reflects the counters
        r = client.get(f"/upload/session/{sid}")
        body = r.get_json()
        assert body["chunk_count"] == 3
        assert body["bytes_received"] == 64 * 3
        # Disk has 3 chunks
        sess_dir = os.path.join(upload_folder, "_sessions", sid)
        chunk_files = sorted(f for f in os.listdir(sess_dir) if f.startswith("chunk-"))
        assert chunk_files == ["chunk-000001.bin", "chunk-000002.bin", "chunk-000003.bin"]

        client.delete(f"/upload/session/{sid}")
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_chunk_upload_out_of_order_returns_409_with_expected_index():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_oo")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/upload/session", json={"mime_type": "audio/webm"})
        sid = resp.get_json()["session_id"]
        # Skip 1, send 2 first
        r = client.post(f"/upload/session/{sid}/chunks/2", data=b"y" * 10, content_type="application/octet-stream")
        assert r.status_code == 409
        body = r.get_json()
        assert body["expected_chunk_index"] == 1
        assert body["got"] == 2
        client.delete(f"/upload/session/{sid}")
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_chunk_upload_rejects_empty_body():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_empty")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/upload/session", json={"mime_type": "audio/webm"})
        sid = resp.get_json()["session_id"]
        r = client.post(f"/upload/session/{sid}/chunks/1", data=b"", content_type="application/octet-stream")
        assert r.status_code == 400
        client.delete(f"/upload/session/{sid}")
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_creates_recording_and_enqueues_stitch_job():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_fin")
        client = app.test_client()
        _login(client, user)
        sid = client.post("/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        client.post(f"/upload/session/{sid}/chunks/1", data=b"\x00" * 100, content_type="application/octet-stream")

        captured = {}

        def fake_enqueue(*args, **kwargs):
            captured.update(kwargs)
            return 999

        with patch("src.services.job_queue.job_queue.enqueue", side_effect=fake_enqueue):
            r = client.post(f"/upload/session/{sid}/finalize", json={"title": "Soak test", "notes": "n"})
            assert r.status_code == 202, r.data
            body = r.get_json()
            assert "recording_id" in body
            assert body["status"] == "finalizing"

        # Recording row exists in STITCHING status
        rec = db.session.get(Recording, body["recording_id"])
        assert rec is not None
        assert rec.status == "STITCHING"
        assert rec.title == "Soak test"
        assert rec.user_id == user.id
        # Stitch enqueue was called with the session id
        assert captured.get("job_type") == "stitch"
        assert captured["params"]["session_id"] == sid
        assert captured.get("is_new_upload") is True

        # Cleanup
        sess = db.session.get(RecordingSession, sid)
        sess.status = 'aborted'  # avoid trying to actually stitch
        db.session.commit()
        client.delete(f"/upload/session/{sid}")
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_rejects_finalizing_into_other_users_personal_folder():
    """IDOR regression. Without folder-access validation in finalize,
    user A could submit a personal folder_id belonging to user B and the
    new recording would be linked to B's folder. Mirrors the validation
    in update_recording (src/api/api_v1.py). Reported by the background
    security review on 2026-06-05."""
    from src.models.organization import Folder

    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        victim = _setup_user("sess_idor_victim")
        attacker = _setup_user("sess_idor_attacker")
        victim_folder = Folder(name="victim-private", user_id=victim.id)
        db.session.add(victim_folder)
        db.session.commit()

        client = app.test_client()
        _login(client, attacker)
        sid = client.post("/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        client.post(
            f"/upload/session/{sid}/chunks/1",
            data=b"\x00" * 100,
            content_type="application/octet-stream",
        )

        r = client.post(
            f"/upload/session/{sid}/finalize",
            json={"title": "exploit", "folder_id": victim_folder.id},
        )
        assert r.status_code == 403, (
            f"Cross-tenant folder finalize must be rejected; got "
            f"{r.status_code} body={r.data!r}"
        )

        # The victim's folder must still belong to the victim; no
        # recording from the attacker may exist with folder_id set.
        from sqlalchemy import select
        attacker_recs = db.session.execute(
            select(Recording).where(
                Recording.user_id == attacker.id,
                Recording.folder_id == victim_folder.id,
            )
        ).scalars().all()
        assert not attacker_recs

        # Cleanup
        sess = db.session.get(RecordingSession, sid)
        if sess:
            sess.status = "aborted"
            db.session.commit()
            client.delete(f"/upload/session/{sid}")
        db.session.delete(victim_folder)
        db.session.delete(victim)
        db.session.delete(attacker)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_returns_404_for_nonexistent_folder():
    """The folder-access check uses 404 vs 403 to distinguish missing
    from forbidden, matching update_recording's behaviour. A made-up
    folder_id should 404, not silently succeed."""
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_bad_folder")
        client = app.test_client()
        _login(client, user)
        sid = client.post("/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        client.post(
            f"/upload/session/{sid}/chunks/1",
            data=b"\x00" * 100,
            content_type="application/octet-stream",
        )

        r = client.post(
            f"/upload/session/{sid}/finalize",
            json={"folder_id": 999_999_999},
        )
        assert r.status_code == 404

        sess = db.session.get(RecordingSession, sid)
        if sess:
            sess.status = "aborted"
            db.session.commit()
            client.delete(f"/upload/session/{sid}")
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_finalize_rejects_when_no_chunks():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_empty_fin")
        client = app.test_client()
        _login(client, user)
        sid = client.post("/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        r = client.post(f"/upload/session/{sid}/finalize", json={})
        assert r.status_code == 409
        client.delete(f"/upload/session/{sid}")
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_abort_removes_session_dir():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_abort")
        client = app.test_client()
        _login(client, user)
        sid = client.post("/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        client.post(f"/upload/session/{sid}/chunks/1", data=b"z" * 50, content_type="application/octet-stream")
        sess_dir = os.path.join(upload_folder, "_sessions", sid)
        assert os.path.isdir(sess_dir)

        r = client.delete(f"/upload/session/{sid}")
        assert r.status_code == 204
        assert not os.path.isdir(sess_dir)

        sess = db.session.get(RecordingSession, sid)
        assert sess.status == "aborted"
        db.session.delete(sess)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_ownership_enforced_other_user_gets_404():
    upload_folder = _make_tmp_upload_folder()
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user_a = _setup_user("sess_owner_a")
        user_b = _setup_user("sess_owner_b")
        client = app.test_client()
        _login(client, user_a)
        sid = client.post("/upload/session", json={"mime_type": "audio/webm"}).get_json()["session_id"]
        # Switch login to user B
        _login(client, user_b)
        # B should not be able to GET / POST chunks / finalize / abort A's session
        assert client.get(f"/upload/session/{sid}").status_code == 404
        assert client.post(f"/upload/session/{sid}/chunks/1", data=b"x", content_type="application/octet-stream").status_code == 404
        assert client.post(f"/upload/session/{sid}/finalize", json={}).status_code == 404
        assert client.delete(f"/upload/session/{sid}").status_code == 404
        # A can still operate
        _login(client, user_a)
        assert client.delete(f"/upload/session/{sid}").status_code == 204
        db.session.delete(user_a)
        db.session.delete(user_b)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_cleanup_auto_finalizes_with_chunks_expires_empty_and_rekicks_stalled():
    """Cleanup policy (#287 unification):
      - stale 'recording' WITH chunks  → auto-finalize (don't lose audio)
      - stale 'recording' WITHOUT chunks → expire + delete dir
      - stale 'finalizing' (has a recording) → re-enqueue stitch to unstick
      - fresh session → untouched

    job_queue.enqueue is patched so the async stitch worker never starts;
    we assert on the synchronous decisions cleanup makes.
    """
    upload_folder = _make_tmp_upload_folder()
    from src.api.recording_sessions import cleanup_expired_sessions
    from src.services.job_queue import job_queue
    old = datetime.utcnow() - timedelta(hours=48)

    enqueued = []

    def _fake_enqueue(**kw):
        enqueued.append(kw)
        return 999

    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _setup_user("sess_expire")

        # stale recording WITH chunks → auto-finalize
        with_chunks = RecordingSession(user_id=user.id, mime_type="audio/webm",
                                       status="recording", chunk_count=3, bytes_received=512)
        db.session.add(with_chunks)
        db.session.flush()
        with_chunks.last_seen_at = old

        # stale recording WITHOUT chunks → expire
        empty = RecordingSession(user_id=user.id, mime_type="audio/webm",
                                 status="recording", chunk_count=0, bytes_received=0)
        db.session.add(empty)
        db.session.flush()
        empty.last_seen_at = old

        # stale finalizing (already has a placeholder recording) → re-enqueue
        stalled_rec = Recording(user_id=user.id, title="stalled", status="STITCHING",
                                processing_source="recording_session", mime_type="audio/webm")
        db.session.add(stalled_rec)
        db.session.flush()
        stalled = RecordingSession(user_id=user.id, mime_type="audio/webm",
                                   status="finalizing", chunk_count=2, bytes_received=256,
                                   finalized_recording_id=stalled_rec.id)
        db.session.add(stalled)
        db.session.flush()
        stalled.last_seen_at = old

        # fresh recording → untouched
        fresh = RecordingSession(user_id=user.id, mime_type="audio/webm",
                                 status="recording", chunk_count=1, bytes_received=128)
        db.session.add(fresh)
        db.session.flush()

        for s in (with_chunks, empty, stalled, fresh):
            os.makedirs(os.path.join(upload_folder, "_sessions", s.id), exist_ok=True)
        db.session.commit()
        with_chunks_dir = os.path.join(upload_folder, "_sessions", with_chunks.id)
        empty_dir = os.path.join(upload_folder, "_sessions", empty.id)

        with patch.object(job_queue, "enqueue", _fake_enqueue):
            reaped = cleanup_expired_sessions(app=app)

        # cleanup committed on its own nested-app-context session; drop the
        # outer session's identity-map cache so our reads hit the DB.
        db.session.expire_all()

        assert reaped == 3  # auto-finalize + expire + re-enqueue (fresh untouched)

        # WITH chunks → finalizing, recording created, dir kept (stitch owns it)
        wc = db.session.get(RecordingSession, with_chunks.id)
        assert wc.status == "finalizing"
        assert wc.finalized_recording_id is not None
        assert os.path.isdir(with_chunks_dir)
        assert any(c.get("job_type") == "stitch" and c.get("recording_id") == wc.finalized_recording_id
                   for c in enqueued)

        # EMPTY → expired, dir removed
        em = db.session.get(RecordingSession, empty.id)
        assert em.status == "expired"
        assert not os.path.isdir(empty_dir)

        # STALLED finalizing → stitch re-enqueued for its recording, last_seen bumped
        st = db.session.get(RecordingSession, stalled.id)
        assert st.status == "finalizing"
        assert st.last_seen_at > old
        assert any(c.get("job_type") == "stitch" and c.get("recording_id") == stalled_rec.id
                   for c in enqueued)

        # FRESH → untouched
        assert db.session.get(RecordingSession, fresh.id).status == "recording"

        # Cleanup (including the placeholder Recording auto-finalize created)
        for s in RecordingSession.query.filter_by(user_id=user.id).all():
            db.session.delete(s)
        for r in Recording.query.filter_by(user_id=user.id).all():
            db.session.delete(r)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


_ORIGINAL_UPLOAD_FOLDER = app.config.get("UPLOAD_FOLDER")


def setup_function(function):  # noqa: D401 - pytest hook
    """Reset UPLOAD_FOLDER before each test so prior mutations do not leak."""
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER


def teardown_function(function):
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER


def teardown_module(module):
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER
    with app.app_context():
        for u in User.query.filter(User.username.like("sess_%")).all():
            for s in RecordingSession.query.filter_by(user_id=u.id).all():
                db.session.delete(s)
            db.session.delete(u)
        db.session.commit()


if __name__ == "__main__":
    test_create_session_returns_session_id_and_makes_dir()
    test_create_session_rejects_unsupported_mime_type()
    test_chunk_upload_in_order_succeeds_and_advances_counter()
    test_chunk_upload_out_of_order_returns_409_with_expected_index()
    test_chunk_upload_rejects_empty_body()
    test_finalize_creates_recording_and_enqueues_stitch_job()
    test_finalize_rejects_when_no_chunks()
    test_abort_removes_session_dir()
    test_ownership_enforced_other_user_gets_404()
    test_cleanup_auto_finalizes_with_chunks_expires_empty_and_rekicks_stalled()
    print("All recording-session tests passed.")
