"""Coverage / behaviour tests for the READ / DELETE / REPROCESS / AUDIO-DELIVERY
and AUTHORIZATION paths in src/api/recordings.py.

Scope (this file owns the read/delete/authz side; the upload/write path lives
elsewhere):
  - list recordings (simple + paginated, owner-scoped, filters, pagination)
  - get single recording detail / status (owner OK, non-owner denied)
  - audio delivery (local send_file path, s3 redirect, deleted/missing audio,
    secure download filename)
  - reprocess transcription / summary / generate_summary (enqueue params,
    ownership, guard conditions)
  - delete recording (row removed + storage.delete called, ownership) and bulk
    delete / bulk reprocess
  - permission/authorization checks on each surface

Harness notes
-------------
1. Flask-Login caches the resolved ``current_user`` on the application context.
   So all DB setup happens inside a short-lived ``_db()`` context that is EXITED
   before any ``client.*`` request; each request then pushes its own context and
   resolves the user from its own session cookie. (Same lesson as
   tests/test_shares_authz.py.)

2. External effects are mocked at the recordings.py import site:
     - ``src.api.recordings.get_storage_service`` -> a fake storage service whose
       ``delete`` is observable and whose ``get_audio_delivery`` returns a
       configurable local-file or redirect result.
     - ``job_queue.enqueue`` is patched on the shared job_queue singleton to
       capture params without spawning workers (workers are already 0 via
       conftest).

3. Every assertion is scoped to the recording IDs / user this test created — the
   session DB is shared across test files, so we never assert on global counts.

Run:
    docker run --rm -v $PWD:/app:ro -e UPLOAD_FOLDER=/tmp/up \
        -e ASR_BASE_URL=http://x:9999 -e COVERAGE_FILE=/tmp/cov/.coverage \
        -e PYTHONDONTWRITEBYTECODE=1 speakr-test:cov sh -c \
        "mkdir -p /tmp/cov && cd /app && python -m pytest \
         tests/test_cov_recordings_read.py -q -o cache_dir=/tmp/ptcache"
"""

