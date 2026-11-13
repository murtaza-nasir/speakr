"""Inbox and star are per user on the API v1 PATCH routes (#599).

A shared editor's PATCH of is_inbox or is_highlighted wrote the recording's
columns, which are the owner's inbox and star. The web app keeps a
recipient's flags on their SharedRecordingState row; the v1 single and
batch PATCH now do the same, and the v1 detail shows the caller's flags.

SHARED-DB: the users, recording, shares and states are removed afterwards.
"""

import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
import src.api.api_v1 as api_v1_module
import src.api.recordings as recordings_module
from src.app import app, db
from src.models import InternalShare, Recording, SharedRecordingState, User

from tests.test_folder_group_shares import _client

app.config["WTF_CSRF_ENABLED"] = False

FLAGS = {"is_inbox": False, "is_highlighted": True}


@pytest.fixture
def world(monkeypatch):
    for module in (app_module, api_v1_module, recordings_module):
        monkeypatch.setattr(module, "ENABLE_INTERNAL_SHARING", True, raising=False)
    with app.app_context():
        s = secrets.token_hex(4)
        owner, editor, viewer = (User(username=f"sf{k}_{s}", email=f"sf{k}_{s}@local.test", password="x")
                                 for k in ("o", "e", "v"))
        db.session.add_all([owner, editor, viewer])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        is_inbox=True, is_highlighted=False)
        db.session.add(rec)
        db.session.commit()
        db.session.add_all([
            InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=editor.id,
                          can_edit=True, can_reshare=False),
            InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=viewer.id,
                          can_edit=False, can_reshare=False),
        ])
        db.session.commit()
        yield dict(owner=owner, editor=editor, viewer=viewer, rec=rec.id)
        db.session.rollback()
        InternalShare.query.filter_by(recording_id=rec.id).delete()
        SharedRecordingState.query.filter_by(recording_id=rec.id).delete()
        Recording.query.filter_by(id=rec.id).delete()
        User.query.filter(User.id.in_([owner.id, editor.id, viewer.id])).delete(synchronize_session=False)
        db.session.commit()


def _columns(rec_id):
    db.session.expire_all()
    r = db.session.get(Recording, rec_id)
    return (r.is_inbox, r.is_highlighted)


def _state(rec_id, user):
    db.session.expire_all()
    s = SharedRecordingState.query.filter_by(recording_id=rec_id, user_id=user.id).first()
    return None if s is None else (s.is_inbox, s.is_highlighted)


def _owner_view(w):
    body = _client(w["owner"]).get(f"/api/recordings/{w['rec']}").get_json()
    return (body["is_inbox"], body["is_highlighted"])


def test_editor_patch_writes_shared_state(world):
    w = world
    resp = _client(w["editor"]).patch(f"/api/v1/recordings/{w['rec']}", json=FLAGS)
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()["recording"]
    assert (out["is_inbox"], out["is_highlighted"]) == (False, True)
    assert _columns(w["rec"]) == (True, False)
    assert _state(w["rec"], w["editor"]) == (False, True)
    assert _owner_view(w) == (True, False)
    detail = _client(w["editor"]).get(f"/api/v1/recordings/{w['rec']}").get_json()
    assert (detail["is_inbox"], detail["is_highlighted"]) == (False, True)
    owner_detail = _client(w["owner"]).get(f"/api/v1/recordings/{w['rec']}").get_json()
    assert (owner_detail["is_inbox"], owner_detail["is_highlighted"]) == (True, False)


def test_editor_batch_patch_writes_shared_state(world):
    w = world
    resp = _client(w["editor"]).patch("/api/v1/recordings/batch",
                                      json={"recording_ids": [w["rec"]], "updates": FLAGS})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["updated"] == 1
    assert body["results"][0]["success"] is True
    assert _columns(w["rec"]) == (True, False)
    assert _state(w["rec"], w["editor"]) == (False, True)


def test_editor_patch_of_other_fields_leaves_flags(world):
    w = world
    resp = _client(w["editor"]).patch(f"/api/v1/recordings/{w['rec']}", json={"is_highlighted": True})
    assert resp.status_code == 200
    assert _state(w["rec"], w["editor"]) == (True, True)
    assert _columns(w["rec"]) == (True, False)


def test_view_only_patch_is_forbidden(world):
    w = world
    resp = _client(w["viewer"]).patch(f"/api/v1/recordings/{w['rec']}", json=FLAGS)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "permission_denied"
    assert _columns(w["rec"]) == (True, False)


def test_owner_patch_still_sets_recording_flags(world):
    w = world
    resp = _client(w["owner"]).patch(f"/api/v1/recordings/{w['rec']}", json=FLAGS)
    assert resp.status_code == 200
    out = resp.get_json()["recording"]
    assert (out["is_inbox"], out["is_highlighted"]) == (False, True)
    assert _columns(w["rec"]) == (False, True)
    resp = _client(w["owner"]).patch("/api/v1/recordings/batch",
                                     json={"recording_ids": [w["rec"]], "updates": {"is_inbox": True}})
    assert resp.status_code == 200
    assert _columns(w["rec"]) == (True, True)
