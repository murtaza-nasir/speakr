"""Per-user storage quotas (#413).

Storage is the size of a user's own recordings whose audio is kept. Uploads
that would go over the quota are refused with 507 and a JSON body; a
recording made in the app is checked when it starts and kept at the end; the
watch folder leaves a file that does not fit where it is and tells the user
once. Admins set the quota, with a default for new accounts.

SHARED-DB: users, tokens, recordings and notifications are removed afterwards.
"""

import io
import os
import secrets
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Notification, Recording, SystemSetting, User
from src.services import storage_quota as sq
from src.utils.token_auth import hash_token
from tests.test_cov_recordings_write import _do_upload, _upload_mocks
from tests.test_recording_sessions import _login

app.config["WTF_CSRF_ENABLED"] = False
MB = 1024 * 1024


@pytest.fixture
def world():
    made = {"users": []}

    def user(quota_mb=None, admin=False):
        s = secrets.token_hex(4)
        u = User(username=f"q_{s}", email=f"q_{s}@local.test", password="x", is_admin=admin,
                 storage_quota_mb=quota_mb)
        db.session.add(u)
        db.session.commit()
        made["users"].append(u.id)
        return u

    def rec(u, size, removed=False):
        r = Recording(user_id=u.id, title="r", status="COMPLETED", original_filename="r.mp3", file_size=size,
                      audio_deleted_at=datetime.utcnow() if removed else None)
        db.session.add(r)
        db.session.commit()
        return r

    with app.app_context():
        made.update(user=user, rec=rec)
        yield made
        db.session.rollback()
        for uid in made["users"]:
            Notification.query.filter_by(user_id=uid).delete()
            APIToken.query.filter_by(user_id=uid).delete()
            Recording.query.filter_by(user_id=uid).delete()
            User.query.filter_by(id=uid).delete()
        db.session.commit()


def _staging():
    return os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")


# ----------------------------------------------------------------- the service

def test_usage_counts_kept_audio_of_own_recordings(world):
    u = world["user"](quota_mb=10)
    world["rec"](u, 3 * MB)
    world["rec"](u, 4 * MB, removed=True)              # audio removed: not counted
    other = world["user"]()
    world["rec"](other, 9 * MB)
    usage = sq.usage(u)
    assert usage["used_bytes"] == 3 * MB and usage["quota_bytes"] == 10 * MB
    assert usage["available_bytes"] == 7 * MB and usage["percentage"] == 30.0
    assert sq.usage(other)["quota_bytes"] is None


def test_check_room(world):
    u = world["user"](quota_mb=10)
    world["rec"](u, 8 * MB)
    sq.check_room(u, 2 * MB)                            # exactly fits
    with pytest.raises(sq.StorageQuotaExceeded) as e:
        sq.check_room(u, 2 * MB + 1)
    assert e.value.to_dict()["available_bytes"] == 2 * MB
    world["rec"](u, 2 * MB)
    with pytest.raises(sq.StorageQuotaExceeded):
        sq.check_room(u)                                # at the quota, nothing new may start
    sq.check_room(world["user"](), 10 ** 12)            # no quota, no limit


# ----------------------------------------------------------------- uploads

def test_an_upload_over_the_quota_is_refused_and_nothing_is_stored(world):
    u = world["user"](quota_mb=1)
    client = app.test_client()
    _login(client, u)
    with _upload_mocks(_staging()) as (storage, enqueue):
        resp = _do_upload(client, payload=b"\x00" * (MB + 1))
    assert resp.status_code == 507
    body = resp.get_json()
    assert body["code"] == "storage_quota_exceeded" and body["quota_bytes"] == MB and body["file_bytes"] == MB + 1
    assert Recording.query.filter_by(user_id=u.id).count() == 0
    assert not storage.uploaded and not enqueue.called


def test_an_upload_that_fits_is_accepted(world):
    u = world["user"](quota_mb=1)
    client = app.test_client()
    _login(client, u)
    with _upload_mocks(_staging()):
        assert _do_upload(client, payload=b"\x00" * 4096).status_code == 202