import os
import sys
import json
import uuid
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Standalone safety net (conftest.py already sets these under pytest).
if "SQLALCHEMY_DATABASE_URI" not in os.environ:
    _D = tempfile.mkdtemp(prefix="speakr_cov_recordings_")
    os.environ["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(_D, 'test.db')}"
    os.environ.setdefault("UPLOAD_FOLDER", os.path.join(_D, "uploads"))
    os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
    os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")
    os.environ.setdefault("TRANSCRIPTION_API_KEY", "test-key")
    os.environ.setdefault("TRANSCRIPTION_BASE_URL", "https://api.openai.com/v1")

import src.api.recordings as rec_module
from src.app import app, db
from src.models import User, Recording
from src.models.organization import Tag, Folder, RecordingTag
from src.models.templates import TranscriptTemplate

app.config["WTF_CSRF_ENABLED"] = False


# --------------------------------------------------------------------------- #
# Fake storage service
# --------------------------------------------------------------------------- #

@dataclass
class _Delivery:
    mode: str  # 'local_file' or 'redirect_url'
    local_path: Optional[str] = None
    url: Optional[str] = None


class FakeStorage:
    """Observable stand-in for the storage facade used by recordings.py."""

    def __init__(self, *, delivery=None, exists=True):
        self._delivery = delivery
        self._exists = exists
        self.deleted = []          # list of audio_path strings passed to delete()
        self.exists_calls = []

    def delete(self, audio_path, missing_ok=True):
        self.deleted.append(audio_path)
        return True

    def exists(self, audio_path):
        self.exists_calls.append(audio_path)
        return self._exists

    def get_audio_delivery(self, audio_path, **kwargs):
        # Capture kwargs for download_name assertions.
        self.last_delivery_kwargs = kwargs
        return self._delivery


@contextmanager
def use_storage(storage):
    with patch.object(rec_module, "get_storage_service", return_value=storage):
        yield storage


@contextmanager
def capture_enqueue():
    """Patch the shared job_queue.enqueue, capturing every call's kwargs."""
    calls = []

    def fake_enqueue(*args, **kwargs):
        calls.append(kwargs)
        return 12345

    with patch.object(rec_module.job_queue, "enqueue", side_effect=fake_enqueue):
        yield calls


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

@contextmanager
def _db():
    """Short-lived app context for DB work; exit before any HTTP request."""
    with app.app_context():
        yield


_PREFIX = "covrecread"


def make_user():
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"{_PREFIX}_{suffix}",
        email=f"{_PREFIX}_{suffix}@test.local",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_recording(user, *, status="COMPLETED", transcription="hello world this is a transcript",
                   audio_path="local://covrec/audio.mp3", title="rec",
                   summary="a summary", original_filename="audio.mp3",
                   mime_type="audio/mpeg", folder_id=None):
    rec = Recording(
        audio_path=audio_path,
        original_filename=original_filename,
        title=title,
        status=status,
        transcription=transcription,
        summary=summary,
        user_id=user.id,
        mime_type=mime_type,
        folder_id=folder_id,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def new_client():
    return app.test_client()


# --------------------------------------------------------------------------- #
# Module-level cleanup
# --------------------------------------------------------------------------- #

def teardown_module(module):
    with app.app_context():
        users = User.query.filter(User.username.like(f"{_PREFIX}_%")).all()
        for u in users:
            for r in Recording.query.filter_by(user_id=u.id).all():
                RecordingTag.query.filter_by(recording_id=r.id).delete()
                db.session.delete(r)
            Tag.query.filter_by(user_id=u.id).delete()
            Folder.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()


# --------------------------------------------------------------------------- #
# Pytest fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def owner():
    with _db():
        u = make_user()
        return u.id


@pytest.fixture
def other():
    with _db():
        u = make_user()
        return u.id


# =========================================================================== #
# LIST: /recordings  (simple list, owner-scoped)
# =========================================================================== #

def test_get_recordings_lists_only_owner(owner, other):
    with _db():
        u = db.session.get(User, owner)
        o = db.session.get(User, other)
        mine = make_recording(u, title="mine-A")
        theirs = make_recording(o, title="theirs-A")
        mine_id, theirs_id = mine.id, theirs.id

    c = new_client()
    login(c, owner)
    resp = c.get("/recordings")
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.get_json()}
    assert mine_id in ids
    assert theirs_id not in ids


def test_get_recordings_unauthenticated_returns_empty_array():
    c = new_client()
    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert resp.get_json() == []


# =========================================================================== #
# LIST: /api/recordings  (paginated + filters)
# =========================================================================== #

def test_paginated_list_owner_scoped(owner, other):
    with _db():
        u = db.session.get(User, owner)
        o = db.session.get(User, other)
        mine_id = make_recording(u, title="p-mine").id
        theirs_id = make_recording(o, title="p-theirs").id

    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings?per_page=100")
    assert resp.status_code == 200
    body = resp.get_json()
    ids = {r["id"] for r in body["recordings"]}
    assert mine_id in ids
    assert theirs_id not in ids
    assert "pagination" in body


def test_paginated_list_pagination_metadata(owner):
    with _db():
        u = db.session.get(User, owner)
        for i in range(3):
            make_recording(u, title=f"page-{i}")

    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings?per_page=2&page=1")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["pagination"]["per_page"] == 2
    assert body["pagination"]["page"] == 1
    # We created >=3, so total >= 3 and there is a next page.
    assert body["pagination"]["total"] >= 3
    assert body["pagination"]["has_next"] is True
    assert body["pagination"]["has_prev"] is False
    assert len(body["recordings"]) <= 2


def test_paginated_per_page_capped_at_100(owner):
    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings?per_page=9999")
    assert resp.status_code == 200
    assert resp.get_json()["pagination"]["per_page"] == 100


def test_paginated_folder_filter(owner):
    with _db():
        u = db.session.get(User, owner)
        folder = Folder(name=f"fold_{uuid.uuid4().hex[:6]}", user_id=u.id)
        db.session.add(folder)
        db.session.commit()
        in_folder = make_recording(u, title="in-folder", folder_id=folder.id).id
        no_folder = make_recording(u, title="no-folder").id
        folder_id = folder.id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/api/recordings?folder={folder_id}&per_page=100")
    ids = {r["id"] for r in resp.get_json()["recordings"]}
    assert in_folder in ids
    assert no_folder not in ids


def test_paginated_folder_none_filter(owner):
    with _db():
        u = db.session.get(User, owner)
        folder = Folder(name=f"fold_{uuid.uuid4().hex[:6]}", user_id=u.id)
        db.session.add(folder)
        db.session.commit()
        in_folder = make_recording(u, title="nf-in", folder_id=folder.id).id
        no_folder = make_recording(u, title="nf-out").id

    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings?folder=none&per_page=100")
    ids = {r["id"] for r in resp.get_json()["recordings"]}
    assert no_folder in ids
    assert in_folder not in ids


def test_paginated_text_search_by_title(owner):
    marker = f"uniqueterm{uuid.uuid4().hex[:8]}"
    with _db():
        u = db.session.get(User, owner)
        match_id = make_recording(u, title=f"meeting {marker} notes").id
        nomatch_id = make_recording(u, title="unrelated recording").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/api/recordings?q={marker}&per_page=100")
    ids = {r["id"] for r in resp.get_json()["recordings"]}
    assert match_id in ids
    assert nomatch_id not in ids


def test_paginated_tag_filter(owner):
    tagname = f"covtag{uuid.uuid4().hex[:6]}"
    with _db():
        u = db.session.get(User, owner)
        tag = Tag(name=tagname, user_id=u.id)
        db.session.add(tag)
        db.session.commit()
        tagged = make_recording(u, title="tagged-rec")
        db.session.add(RecordingTag(recording_id=tagged.id, tag_id=tag.id, order=1))
        db.session.commit()
        tagged_id = tagged.id
        untagged_id = make_recording(u, title="untagged-rec").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/api/recordings?q=tag:{tagname}&per_page=100")
    ids = {r["id"] for r in resp.get_json()["recordings"]}
    assert tagged_id in ids
    assert untagged_id not in ids


def test_paginated_speaker_filter(owner):
    speaker = f"covspk{uuid.uuid4().hex[:6]}"
    with _db():
        u = db.session.get(User, owner)
        rec = make_recording(u, title="spk-rec")
        rec.participants = speaker
        db.session.commit()
        match_id = rec.id
        nomatch_id = make_recording(u, title="spk-none").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/api/recordings?q=speaker:{speaker}&per_page=100")
    ids = {r["id"] for r in resp.get_json()["recordings"]}
    assert match_id in ids
    assert nomatch_id not in ids


def test_paginated_requires_login():
    c = new_client()
    resp = c.get("/api/recordings")
    assert resp.status_code in (302, 401)


def test_paginated_can_delete_flag_for_owner(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="del-flag").id

    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings?per_page=100")
    rec = next(r for r in resp.get_json()["recordings"] if r["id"] == rid)
    assert rec["is_owner"] is True
    assert rec["can_delete"] is True


# =========================================================================== #
# INBOX / ARCHIVED lists
# =========================================================================== #

def test_inbox_recordings_owner_scoped(owner, other):
    with _db():
        u = db.session.get(User, owner)
        o = db.session.get(User, other)
        mine = make_recording(u, status="PROCESSING", title="inbox-mine")
        mine.is_inbox = True
        theirs = make_recording(o, status="PROCESSING", title="inbox-theirs")
        theirs.is_inbox = True
        db.session.commit()
        mine_id, theirs_id = mine.id, theirs.id

    c = new_client()
    login(c, owner)
    resp = c.get("/api/inbox_recordings")
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.get_json()}
    assert mine_id in ids
    assert theirs_id not in ids


