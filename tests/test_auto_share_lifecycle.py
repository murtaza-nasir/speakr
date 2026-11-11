"""Shares made by a group tag or a group folder live only as long as their reason.

#571: a revoked share leaves the recipient's SharedRecordingState behind, and a
later folder share inserted a second one, so the move answered 500.
#570: removing a group member left the folder share.
#582: taking a recording out of a group folder, or deleting the folder, left it.
The sibling paths (role change, group delete, folder move, bulk move, v1
PATCH and batch, tag removal and deletion) are covered here too.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
import src.api.api_v1 as api_v1_module
import src.api.groups as groups_module
import src.api.recordings as recordings_module
import src.api.tags as tags_module
from src.app import app, db
from src.models import (Folder, Group, GroupMembership, InternalShare, Recording, RecordingTag,
                        SharedRecordingState, Tag, User)

from tests.test_folder_group_shares import _client, _shares, team  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _sharing_on(monkeypatch):
    for module in (app_module, groups_module, recordings_module, tags_module, api_v1_module):
        monkeypatch.setattr(module, "ENABLE_INTERNAL_SHARING", True, raising=False)


def _state(recording_id, user_id):
    return SharedRecordingState.query.filter_by(recording_id=recording_id, user_id=user_id).all()


def _leftover_state(recording_id, user_id, notes="my notes"):
    """A state row as a revoked manual share leaves it: no share, notes kept."""
    db.session.add(SharedRecordingState(recording_id=recording_id, user_id=user_id,
                                        is_inbox=False, is_highlighted=False, personal_notes=notes))
    db.session.commit()


def _can_read(user, recording_id):
    return _client(user).get(f"/api/recordings/{recording_id}").status_code == 200


# --- #571 ------------------------------------------------------------------

def test_web_move_after_revoked_share_reuses_state(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _leftover_state(r.id, t["member"].id)
    resp = _client(t["owner"]).put(f"/api/recordings/{r.id}/folder", json={"folder_id": f.id})
    assert resp.status_code == 200
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}
    states = _state(r.id, t["member"].id)
    assert len(states) == 1
    assert states[0].personal_notes == "my notes"
    assert states[0].is_inbox is True


def test_bulk_move_after_revoked_share_reuses_state(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _leftover_state(r.id, t["member"].id)
    resp = _client(t["owner"]).post("/api/recordings/bulk/folder",
                                    json={"recording_ids": [r.id], "folder_id": f.id})
    assert resp.status_code == 200
    assert resp.get_json()["updated_count"] == 1
    assert len(_state(r.id, t["member"].id)) == 1
    assert t["member"].id in _shares(r.id)


def test_v1_patch_after_revoked_share_reuses_state(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _leftover_state(r.id, t["member"].id)
    resp = _client(t["owner"]).patch(f"/api/v1/recordings/{r.id}", json={"folder_id": f.id})
    assert resp.status_code == 200
    assert len(_state(r.id, t["member"].id)) == 1


def test_sync_shares_after_revoked_share_reuses_state(team):
    t = team
    r = t["recording"]()
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id)
    db.session.add(tag)
    db.session.commit()
    db.session.add(RecordingTag(recording_id=r.id, tag_id=tag.id))
    db.session.commit()
    _leftover_state(r.id, t["member"].id)
    try:
        resp = _client(t["owner"]).post(f"/api/groups/{t['group'].id}/sync-shares")
        assert resp.status_code == 200
        assert len(_state(r.id, t["member"].id)) == 1
        assert _shares(r.id)[t["member"].id].source_type == "group_tag"
    finally:
        RecordingTag.query.filter_by(tag_id=tag.id).delete()
        InternalShare.query.filter_by(recording_id=r.id).delete()
        db.session.delete(db.session.get(Tag, tag.id))
        db.session.commit()
