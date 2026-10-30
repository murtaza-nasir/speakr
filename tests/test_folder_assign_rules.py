"""Who may put a recording into a folder, on every move path (#515).

The group-tag rule applied: a group folder takes another user's recording
only from a group admin with edit access, and a personal folder only takes
the caller's own recordings. Without it, an editor of a shared recording
could share it with a whole group by moving it into their group folder.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import db
from src.models import Folder, InternalShare, Recording

from tests.test_folder_group_shares import _client, _shares, team  # noqa: F401  (fixture)


@pytest.fixture
def editors(team, monkeypatch):
    """The owner's recording, shared with edit rights with the other admin and the member."""
    monkeypatch.setattr(sys.modules["src.app"], "ENABLE_INTERNAL_SHARING", True)
    t = team
    rec = t["recording"]()
    for user in (t["other_admin"], t["member"]):
        db.session.add(InternalShare(recording_id=rec.id, owner_id=t["owner"].id,
                                     shared_with_user_id=user.id, can_edit=True, can_reshare=False))
    db.session.commit()
    t["rec"] = rec
    return t


def _move(path, client, rec_id, folder_id):
    if path == "web":
        return client.put(f"/api/recordings/{rec_id}/folder", json={"folder_id": folder_id})
    if path == "bulk":
        return client.post("/api/recordings/bulk/folder",
                           json={"recording_ids": [rec_id], "folder_id": folder_id})
    if path == "v1":
        return client.patch(f"/api/v1/recordings/{rec_id}", json={"folder_id": folder_id})
    return client.patch("/api/v1/recordings/batch",
                        json={"recording_ids": [rec_id], "updates": {"folder_id": folder_id}})


def _refused(path, resp):
    if path == "batch":
        assert resp.status_code == 200
        result = resp.get_json()["results"][0]
        return result["success"] is False and result["code"] == "folder_assign_restricted"
    return resp.status_code == 403 and resp.get_json()["code"] == "folder_assign_restricted"


PATHS = ["web", "bulk", "v1", "batch"]


@pytest.mark.parametrize("path", PATHS)
def test_member_editor_cannot_move_into_group_folder(editors, path):
    t = editors
    # A folder whose sharing would reach the whole group.
    f = t["folder"]()
    resp = _move(path, _client(t["member"]), t["rec"].id, f.id)
    assert _refused(path, resp), resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Recording, t["rec"].id).folder_id is None
    assert InternalShare.query.filter_by(recording_id=t["rec"].id, source_type="group_folder").count() == 0


@pytest.mark.parametrize("path", PATHS)
def test_group_admin_editor_may_move_and_it_shares(editors, path):
    t = editors
    f = t["folder"]()
    resp = _move(path, _client(t["other_admin"]), t["rec"].id, f.id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    if path == "batch":
        assert resp.get_json()["results"][0]["success"] is True
    db.session.expire_all()
    assert db.session.get(Recording, t["rec"].id).folder_id == f.id
    # The member already had a manual share; nobody else is in the group.
    assert set(_shares(t["rec"].id)) == {t["other_admin"].id, t["member"].id}


@pytest.mark.parametrize("path", PATHS)
def test_editor_cannot_put_the_owners_recording_in_their_personal_folder(editors, path):
    t = editors
    theirs = Folder(name="member personal", user_id=t["member"].id)
    db.session.add(theirs)
    db.session.commit()
    resp = _move(path, _client(t["member"]), t["rec"].id, theirs.id)
    assert _refused(path, resp), resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Recording, t["rec"].id).folder_id is None


@pytest.mark.parametrize("path", PATHS)
def test_owner_moves_as_before(editors, path):
    t = editors
    group_folder, personal = t["folder"](), t["folder"](group_id=None)
    client = _client(t["owner"])
    for f in (group_folder, personal):
        resp = _move(path, client, t["rec"].id, f.id)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(Recording, t["rec"].id).folder_id == f.id


@pytest.mark.parametrize("path", PATHS)
def test_editor_may_still_remove_from_a_folder(editors, path):
    t = editors
    f = t["folder"]()
    rec = db.session.get(Recording, t["rec"].id)
    rec.folder_id = f.id
    db.session.commit()
    resp = _move(path, _client(t["member"]), t["rec"].id, None)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Recording, t["rec"].id).folder_id is None