def test_archived_recordings_only_audio_deleted(owner):
    from datetime import datetime
    with _db():
        u = db.session.get(User, owner)
        archived = make_recording(u, title="arch")
        archived.audio_deleted_at = datetime.utcnow()
        db.session.commit()
        archived_id = archived.id
        active_id = make_recording(u, title="active").id

    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings/archived")
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.get_json()}
    assert archived_id in ids
    assert active_id not in ids


# =========================================================================== #
# GET single recording detail
# =========================================================================== #

def test_get_detail_owner_ok(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="detail-ok").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/api/recordings/{rid}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"] == rid
    assert body["is_owner"] is True
    assert body["is_shared"] is False


def test_get_detail_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="detail-denied").id

    c = new_client()
    login(c, other)
    resp = c.get(f"/api/recordings/{rid}")
    assert resp.status_code == 403


def test_get_detail_not_found(owner):
    c = new_client()
    login(c, owner)
    resp = c.get("/api/recordings/99999999")
    assert resp.status_code == 404


def test_get_detail_requires_login(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id
    c = new_client()
    resp = c.get(f"/api/recordings/{rid}")
    assert resp.status_code in (302, 401)


# =========================================================================== #
# status endpoints
# =========================================================================== #

def test_status_only_owner(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="COMPLETED").id
    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/status")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "COMPLETED"


def test_status_only_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id
    c = new_client()
    login(c, other)
    resp = c.get(f"/recording/{rid}/status")
    assert resp.status_code == 403


def test_status_only_not_found(owner):
    c = new_client()
    login(c, owner)
    resp = c.get("/recording/99999999/status")
    assert resp.status_code == 404


def test_full_status_owner(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="full-status").id
    c = new_client()
    login(c, owner)
    resp = c.get(f"/status/{rid}")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == rid


def test_full_status_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id
    c = new_client()
    login(c, other)
    resp = c.get(f"/status/{rid}")
    assert resp.status_code == 403


def test_batch_status_only_accessible(owner, other):
    with _db():
        u = db.session.get(User, owner)
        o = db.session.get(User, other)
        mine = make_recording(u, status="COMPLETED").id
        theirs = make_recording(o, status="FAILED").id

    c = new_client()
    login(c, owner)
    resp = c.post("/api/recordings/batch-status",
                  json={"recording_ids": [mine, theirs]})
    assert resp.status_code == 200
    statuses = resp.get_json()["statuses"]
    assert statuses.get(str(mine)) == "COMPLETED"
    assert str(theirs) not in statuses


def test_batch_status_requires_list(owner):
    c = new_client()
    login(c, owner)
    resp = c.post("/api/recordings/batch-status", json={"recording_ids": "nope"})
    assert resp.status_code == 400


def test_batch_status_too_many(owner):
    c = new_client()
    login(c, owner)
    resp = c.post("/api/recordings/batch-status",
                  json={"recording_ids": list(range(51))})
    assert resp.status_code == 400


# =========================================================================== #
# AUDIO delivery
# =========================================================================== #

def test_audio_local_send_file(owner):
    # Create a real file on disk for send_file to stream.
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.write(fd, b"ID3fakeaudio")
    os.close(fd)
    try:
        with _db():
            u = db.session.get(User, owner)
            rid = make_recording(u, title="audio-local").id

        storage = FakeStorage(delivery=_Delivery(mode="local_file", local_path=path))
        c = new_client()
        login(c, owner)
        with use_storage(storage):
            resp = c.get(f"/audio/{rid}")
        assert resp.status_code == 200
        assert resp.data == b"ID3fakeaudio"
    finally:
        os.remove(path)


def test_audio_s3_redirect(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="audio-s3").id

    storage = FakeStorage(delivery=_Delivery(mode="redirect_url",
                                             url="https://s3.example/signed"))
    c = new_client()
    login(c, owner)
    with use_storage(storage):
        resp = c.get(f"/audio/{rid}")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://s3.example/signed"


def test_audio_missing_local_file_404(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="audio-missing").id

    storage = FakeStorage(delivery=_Delivery(mode="local_file",
                                             local_path="/nonexistent/path.mp3"))
    c = new_client()
    login(c, owner)
    with use_storage(storage):
        resp = c.get(f"/audio/{rid}")
    assert resp.status_code == 404


def test_audio_no_audio_path_404(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, audio_path=None, title="no-audio").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/audio/{rid}")
    assert resp.status_code == 404


def test_audio_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, title="audio-denied").id

    storage = FakeStorage(delivery=_Delivery(mode="redirect_url", url="x"))
    c = new_client()
    login(c, other)
    with use_storage(storage):
        resp = c.get(f"/audio/{rid}")
    assert resp.status_code == 403


