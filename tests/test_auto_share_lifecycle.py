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
        Tag.query.filter_by(id=tag.id).delete()
        db.session.commit()


# --- #570 ------------------------------------------------------------------

def _into_folder(t, r, f):
    assert _client(t["owner"]).put(f"/api/recordings/{r.id}/folder", json={"folder_id": f.id}).status_code == 200


def test_removed_member_loses_folder_share(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    assert _can_read(t["member"], r.id)
    resp = _client(t["owner"]).delete(f"/api/admin/groups/{t['group'].id}/members/{t['member'].id}")
    assert resp.status_code == 200
    assert t["member"].id not in _shares(r.id)
    assert not _can_read(t["member"], r.id)
    # The state row stays (personal notes), and the other admin keeps access
    assert len(_state(r.id, t["member"].id)) == 1
    assert t["other_admin"].id in _shares(r.id)
    assert _can_read(t["owner"], r.id)


def test_removed_member_keeps_manual_share(team):
    t = team
    r = t["recording"]()
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["member"].id,
                                 can_edit=False, can_reshare=False, source_type="manual"))
    db.session.commit()
    resp = _client(t["owner"]).delete(f"/api/admin/groups/{t['group'].id}/members/{t['member'].id}")
    assert resp.status_code == 200
    assert _shares(r.id)[t["member"].id].source_type == "manual"
    assert _can_read(t["member"], r.id)


def test_removed_member_keeps_share_another_group_still_gives(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    other = Group(name=f"g2_{uuid.uuid4().hex[:8]}")
    db.session.add(other)
    db.session.commit()
    db.session.add_all([GroupMembership(user_id=t["owner"].id, group_id=other.id, role="admin"),
                        GroupMembership(user_id=t["member"].id, group_id=other.id, role="member")])
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=other.id)
    db.session.add(tag)
    db.session.commit()
    try:
        _into_folder(t, r, f)
        assert _client(t["owner"]).post(f"/api/recordings/{r.id}/tags", json={"tag_id": tag.id}).status_code == 200
        resp = _client(t["owner"]).delete(f"/api/admin/groups/{t['group'].id}/members/{t['member'].id}")
        assert resp.status_code == 200
        share = _shares(r.id)[t["member"].id]
        assert (share.source_type, share.source_tag_id, share.source_folder_id) == ("group_tag", tag.id, None)
        assert _can_read(t["member"], r.id)
    finally:
        RecordingTag.query.filter_by(tag_id=tag.id).delete()
        InternalShare.query.filter_by(recording_id=r.id).delete()
        Tag.query.filter_by(id=tag.id).delete()
        GroupMembership.query.filter_by(group_id=other.id).delete()
        Group.query.filter_by(id=other.id).delete()
        db.session.commit()


def test_demoted_admin_loses_edit_on_group_shares(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    assert _shares(r.id)[t["other_admin"].id].can_edit is True
    resp = _client(t["owner"]).put(f"/api/admin/groups/{t['group'].id}/members/{t['other_admin'].id}",
                                   json={"role": "member"})
    assert resp.status_code == 200
    assert _shares(r.id)[t["other_admin"].id].can_edit is False


def test_deleted_group_ends_its_shares(team):
    t = team
    r = t["recording"]()
    group = Group(name=f"g3_{uuid.uuid4().hex[:8]}")
    db.session.add(group)
    db.session.commit()
    db.session.add_all([GroupMembership(user_id=t["owner"].id, group_id=group.id, role="admin"),
                        GroupMembership(user_id=t["member"].id, group_id=group.id, role="member")])
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=group.id)
    db.session.add(tag)
    db.session.commit()
    admin = User(username=f"adm_{uuid.uuid4().hex[:8]}", email=f"adm_{uuid.uuid4().hex[:8]}@local.test",
                 password="x", is_admin=True)
    db.session.add(admin)
    db.session.commit()
    try:
        assert _client(t["owner"]).post(f"/api/recordings/{r.id}/tags", json={"tag_id": tag.id}).status_code == 200
        assert t["member"].id in _shares(r.id)
        assert _client(admin).delete(f"/api/admin/groups/{group.id}").status_code == 200
        assert t["member"].id not in _shares(r.id)
        assert not _can_read(t["member"], r.id)
    finally:
        RecordingTag.query.filter_by(recording_id=r.id).delete()
        InternalShare.query.filter_by(recording_id=r.id).delete()
        Tag.query.filter_by(id=tag.id).delete()
        GroupMembership.query.filter_by(group_id=group.id).delete()
        Group.query.filter_by(id=group.id).delete()
        User.query.filter_by(id=admin.id).delete()
        db.session.commit()


