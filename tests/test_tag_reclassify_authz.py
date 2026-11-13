"""Turning a tag into a group tag follows the rule for applying a group tag (#578, #579).

A view-only recipient who administers some group attached their personal tag
to the owner's recording, then set the tag's group. The group tag then gave
them edit access (#579) and let a sync share the recording with their group
(#578). The bulk paths that apply a group tag skipped the owner-or-admin rule.
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
from src.models import Group, GroupMembership, InternalShare, Recording, RecordingTag, SharedRecordingState, Tag, User

from tests.test_folder_group_shares import _client

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def world(monkeypatch):
    for module in (app_module, groups_module, recordings_module, tags_module, api_v1_module):
        monkeypatch.setattr(module, "ENABLE_INTERNAL_SHARING", True, raising=False)
    with app.app_context():
        users = {}
        for name in ("owner", "recipient", "member"):
            s = uuid.uuid4().hex[:8]
            users[name] = User(username=f"rc_{s}", email=f"rc_{s}@local.test", password="x")
            db.session.add(users[name])
        db.session.commit()
        group = Group(name=f"g_{uuid.uuid4().hex[:8]}")
        db.session.add(group)
        db.session.commit()
        db.session.add_all([GroupMembership(user_id=users["recipient"].id, group_id=group.id, role="admin"),
                            GroupMembership(user_id=users["member"].id, group_id=group.id, role="member")])
        rec = Recording(user_id=users["owner"].id, title="original", audio_path="/tmp/rc.mp3", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        share = InternalShare(recording_id=rec.id, owner_id=users["owner"].id,
                              shared_with_user_id=users["recipient"].id, can_edit=False, can_reshare=False,
                              source_type="manual")
        db.session.add(share)
        db.session.commit()
        yield dict(users, group=group, rec=rec, share=share)
        db.session.rollback()
        ids = [u.id for u in users.values()]
        rec_ids = [r.id for r in Recording.query.filter(Recording.user_id.in_(ids))]
        for model in (InternalShare, SharedRecordingState, RecordingTag):
            model.query.filter(model.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
        Tag.query.filter(Tag.user_id.in_(ids)).delete(synchronize_session=False)
        GroupMembership.query.filter_by(group_id=group.id).delete()
        Group.query.filter_by(id=group.id).delete()
        User.query.filter(User.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()


def _personal_tag_on(client, rec_id):
    resp = client.post("/api/tags", json={"name": f"p_{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 201
    tag_id = resp.get_json()["id"]
    assert client.post(f"/api/recordings/{rec_id}/tags", json={"tag_id": tag_id}).status_code == 200
    return tag_id


def test_view_only_admin_cannot_reclassify_tag_onto_group(world):
    w = world
    recipient = _client(w["recipient"])
    tag_id = _personal_tag_on(recipient, w["rec"].id)
    resp = recipient.put(f"/api/tags/{tag_id}", json={"group_id": w["group"].id})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "group_tag_apply_denied"
    assert db.session.get(Tag, tag_id).group_id is None
    assert RecordingTag.query.filter_by(recording_id=w["rec"].id, tag_id=tag_id).count() == 1

    # #578: the sync has nothing to share
    resp = recipient.post(f"/api/groups/{w['group'].id}/sync-shares")
    assert resp.status_code == 200 and resp.get_json()["shares_created"] == 0
    assert InternalShare.query.filter_by(recording_id=w["rec"].id, shared_with_user_id=w["member"].id).count() == 0
    assert _client(w["member"]).get(f"/api/recordings/{w['rec'].id}").status_code == 404

    # #579: still view-only
    resp = recipient.post("/save", json={"id": w["rec"].id, "title": "renamed"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "recording_edit_denied"
    assert db.session.get(Recording, w["rec"].id).title == "original"


def test_view_only_account_cannot_rename(world):
    w = world
    resp = _client(w["recipient"]).post("/save", json={"id": w["rec"].id, "title": "renamed"})
    assert resp.status_code == 403
    assert db.session.get(Recording, w["rec"].id).title == "original"


def test_editor_who_administers_the_group_may_reclassify(world):
    """Same as applying a group tag directly: an editor who is a group admin may."""
    w = world
    w["share"].can_edit = True
    db.session.commit()
    recipient = _client(w["recipient"])
    tag_id = _personal_tag_on(recipient, w["rec"].id)
    assert recipient.put(f"/api/tags/{tag_id}", json={"group_id": w["group"].id}).status_code == 200
    assert db.session.get(Tag, tag_id).group_id == w["group"].id


def test_owner_reclassify_still_syncs_group_members(world):
    w = world
    db.session.add(GroupMembership(user_id=w["owner"].id, group_id=w["group"].id, role="admin"))
    db.session.commit()
    owner = _client(w["owner"])
    tag_id = _personal_tag_on(owner, w["rec"].id)
    assert owner.put(f"/api/tags/{tag_id}", json={"group_id": w["group"].id}).status_code == 200
    # The change of group shares the recording itself (#586 for tags); a sync finds nothing left
    resp = owner.post(f"/api/groups/{w['group'].id}/sync-shares")
    assert resp.status_code == 200 and resp.get_json()["shares_created"] == 0
    share = InternalShare.query.filter_by(recording_id=w["rec"].id, shared_with_user_id=w["member"].id).one()
    assert (share.source_type, share.can_edit) == ("group_tag", False)
    assert _client(w["member"]).get(f"/api/recordings/{w['rec'].id}").status_code == 200


def test_owner_applied_group_tag_still_lets_admin_edit(world):
    w = world
    db.session.add(GroupMembership(user_id=w["owner"].id, group_id=w["group"].id, role="member"))
    tag = Tag(name=f"g_{uuid.uuid4().hex[:6]}", user_id=w["recipient"].id, group_id=w["group"].id)
    db.session.add(tag)
    db.session.commit()
    assert _client(w["owner"]).post(f"/api/recordings/{w['rec'].id}/tags", json={"tag_id": tag.id}).status_code == 200
    resp = _client(w["recipient"]).post("/save", json={"id": w["rec"].id, "title": "renamed by admin"})
    assert resp.status_code == 200
    assert db.session.get(Recording, w["rec"].id).title == "renamed by admin"


@pytest.mark.parametrize("path", ["web_bulk", "v1_batch"])
def test_plain_member_editor_cannot_bulk_apply_group_tag(world, path):
    """The bulk paths follow the single-tag rule: another user's recording
    takes a group tag only from a group admin."""
    w = world
    w["share"].shared_with_user_id = w["member"].id
    w["share"].can_edit = True
    db.session.commit()
    tag = Tag(name=f"g_{uuid.uuid4().hex[:6]}", user_id=w["recipient"].id, group_id=w["group"].id)
    db.session.add(tag)
    db.session.commit()
    member = _client(w["member"])
    if path == "web_bulk":
        resp = member.post("/api/recordings/bulk-tags", json={"recording_ids": [w["rec"].id], "tag_id": tag.id,
                                                              "action": "add"})
    else:
        resp = member.patch("/api/v1/recordings/batch", json={"recording_ids": [w["rec"].id],
                                                               "updates": {"add_tag_ids": [tag.id]}})
    assert resp.status_code == 200
    assert RecordingTag.query.filter_by(recording_id=w["rec"].id, tag_id=tag.id).count() == 0