def test_audio_download_secure_filename(owner):
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.write(fd, b"audiobytes")
    os.close(fd)
    try:
        with _db():
            u = db.session.get(User, owner)
            # Title with characters that must be stripped from download_name.
            rid = make_recording(u, title="My/Bad:Title*?",
                                 original_filename="orig.wav").id

        storage = FakeStorage(delivery=_Delivery(mode="local_file", local_path=path))
        c = new_client()
        login(c, owner)
        with use_storage(storage):
            resp = c.get(f"/audio/{rid}?download=true")
        assert resp.status_code == 200
        # The endpoint computes a sanitized download_name and passes it to
        # storage.get_audio_delivery; verify the dangerous chars were stripped
        # and the original-filename extension (.wav) preserved.
        dn = storage.last_delivery_kwargs["download_name"]
        assert "/" not in dn and ":" not in dn and "*" not in dn and "?" not in dn
        assert dn.endswith(".wav")
        assert storage.last_delivery_kwargs["download"] is True
        # Response is served as an attachment.
        assert "attachment" in resp.headers.get("Content-Disposition", "")
    finally:
        os.remove(path)


# =========================================================================== #
# REPROCESS transcription
# =========================================================================== #

def test_reprocess_transcription_enqueues(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="COMPLETED").id

    storage = FakeStorage(exists=True)
    c = new_client()
    login(c, owner)
    with use_storage(storage), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_transcription",
                      json={"language": "es", "min_speakers": "2"})
    assert resp.status_code in (200, 202)
    assert len(calls) == 1
    call = calls[0]
    assert call["job_type"] == "reprocess_transcription"
    assert call["recording_id"] == rid
    assert call["params"]["language"] == "es"
    assert call["params"]["min_speakers"] == 2


def test_reprocess_transcription_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    storage = FakeStorage(exists=True)
    c = new_client()
    login(c, other)
    with use_storage(storage), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_transcription", json={})
    assert resp.status_code == 403
    assert calls == []