def _overlapping_uploads(u, size):
    """Two uploads by u, both past the early check before either is stored (#509)."""
    import threading
    import src.api.recordings as rec_module
    barrier = threading.Barrier(2, timeout=20)
    results = []
    with _upload_mocks(_staging()) as (storage, enqueue):
        convert = rec_module.convert_if_needed     # the mock _upload_mocks installed

        def _convert_then_wait(*args, **kwargs):
            result = convert(*args, **kwargs)
            barrier.wait()                          # both requests have passed check_room
            return result

        def _one(name):
            client = app.test_client()
            _login(client, u)
            resp = _do_upload(client, filename=name, payload=os.urandom(size))
            results.append((resp.status_code, resp.get_json()))

        with patch("src.api.recordings.convert_if_needed", side_effect=_convert_then_wait):
            threads = [threading.Thread(target=_one, args=(n,)) for n in ("a.mp3", "b.mp3")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        uploaded, enqueued = len(storage.uploaded), enqueue.call_count
    db.session.expire_all()
    return results, uploaded, enqueued


def test_overlapping_uploads_cannot_both_use_the_same_room(world):
    u = world["user"](quota_mb=1)
    size = MB // 2 + 1
    results, uploaded, enqueued = _overlapping_uploads(u, size)
    assert sorted(code for code, _ in results) == [202, 507]
    refused = next(body for code, body in results if code == 507)
    assert refused["code"] == "storage_quota_exceeded"
    assert refused["used_bytes"] == size and refused["file_bytes"] == size
    assert Recording.query.filter_by(user_id=u.id).count() == 1
    assert sq.usage(u)["used_bytes"] == size
    assert uploaded == 1 and enqueued == 1


def test_overlapping_uploads_that_both_fit_are_both_stored(world):
    u = world["user"](quota_mb=1)
    results, uploaded, _ = _overlapping_uploads(u, MB // 2)
    assert [code for code, _ in results] == [202, 202]
    assert sq.usage(u)["used_bytes"] == MB and uploaded == 2


def test_an_upload_that_fills_the_quota_exactly_is_accepted(world):
    u = world["user"](quota_mb=1)
    client = app.test_client()
    _login(client, u)
    with _upload_mocks(_staging()):
        assert _do_upload(client, payload=b"\x00" * MB).status_code == 202
        resp = _do_upload(client, payload=b"\x00")
    assert resp.status_code == 507 and resp.get_json()["file_bytes"] == 1
    assert Recording.query.filter_by(user_id=u.id).count() == 1


def test_reserve_room_leaves_the_user_unchanged(world):
    u = world["user"](quota_mb=1)
    sq.reserve_room(u, MB)
    db.session.commit()
    db.session.expire_all()
    assert u.storage_quota_mb == 1
    with pytest.raises(sq.StorageQuotaExceeded):
        sq.reserve_room(u, MB + 1)
    db.session.rollback()
    sq.reserve_room(world["user"](), 10 * MB)      # no quota: nothing to hold
    db.session.rollback()

def test_a_recording_made_in_the_app_is_kept_at_the_end(world):
    u = world["user"](quota_mb=1)
    world["rec"](u, MB)                                  # already full
    client = app.test_client()
    _login(client, u)
    with _upload_mocks(_staging()):
        assert _do_upload(client, payload=b"\x00" * 4096, from_recorder="true").status_code == 202


def test_an_api_token_cannot_claim_a_recording_was_made_in_the_app(world):
    u = world["user"](quota_mb=1)
    world["rec"](u, MB)
    plain = f"tok-{secrets.token_urlsafe(12)}"
    db.session.add(APIToken(user_id=u.id, token_hash=hash_token(plain), name="t"))
    db.session.commit()
    with _upload_mocks(_staging()), app.test_client() as c:
        resp = c.post("/api/v1/recordings/upload", headers={"Authorization": f"Bearer {plain}"},
                      data={"file": (io.BytesIO(b"\x00" * 4096), "a.mp3"), "from_recorder": "true"},
                      content_type="multipart/form-data")
    assert resp.status_code == 507 and resp.get_json()["code"] == "storage_quota_exceeded"


def test_a_session_cannot_open_at_the_quota(world):
    u = world["user"](quota_mb=1)
    world["rec"](u, MB)
    client = app.test_client()
    _login(client, u)
    assert client.post("/upload/session", json={"mime_type": "audio/webm"}).status_code == 507
    assert client.post("/upload/session", json={"filename": "a.mp4", "total_bytes": 10}).status_code == 507


def test_a_sliced_upload_must_fit(world):
    u = world["user"](quota_mb=1)
    client = app.test_client()
    _login(client, u)
    assert client.post("/upload/session", json={"filename": "a.mp4", "total_bytes": MB + 1}).status_code == 507
    ok = client.post("/upload/session", json={"filename": "a.mp4", "total_bytes": 100})
    assert ok.status_code == 201
    client.post(f"/upload/session/{ok.get_json()['session_id']}/abort")


def test_a_merge_that_keeps_the_originals_needs_room(world):
    u = world["user"](quota_mb=10)
    a, b = world["rec"](u, 3 * MB), world["rec"](u, 3 * MB)
    client = app.test_client()
    _login(client, u)
    resp = client.post("/api/recordings/merge", json={"recording_ids": [a.id, b.id]})
    assert resp.status_code == 507


# ----------------------------------------------------------------- watch folder

def test_the_watch_folder_leaves_a_file_that_does_not_fit(world):
    from src.file_monitor import FileMonitor
    u = world["user"](quota_mb=1)
    world["rec"](u, MB)
    folder = Path(tempfile.mkdtemp())
    locked = folder / "meeting.mp3.processing"
    locked.write_bytes(b"\x00" * 4096)
    monitor = FileMonitor(str(folder), check_interval=30)
    monitor._process_file(locked, u.id)
    assert (folder / "meeting.mp3").exists() and not locked.exists()
    assert Recording.query.filter_by(user_id=u.id).count() == 1
    notes = Notification.query.filter_by(user_id=u.id, kind=sq.KIND_STORAGE_QUOTA_REACHED).all()
    assert len(notes) == 1 and notes[0].params["filename"] == "meeting.mp3"
    # Room again: the notice is resolved on the next check that fits.
    u.storage_quota_mb = 100
    db.session.commit()
    sq.check_room(u, 4096)
    assert db.session.get(Notification, notes[0].id).resolved_at is not None


# ----------------------------------------------------------------- admin and display

def test_admins_set_and_clear_the_quota(world):
    admin = world["user"](admin=True)
    target = world["user"]()
    client = app.test_client()
    _login(client, admin)
    assert client.put(f"/admin/users/{target.id}", json={"storage_quota_mb": 500}).get_json()["storage_quota_mb"] == 500
    assert client.put(f"/admin/users/{target.id}", json={"storage_quota_mb": -1}).status_code == 400
    assert client.put(f"/admin/users/{target.id}", json={"storage_quota_mb": None}).get_json()["storage_quota_mb"] is None


def test_new_accounts_get_the_default_quota(world):
    admin = world["user"](admin=True)
    client = app.test_client()
    _login(client, admin)
    old = SystemSetting.get_setting("default_storage_quota_mb", 0)
    SystemSetting.set_setting("default_storage_quota_mb", "250", setting_type="integer")
    try:
        s = secrets.token_hex(4)
        made = client.post("/admin/users", json={"username": f"n_{s}", "email": f"n_{s}@local.test", "password": "Passw0rd!x"})
        assert made.status_code == 201, made.data
        world["users"].append(made.get_json()["id"])
        assert made.get_json()["storage_quota_mb"] == 250
        s = secrets.token_hex(4)
        own = client.post("/admin/users", json={"username": f"n_{s}", "email": f"n_{s}@local.test",
                                                "password": "Passw0rd!x", "storage_quota_mb": 40})
        world["users"].append(own.get_json()["id"])
        assert own.get_json()["storage_quota_mb"] == 40
    finally:
        SystemSetting.set_setting("default_storage_quota_mb", str(old or 0), setting_type="integer")


def test_users_see_their_usage(world):
    u = world["user"](quota_mb=10)
    world["rec"](u, 2 * MB)
    client = app.test_client()
    _login(client, u)
    body = client.get("/api/account/storage").get_json()
    assert body["used_bytes"] == 2 * MB and body["quota_bytes"] == 10 * MB
    page = client.get("/account")
    assert page.status_code == 200 and b'id="account-storage"' in page.data
    plain = f"tok-{secrets.token_urlsafe(12)}"
    db.session.add(APIToken(user_id=u.id, token_hash=hash_token(plain), name="t"))
    db.session.commit()
    with app.test_client() as c:
        me = c.get("/api/v1/users/me", headers={"Authorization": f"Bearer {plain}"}).get_json()
    assert me["storage"] == {"used_bytes": 2 * MB, "quota_bytes": 10 * MB, "available_bytes": 8 * MB}


def test_the_header_meter_reports_storage_only_with_a_quota(world):
    u = world["user"](quota_mb=10)
    world["rec"](u, 9 * MB)
    client = app.test_client()
    _login(client, u)
    storage = client.get("/api/user/token-budget").get_json()["storage"]
    assert storage["has_quota"] is True and storage["percentage"] == 90.0
    assert storage["used_label"] == "9 MB" and storage["quota_label"] == "10 MB"
    free = world["user"]()
    _login(client, free)
    assert client.get("/api/user/token-budget").get_json()["storage"]["has_quota"] is False


def test_storage_figures_follow_the_interface_language(world):
    """The header meter and the Account page format sizes for the user's
    interface language (#481): a decimal comma in German."""
    u = world["user"](quota_mb=10)
    u.ui_language = "de"
    db.session.commit()
    world["rec"](u, int(1.5 * MB))
    client = app.test_client()
    _login(client, u)
    storage = client.get("/api/user/token-budget").get_json()["storage"]
    assert storage["used_label"] == "1,5 MB" and storage["quota_label"] == "10 MB"
    page = client.get("/account").get_data(as_text=True)
    assert '"used": "1,5 MB", "quota": "10 MB"' in page