# --- #582 ------------------------------------------------------------------

def _group_folder_shares(recording_id):
    return [s for s in InternalShare.query.filter_by(recording_id=recording_id) if s.source_type == "group_folder"]


def test_unlinking_group_folder_ends_its_shares(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    assert _can_read(t["member"], r.id)
    resp = _client(t["owner"]).delete(f"/api/recordings/{r.id}/folder")
    assert resp.status_code == 200
    assert db.session.get(Recording, r.id).folder_id is None
    assert _group_folder_shares(r.id) == []
    assert not _can_read(t["member"], r.id)
    assert _can_read(t["owner"], r.id)


@pytest.mark.parametrize("how", ["put_empty", "bulk_none", "v1_patch_none", "v1_batch_none", "personal_folder"])
def test_every_way_out_of_a_group_folder_ends_its_shares(team, how):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    owner = _client(t["owner"])
    if how == "put_empty":
        resp = owner.put(f"/api/recordings/{r.id}/folder", json={"folder_id": None})
    elif how == "bulk_none":
        resp = owner.post("/api/recordings/bulk/folder", json={"recording_ids": [r.id], "folder_id": None})
    elif how == "v1_patch_none":
        resp = owner.patch(f"/api/v1/recordings/{r.id}", json={"folder_id": None})
    elif how == "v1_batch_none":
        resp = owner.patch("/api/v1/recordings/batch", json={"recording_ids": [r.id], "updates": {"folder_id": None}})
    else:
        personal = t["folder"](group_id=None)
        resp = owner.put(f"/api/recordings/{r.id}/folder", json={"folder_id": personal.id})
    assert resp.status_code == 200
    assert _group_folder_shares(r.id) == []


def test_deleting_group_folder_ends_its_shares(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    resp = _client(t["owner"]).delete(f"/api/folders/{f.id}")
    assert resp.status_code == 200
    assert _group_folder_shares(r.id) == []
    assert not _can_read(t["member"], r.id)


def test_manual_share_survives_folder_unlink(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["member"].id,
                                 can_edit=False, can_reshare=False, source_type="manual"))
    db.session.commit()
    _into_folder(t, r, f)
    assert _client(t["owner"]).delete(f"/api/recordings/{r.id}/folder").status_code == 200
    assert _shares(r.id)[t["member"].id].source_type == "manual"
    assert _can_read(t["member"], r.id)
    assert t["other_admin"].id not in _shares(r.id)


def test_folder_share_survives_tag_removal_and_tag_share_survives_unlink(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id)
    db.session.add(tag)
    db.session.commit()
    owner = _client(t["owner"])
    try:
        assert owner.post(f"/api/recordings/{r.id}/tags", json={"tag_id": tag.id}).status_code == 200
        _into_folder(t, r, f)
        # Taking the tag off keeps the share: the folder still gives it
        assert owner.delete(f"/api/recordings/{r.id}/tags/{tag.id}").status_code == 200
        share = _shares(r.id)[t["member"].id]
        assert (share.source_type, share.source_folder_id) == ("group_folder", f.id)
        # Back on, then out of the folder: the tag still gives it
        assert owner.post(f"/api/recordings/{r.id}/tags", json={"tag_id": tag.id}).status_code == 200
        assert owner.delete(f"/api/recordings/{r.id}/folder").status_code == 200
        share = _shares(r.id)[t["member"].id]
        assert (share.source_type, share.source_tag_id) == ("group_tag", tag.id)
    finally:
        RecordingTag.query.filter_by(tag_id=tag.id).delete()
        InternalShare.query.filter_by(recording_id=r.id).delete()
        Tag.query.filter_by(id=tag.id).delete()
        db.session.commit()


def test_folder_moved_to_another_group_ends_old_shares(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    other = Group(name=f"g4_{uuid.uuid4().hex[:8]}")
    db.session.add(other)
    db.session.commit()
    db.session.add(GroupMembership(user_id=t["owner"].id, group_id=other.id, role="admin"))
    db.session.commit()
    try:
        resp = _client(t["owner"]).put(f"/api/folders/{f.id}", json={"group_id": other.id})
        assert resp.status_code == 200
        assert _group_folder_shares(r.id) == []
    finally:
        Folder.query.filter_by(id=f.id).update({"group_id": None})
        GroupMembership.query.filter_by(group_id=other.id).delete()
        Group.query.filter_by(id=other.id).delete()
        db.session.commit()


# --- tag paths (same rule as #570/#582) --------------------------------------

@pytest.fixture
def tagged(team):
    """A completed recording carrying a group tag of the team's group."""
    t = team
    r = t["recording"]()
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id)
    db.session.add(tag)
    db.session.commit()
    assert _client(t["owner"]).post(f"/api/recordings/{r.id}/tags", json={"tag_id": tag.id}).status_code == 200
    assert t["member"].id in _shares(r.id)
    yield dict(t, rec=r, tag=tag)
    db.session.rollback()
    RecordingTag.query.filter_by(recording_id=r.id).delete()
    InternalShare.query.filter_by(recording_id=r.id).delete()
    Tag.query.filter_by(id=tag.id).delete()
    db.session.commit()


@pytest.mark.parametrize("how", ["web_remove", "web_bulk_remove", "web_delete_tag", "web_make_personal",
                                 "v1_remove", "v1_replace", "v1_delete_tag", "v1_batch_remove"])
def test_every_way_a_group_tag_goes_ends_its_shares(tagged, how):
    t = tagged
    r, tag, owner = t["rec"], t["tag"], _client(t["owner"])
    if how == "web_remove":
        resp = owner.delete(f"/api/recordings/{r.id}/tags/{tag.id}")
    elif how == "web_bulk_remove":
        resp = owner.post("/api/recordings/bulk-tags", json={"recording_ids": [r.id], "tag_id": tag.id,
                                                             "action": "remove"})
    elif how == "web_delete_tag":
        resp = owner.delete(f"/api/tags/{tag.id}")
    elif how == "web_make_personal":
        resp = owner.put(f"/api/tags/{tag.id}", json={"group_id": None})
    elif how == "v1_remove":
        resp = owner.delete(f"/api/v1/recordings/{r.id}/tags/{tag.id}")
    elif how == "v1_replace":
        resp = owner.put(f"/api/v1/recordings/{r.id}/tags", json={"tag_ids": []})
    elif how == "v1_delete_tag":
        resp = owner.delete(f"/api/v1/tags/{tag.id}")
    else:
        resp = owner.patch("/api/v1/recordings/batch", json={"recording_ids": [r.id],
                                                              "updates": {"remove_tag_ids": [tag.id]}})
    assert resp.status_code == 200, resp.get_json()
    assert _shares(r.id) == {}
    assert not _can_read(t["member"], r.id)
    assert _can_read(t["owner"], r.id)


def test_tag_removal_keeps_a_manual_share(tagged):
    t = tagged
    r = t["rec"]
    InternalShare.query.filter_by(recording_id=r.id, shared_with_user_id=t["member"].id).update(
        {"source_type": "manual", "source_tag_id": None})
    db.session.commit()
    assert _client(t["owner"]).delete(f"/api/recordings/{r.id}/tags/{t['tag'].id}").status_code == 200
    assert set(_shares(r.id)) == {t["member"].id}
    assert _can_read(t["member"], r.id)


# --- one-shot cleanup of shares left by earlier versions --------------------

def test_cleanup_removes_only_unfounded_auto_shares(team):
    from src.services.auto_shares import remove_unfounded_auto_shares
    t = team
    f, kept, stale, manual = t["folder"](), t["recording"](), t["recording"](), t["recording"]()
    _into_folder(t, kept, f)
    for r, source in ((stale, "group_folder"), (manual, "manual")):
        db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["member"].id,
                                     can_edit=False, can_reshare=False, source_type=source,
                                     source_folder_id=f.id if source == "group_folder" else None))
    db.session.commit()  # stale: as a folder unlink before #582 left it
    assert remove_unfounded_auto_shares() == 1
    assert t["member"].id in _shares(kept.id)
    assert _shares(stale.id) == {}
    assert t["member"].id in _shares(manual.id)
    assert remove_unfounded_auto_shares() == 0