def test_reprocess_transcription_audio_missing_404(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    storage = FakeStorage(exists=False)  # storage.exists() returns False
    c = new_client()
    login(c, owner)
    with use_storage(storage), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_transcription", json={})
    assert resp.status_code == 404
    assert calls == []


def test_reprocess_transcription_already_processing(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="PROCESSING").id

    storage = FakeStorage(exists=True)
    c = new_client()
    login(c, owner)
    with use_storage(storage), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_transcription", json={})
    assert resp.status_code == 400
    assert calls == []


def test_reprocess_transcription_not_found(owner):
    c = new_client()
    login(c, owner)
    storage = FakeStorage(exists=True)
    with use_storage(storage):
        resp = c.post("/recording/99999999/reprocess_transcription", json={})
    assert resp.status_code == 404


# =========================================================================== #
# REPROCESS summary
# =========================================================================== #

def test_reprocess_summary_enqueues(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="COMPLETED",
                             transcription="a real transcript with content").id

    c = new_client()
    login(c, owner)
    # client (the OpenRouter client) must not be None for the endpoint to proceed.
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_summary", json={})
    assert resp.status_code in (200, 202)
    assert len(calls) == 1
    assert calls[0]["job_type"] == "reprocess_summary"
    assert calls[0]["recording_id"] == rid


def test_reprocess_summary_custom_prompt_append(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="a real transcript here").id

    c = new_client()
    login(c, owner)
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_summary",
                      json={"custom_prompt": "Be brief", "prompt_mode": "append"})
    assert resp.status_code in (200, 202)
    params = calls[0]["params"]
    assert params["custom_prompt"] == "Be brief"
    assert params["custom_prompt_append"] is True


def test_reprocess_summary_no_transcription_400(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="").id

    c = new_client()
    login(c, owner)
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_summary", json={})
    assert resp.status_code == 400
    assert calls == []


def test_reprocess_summary_transcription_error_400(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="Transcription failed: boom").id

    c = new_client()
    login(c, owner)
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_summary", json={})
    assert resp.status_code == 400
    assert calls == []


def test_reprocess_summary_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="a real transcript here").id

    c = new_client()
    login(c, other)
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_summary", json={})
    assert resp.status_code == 403
    assert calls == []


def test_reprocess_summary_client_unavailable_503(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="a real transcript here").id

    c = new_client()
    login(c, owner)
    with patch.object(rec_module, "client", None), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/reprocess_summary", json={})
    assert resp.status_code == 503
    assert calls == []


# =========================================================================== #
# generate_summary
# =========================================================================== #

def test_generate_summary_enqueues(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="COMPLETED",
                             transcription="a real transcript here").id

    c = new_client()
    login(c, owner)
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/generate_summary", json={})
    assert resp.status_code in (200, 202)
    assert len(calls) == 1
    assert calls[0]["job_type"] == "summarize"


def test_generate_summary_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="a real transcript here").id

    c = new_client()
    login(c, other)
    with patch.object(rec_module, "client", object()), capture_enqueue() as calls:
        resp = c.post(f"/recording/{rid}/generate_summary", json={})
    assert resp.status_code == 403
    assert calls == []


# =========================================================================== #
# DELETE single recording
# =========================================================================== #

def test_delete_recording_removes_row_and_calls_storage(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, audio_path="local://covrec/del.mp3").id

    storage = FakeStorage()
    c = new_client()
    login(c, owner)
    with use_storage(storage):
        resp = c.delete(f"/recording/{rid}")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert "local://covrec/del.mp3" in storage.deleted
    with _db():
        assert db.session.get(Recording, rid) is None


def test_delete_recording_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    storage = FakeStorage()
    c = new_client()
    login(c, other)
    with use_storage(storage):
        resp = c.delete(f"/recording/{rid}")
    assert resp.status_code == 403
    assert storage.deleted == []
    with _db():
        assert db.session.get(Recording, rid) is not None


def test_delete_recording_not_found(owner):
    c = new_client()
    login(c, owner)
    storage = FakeStorage()
    with use_storage(storage):
        resp = c.delete("/recording/99999999")
    assert resp.status_code == 404


def test_delete_recording_requires_login(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id
    c = new_client()
    resp = c.delete(f"/recording/{rid}")
    assert resp.status_code in (302, 401)
    with _db():
        assert db.session.get(Recording, rid) is not None


# =========================================================================== #
# BULK delete
# =========================================================================== #

def test_bulk_delete_owner(owner):
    with _db():
        u = db.session.get(User, owner)
        a = make_recording(u, audio_path="local://covrec/a.mp3").id
        b = make_recording(u, audio_path="local://covrec/b.mp3").id

    storage = FakeStorage()
    c = new_client()
    login(c, owner)
    with use_storage(storage):
        resp = c.delete("/api/recordings/bulk", json={"recording_ids": [a, b]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body["deleted_ids"]) == {a, b}
    assert body["deleted_count"] == 2
    with _db():
        assert db.session.get(Recording, a) is None
        assert db.session.get(Recording, b) is None


def test_bulk_delete_skips_non_owned(owner, other):
    with _db():
        u = db.session.get(User, owner)
        o = db.session.get(User, other)
        mine = make_recording(u, audio_path="local://covrec/m.mp3").id
        theirs = make_recording(o, audio_path="local://covrec/t.mp3").id

    storage = FakeStorage()
    c = new_client()
    login(c, owner)
    with use_storage(storage):
        resp = c.delete("/api/recordings/bulk",
                        json={"recording_ids": [mine, theirs]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert mine in body["deleted_ids"]
    assert theirs not in body["deleted_ids"]
    assert body["errors"]  # the non-owned one reports an error
    with _db():
        assert db.session.get(Recording, mine) is None
        assert db.session.get(Recording, theirs) is not None  # untouched


def test_bulk_delete_empty_list_400(owner):
    c = new_client()
    login(c, owner)
    resp = c.delete("/api/recordings/bulk", json={"recording_ids": []})
    assert resp.status_code == 400


def test_bulk_delete_missing_key_400(owner):
    c = new_client()
    login(c, owner)
    resp = c.delete("/api/recordings/bulk", json={})
    assert resp.status_code == 400


def test_bulk_delete_too_many_400(owner):
    c = new_client()
    login(c, owner)
    resp = c.delete("/api/recordings/bulk",
                    json={"recording_ids": list(range(101))})
    assert resp.status_code == 400


# =========================================================================== #
# BULK reprocess
# =========================================================================== #

def test_bulk_reprocess_summary_enqueues(owner):
    with _db():
        u = db.session.get(User, owner)
        a = make_recording(u, status="COMPLETED", transcription="content a").id
        b = make_recording(u, status="COMPLETED", transcription="content b").id

    c = new_client()
    login(c, owner)
    with capture_enqueue() as calls:
        resp = c.post("/api/recordings/bulk-reprocess",
                      json={"recording_ids": [a, b], "type": "summary"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body["queued_ids"]) == {a, b}
    assert all(call["job_type"] == "reprocess_summary" for call in calls)
    assert len(calls) == 2


def test_bulk_reprocess_transcription_checks_audio(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="COMPLETED").id

    storage = FakeStorage(exists=True)
    c = new_client()
    login(c, owner)
    with use_storage(storage), capture_enqueue() as calls:
        resp = c.post("/api/recordings/bulk-reprocess",
                      json={"recording_ids": [rid], "type": "transcription"})
    assert resp.status_code == 200
    assert rid in resp.get_json()["queued_ids"]
    assert calls[0]["job_type"] == "reprocess_transcription"


def test_bulk_reprocess_skips_non_owned(owner, other):
    with _db():
        u = db.session.get(User, owner)
        o = db.session.get(User, other)
        mine = make_recording(u, status="COMPLETED", transcription="x").id
        theirs = make_recording(o, status="COMPLETED", transcription="y").id

    c = new_client()
    login(c, owner)
    with capture_enqueue() as calls:
        resp = c.post("/api/recordings/bulk-reprocess",
                      json={"recording_ids": [mine, theirs], "type": "summary"})
    assert resp.status_code == 200
    qids = resp.get_json()["queued_ids"]
    assert mine in qids
    assert theirs not in qids


def test_bulk_reprocess_skips_non_completed(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, status="PROCESSING", transcription="x").id

    c = new_client()
    login(c, owner)
    with capture_enqueue() as calls:
        resp = c.post("/api/recordings/bulk-reprocess",
                      json={"recording_ids": [rid], "type": "summary"})
    assert resp.status_code == 200
    assert rid not in resp.get_json()["queued_ids"]
    assert calls == []


def test_bulk_reprocess_bad_type_400(owner):
    c = new_client()
    login(c, owner)
    resp = c.post("/api/recordings/bulk-reprocess",
                  json={"recording_ids": [1], "type": "nope"})
    assert resp.status_code == 400


# =========================================================================== #
# delete-via-job + toggle_deletion_exempt authz
# =========================================================================== #

def test_toggle_deletion_exempt_owner(owner):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    c = new_client()
    login(c, owner)
    resp = c.post(f"/api/recordings/{rid}/toggle_deletion_exempt")
    assert resp.status_code == 200
    assert resp.get_json()["deletion_exempt"] is True


def test_toggle_deletion_exempt_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    c = new_client()
    login(c, other)
    resp = c.post(f"/api/recordings/{rid}/toggle_deletion_exempt")
    assert resp.status_code == 403


def test_process_chunks_non_owner_denied(owner, other):
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    c = new_client()
    login(c, other)
    resp = c.post(f"/api/recording/{rid}/process_chunks")
    assert resp.status_code == 403


# =========================================================================== #
# DOWNLOAD: transcript-with-template / summary / chat  (owner content checks +
# diarized-vs-plain template formatting).  These close mutation survivors in the
# download handlers that no other test reaches; the 403/authz side is owned by
# tests/test_cov_recordings_download_authz.py.
# =========================================================================== #

def _diarized_json():
    """A speaker-labelled (diarized) transcript: a JSON list of segments.

    download_transcript_with_template treats a JSON *list* as diarized and runs
    the per-segment template formatter; anything else is plain text returned
    verbatim.  Keys match what the handler reads: speaker / sentence /
    start_time / end_time.
    """
    return json.dumps([
        {"speaker": "Alice", "sentence": "Hello there", "start_time": 0.0, "end_time": 1.5},
        {"speaker": "Bob", "sentence": "Hi back", "start_time": 1.5, "end_time": 3.0},
    ])


def _make_template(user, *, name, template, is_default=False):
    t = TranscriptTemplate(user_id=user.id, name=name, template=template,
                           is_default=is_default)
    db.session.add(t)
    db.session.commit()
    return t


# --- /download/transcript : content-presence guard (line 173) ------------- #

def test_download_transcript_empty_transcription_400(owner):
    """No transcription -> 400 (guard at recordings.py:173).

    MUTATION-VERIFIED: `if not recording.transcription` -> `if recording.transcription`
    makes the empty-transcript recording fall through to a 200 download; this test
    then FAILS.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/transcript")
    assert resp.status_code == 400
    assert "transcription" in resp.get_json()["error"].lower()


def test_download_transcript_present_returns_file_not_400(owner):
    """A transcription present -> a 200 text file, NOT a 400 (line 173 inverse)."""
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="hello plain transcript body").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/transcript")
    assert resp.status_code == 200
    assert b"hello plain transcript body" in resp.data
    assert resp.headers["Content-Type"].startswith("text/plain")


# --- /download/transcript : plain-text vs diarized branch (221/226/232) ---- #

def test_download_transcript_plain_text_returned_verbatim(owner):
    """A non-JSON transcription is returned as-is (is_diarized False path).

    MUTATION-VERIFIED: flipping line 232 `if not is_diarized` -> `if is_diarized`
    sends plain text down the segment-iteration branch (iterating None) -> 500;
    this test (expecting 200 + verbatim body) then FAILS.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription="just some words, not json").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/transcript")
    assert resp.status_code == 200
    assert resp.data.decode() == "just some words, not json"
    # Plain-text filename branch (line 272): no "_formatted" / template suffix.
    cd = resp.headers.get("Content-Disposition", "")
    assert "_formatted" not in cd


def test_download_transcript_diarized_no_template_basic_format(owner):
    """A diarized transcript with no template uses the basic `[speaker]: text`
    format and a `_formatted` filename.

    MUTATION-VERIFIED: line 226 (`is_diarized = True`) or line 232 flips send the
    JSON list down the plain-text branch (raw JSON returned), failing the body
    assertion; line 193 (`if not template`) flipped dereferences None.template
    -> 500; line 266/268 filename branch flip drops `_formatted`.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, transcription=_diarized_json(), title="diar-basic").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/transcript")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Template applied per segment -> formatted lines, NOT the raw JSON string.
    assert "[Alice]: Hello there" in body
    assert "[Bob]: Hi back" in body
    assert '"sentence"' not in body  # raw JSON would contain this key
    assert "_formatted" in resp.headers.get("Content-Disposition", "")


def test_download_transcript_diarized_uses_default_template(owner):
    """Without an explicit template_id, the user's *default* template is chosen
    (lines 187-189) and used to format (line 196), and its name lands in the
    filename (line 266).

    MUTATION-VERIFIED: line 189 `is_default=True` -> `is_default=False` selects the
    non-default template ("OTH ..."), failing the "DEF ..." body assertion; line 193
    flip uses the basic `[..]` format instead of template.template, also failing.
    """
    with _db():
        u = db.session.get(User, owner)
        _make_template(u, name="OtherTmpl", template="OTH {{speaker}}={{text}}",
                       is_default=False)
        _make_template(u, name="DefaultTmpl", template="DEF {{speaker}}={{text}}",
                       is_default=True)
        rid = make_recording(u, transcription=_diarized_json(), title="diar-tmpl").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/transcript")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "DEF Alice=Hello there" in body
    assert "DEF Bob=Hi back" in body
    assert "OTH " not in body
    # is_diarized and template -> filename embeds the template name (line 266/267).
    assert "DefaultTmpl" in resp.headers.get("Content-Disposition", "")


def test_download_transcript_explicit_template_id(owner):
    """An explicit ?template_id= selects that template (lines 180-184) over the
    default."""
    with _db():
        u = db.session.get(User, owner)
        _make_template(u, name="DefaultTmpl", template="DEF {{speaker}}={{text}}",
                       is_default=True)
        chosen = _make_template(u, name="ChosenTmpl",
                                template="PICK {{speaker}}/{{text}}", is_default=False)
        chosen_id = chosen.id
        rid = make_recording(u, transcription=_diarized_json(), title="diar-pick").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/transcript?template_id={chosen_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "PICK Alice/Hello there" in body
    assert "DEF " not in body


# --- /download/summary : content-presence guard (line 303) ---------------- #

def test_download_summary_empty_400(owner):
    """No summary -> 400 (guard at recordings.py:303).

    MUTATION-VERIFIED: `if not recording.summary` -> `if recording.summary` lets the
    empty-summary recording fall through to a 200 .docx; this test then FAILS.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, summary="").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/summary")
    assert resp.status_code == 400
    assert "summary" in resp.get_json()["error"].lower()


def test_download_summary_present_returns_docx(owner):
    """A summary present -> a 200 Word document, not a 400 (line 303 inverse).

    Also pins recordings.py:368 `as_attachment=False`: the doc is a BytesIO with
    no download_name, so MUTATION-VERIFIED flipping it to `as_attachment=True`
    raises "No name provided for attachment" -> 500, and this 200 assertion FAILS.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u, summary="## Heading\n\nSome **summary** content.").id

    c = new_client()
    login(c, owner)
    resp = c.get(f"/recording/{rid}/download/summary")
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["Content-Type"]
    assert len(resp.data) > 0


# --- /download/chat : messages-presence guard (line 421) ------------------ #

def test_download_chat_empty_messages_400(owner):
    """An empty messages list -> 400 (guard at recordings.py:421, distinct from
    the missing-key guard at 417).

    MUTATION-VERIFIED: `if not messages` -> `if messages` lets the empty list build
    an empty .docx and return 200; this test then FAILS.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    c = new_client()
    login(c, owner)
    resp = c.post(f"/recording/{rid}/download/chat", json={"messages": []})
    assert resp.status_code == 400
    assert "messages" in resp.get_json()["error"].lower()


def test_download_chat_missing_key_400(owner):
    """No 'messages' key at all -> 400 (guard at recordings.py:417)."""
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    c = new_client()
    login(c, owner)
    resp = c.post(f"/recording/{rid}/download/chat", json={})
    assert resp.status_code == 400


def test_download_chat_with_messages_returns_docx(owner):
    """Messages present -> a 200 Word document (line 421 inverse).

    Also pins recordings.py:507 `as_attachment=False` (same BytesIO/no-name
    mechanism as the summary download): MUTATION-VERIFIED flipping it to True -> 500.
    """
    with _db():
        u = db.session.get(User, owner)
        rid = make_recording(u).id

    c = new_client()
    login(c, owner)
    resp = c.post(
        f"/recording/{rid}/download/chat",
        json={"messages": [
            {"role": "user", "content": "What was discussed?"},
            {"role": "assistant", "content": "The **roadmap**."},
        ]},
    )
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["Content-Type"]
    assert len(resp.data) > 0


# --- ENABLE_INQUIRE_MODE gate on the async reindex helper (line 118) ------- #

def test_reindex_chunks_noop_when_inquire_disabled(owner):
    """reindex_recording_chunks_async is a no-op when Inquire mode is off
    (guard at recordings.py:118).

    MUTATION-VERIFIED: `if not ENABLE_INQUIRE_MODE` -> `if ENABLE_INQUIRE_MODE`
    makes the disabled-mode call spawn a reindex thread; this test (asserting no
    thread / no process_recording_chunks call) then FAILS.
    """
    # Force the disabled state (the test image may have inquire enabled), so the
    # gate is exercised regardless of environment.
    with _db():
        with patch.object(rec_module, "ENABLE_INQUIRE_MODE", False), \
             patch.object(rec_module, "process_recording_chunks") as proc, \
             patch.object(rec_module.threading, "Thread") as thread_cls:
            rec_module.reindex_recording_chunks_async(987654321)
            thread_cls.assert_not_called()
            proc.assert_not_called()