# --- #586: a folder's group changes; the new group's members get the share ---

def _second_group(t, roles=("member",)):
    """A group the owner administers, with new users of the given roles."""
    other = Group(name=f"g5_{uuid.uuid4().hex[:8]}")
    db.session.add(other)
    users = []
    for _role in roles:
        s = uuid.uuid4().hex[:8]
        users.append(User(username=f"nc_{s}", email=f"nc_{s}@local.test", password="x"))
    db.session.add_all(users)
    db.session.commit()
    db.session.add(GroupMembership(user_id=t["owner"].id, group_id=other.id, role="admin"))
    db.session.add_all([GroupMembership(user_id=u.id, group_id=other.id, role=role) for u, role in zip(users, roles)])
    db.session.commit()
    return other, users


def _drop_second_group(other, users, recording_ids):
    db.session.rollback()
    ids = [u.id for u in users]
    InternalShare.query.filter(InternalShare.recording_id.in_(recording_ids),
                               InternalShare.shared_with_user_id.in_(ids)).delete(synchronize_session=False)
    SharedRecordingState.query.filter(SharedRecordingState.recording_id.in_(recording_ids),
                                      SharedRecordingState.user_id.in_(ids)).delete(synchronize_session=False)
    Folder.query.filter(Folder.group_id == other.id).update({"group_id": None})
    GroupMembership.query.filter_by(group_id=other.id).delete()
    Group.query.filter_by(id=other.id).delete()
    User.query.filter(User.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()


def test_changing_folder_group_shares_with_the_new_group(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    assert _can_read(t["member"], r.id)
    other, (newcomer,) = _second_group(t)
    try:
        resp = _client(t["owner"]).put(f"/api/folders/{f.id}", json={"group_id": other.id})
        assert resp.status_code == 200, resp.get_json()
        shares = _shares(r.id)
        assert set(shares) == {newcomer.id}
        share = shares[newcomer.id]
        assert (share.source_type, share.source_folder_id, share.can_edit) == ("group_folder", f.id, False)
        assert len(_state(r.id, newcomer.id)) == 1
        assert _can_read(newcomer, r.id)
        assert not _can_read(t["member"], r.id)
        assert _can_read(t["owner"], r.id)
    finally:
        _drop_second_group(other, [newcomer], [r.id])


def test_changing_folder_group_follows_the_lead_only_setting(team):
    t = team
    f, r = t["folder"](auto=False, lead=True), t["recording"]()
    _into_folder(t, r, f)
    other, (member, lead) = _second_group(t, roles=("member", "admin"))
    try:
        assert _client(t["owner"]).put(f"/api/folders/{f.id}", json={"group_id": other.id}).status_code == 200
        shares = _shares(r.id)
        assert set(shares) == {lead.id}
        assert shares[lead.id].can_edit is True
    finally:
        _drop_second_group(other, [member, lead], [r.id])


def test_clearing_folder_group_revokes_the_share(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    assert _can_read(t["member"], r.id)
    resp = _client(t["owner"]).put(f"/api/folders/{f.id}", json={"group_id": None})
    assert resp.status_code == 200
    assert db.session.get(Folder, f.id).group_id is None
    assert _shares(r.id) == {}
    assert not _can_read(t["member"], r.id)
    assert _can_read(t["owner"], r.id)


def test_folder_group_change_keeps_a_manual_share(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["member"].id,
                                 can_edit=False, can_reshare=False, source_type="manual"))
    db.session.commit()
    _into_folder(t, r, f)
    assert _client(t["owner"]).put(f"/api/folders/{f.id}", json={"group_id": None}).status_code == 200
    assert {uid: s.source_type for uid, s in _shares(r.id).items()} == {t["member"].id: "manual"}


# --- #591: a bulk add of a group tag shares as a single add does ------------

def test_bulk_add_of_a_group_tag_creates_the_share(team):
    t = team
    r = t["recording"]()
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id,
              auto_share_on_apply=True)
    db.session.add(tag)
    db.session.commit()
    try:
        assert not _can_read(t["member"], r.id)
        resp = _client(t["owner"]).post("/api/recordings/bulk-tags", json={"recording_ids": [r.id], "tag_id": tag.id,
                                                                           "action": "add"})
        assert resp.status_code == 200
        assert resp.get_json()["affected_count"] == 1
        shares = _shares(r.id)
        assert set(shares) == {t["other_admin"].id, t["member"].id}
        assert (shares[t["member"].id].source_type, shares[t["member"].id].source_tag_id,
                shares[t["member"].id].can_edit) == ("group_tag", tag.id, False)
        assert shares[t["other_admin"].id].can_edit is True
        assert len(_state(r.id, t["member"].id)) == 1
        assert _can_read(t["member"], r.id)
    finally:
        RecordingTag.query.filter_by(tag_id=tag.id).delete()
        InternalShare.query.filter_by(recording_id=r.id).delete()
        Tag.query.filter_by(id=tag.id).delete()
        db.session.commit()


def test_bulk_add_of_a_group_tag_waits_for_processing(team):
    t = team
    r = t["recording"]()
    r.status = "PROCESSING"
    tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id,
              auto_share_on_apply=True)
    db.session.add(tag)
    db.session.commit()
    try:
        resp = _client(t["owner"]).post("/api/recordings/bulk-tags", json={"recording_ids": [r.id], "tag_id": tag.id,
                                                                           "action": "add"})
        assert resp.status_code == 200
        assert _shares(r.id) == {}
    finally:
        RecordingTag.query.filter_by(tag_id=tag.id).delete()
        Tag.query.filter_by(id=tag.id).delete()
        db.session.commit()


def test_bulk_remove_keeps_a_share_another_group_tag_gives(tagged):
    t = tagged
    r, tag = t["rec"], t["tag"]
    second = Tag(name=f"t2_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id,
                 auto_share_on_apply=True)
    db.session.add(second)
    db.session.commit()
    try:
        db.session.add(RecordingTag(recording_id=r.id, tag_id=second.id, order=2))
        db.session.commit()
        resp = _client(t["owner"]).post("/api/recordings/bulk-tags", json={"recording_ids": [r.id], "tag_id": tag.id,
                                                                           "action": "remove"})
        assert resp.status_code == 200
        share = _shares(r.id)[t["member"].id]
        assert (share.source_type, share.source_tag_id) == ("group_tag", second.id)
        assert _can_read(t["member"], r.id)
    finally:
        RecordingTag.query.filter_by(tag_id=second.id).delete()
        Tag.query.filter_by(id=second.id).delete()
        db.session.commit()


# --- #590: deleting a group tag ends its shares, unless another tag gives them

@pytest.mark.parametrize("how", ["web", "v1"])
def test_deleting_a_group_tag_keeps_a_share_another_group_tag_gives(tagged, how):
    t = tagged
    r, tag = t["rec"], t["tag"]
    second = Tag(name=f"t2_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id,
                 auto_share_on_apply=True)
    db.session.add(second)
    db.session.commit()
    try:
        db.session.add(RecordingTag(recording_id=r.id, tag_id=second.id, order=2))
        db.session.commit()
        path = f"/api/tags/{tag.id}" if how == "web" else f"/api/v1/tags/{tag.id}"
        assert _client(t["owner"]).delete(path).status_code == 200
        share = _shares(r.id)[t["member"].id]
        assert (share.source_type, share.source_tag_id) == ("group_tag", second.id)
        assert _can_read(t["member"], r.id)
    finally:
        RecordingTag.query.filter_by(tag_id=second.id).delete()
        Tag.query.filter_by(id=second.id).delete()
        db.session.commit()
