"""A tag's or folder's group and share settings decide its shares, in the web app and API v1.

API v1 could not change a tag's group or share settings, and a change of
auto_share_on_apply or share_with_group_lead without a change of group left
the existing shares as they were: turning auto-share off kept every member's
access, and turning it on shared nothing until the tag or folder was applied
again. One service function (src.services.auto_shares.sync_source_shares) now
brings the shares in line after either change, on every path. Manual shares
and access another group tag or folder gives always stay.
"""
import json
import os
import secrets
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
import src.api.api_v1 as api_v1_module
import src.api.folders as folders_module
import src.api.groups as groups_module
import src.api.recordings as recordings_module
import src.api.tags as tags_module
import src.tasks.processing as processing_module
from src.app import app, db
from src.models import (APIToken, Folder, GroupMembership, InternalShare, Recording, RecordingTag,
                        SharedRecordingState, Tag, User)
from src.utils.token_auth import hash_token

from tests.test_folder_group_shares import _Client, _client, _shares, team  # noqa: F401  (fixture)
from tests.test_auto_share_lifecycle import (_can_read, _drop_second_group, _into_folder, _leftover_state,
                                             _second_group, _state)


@pytest.fixture(autouse=True)
def _sharing_on(monkeypatch):
    for module in (app_module, groups_module, recordings_module, tags_module, api_v1_module,
                   folders_module, processing_module):
        monkeypatch.setattr(module, "ENABLE_INTERNAL_SHARING", True, raising=False)


@pytest.fixture
def tag_on(team):
    """tag_on(group_id, *recordings, auto, lead): a tag of the owner on those recordings."""
    made = []

    def make(group_id, *recordings, auto=True, lead=True):
        tag = Tag(name=f"t_{uuid.uuid4().hex[:8]}", user_id=team["owner"].id, group_id=group_id,
                  auto_share_on_apply=auto, share_with_group_lead=lead)
        db.session.add(tag)
        db.session.commit()
        for order, r in enumerate(recordings, 1):
            db.session.add(RecordingTag(recording_id=r.id, tag_id=tag.id, order=order))
        db.session.commit()
        made.append(tag.id)
        return tag

    yield make
    db.session.rollback()
    InternalShare.query.filter(InternalShare.source_tag_id.in_(made)).delete(synchronize_session=False)
    RecordingTag.query.filter(RecordingTag.tag_id.in_(made)).delete(synchronize_session=False)
    Tag.query.filter(Tag.id.in_(made)).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture
def token_for(team):
    """token_for(user, scopes): a plain API token; scopes None is a full token."""
    made = []

    def make(user, scopes=None):
        plain = f"tok-{secrets.token_urlsafe(16)}"
        token = APIToken(user_id=user.id, token_hash=hash_token(plain), name="sync",
                         scopes=json.dumps(scopes) if scopes is not None else None)
        db.session.add(token)
        db.session.commit()
        made.append(token.id)
        return plain

    yield make
    db.session.rollback()
    APIToken.query.filter(APIToken.id.in_(made)).delete(synchronize_session=False)
    db.session.commit()


def _put(user, path, body, token=None):
    """PUT as the web app (session) or through API v1 (token)."""
    if token is None:
        return _client(user).put(path, json=body)
    # _Client forgets the user of the previous request, which g keeps in tests
    return _Client(app, app.response_class).put(path, json=body, headers={"Authorization": f"Bearer {token}"})


def _change(t, source, body, how, token_for, expect=200):
    """Change a tag or folder through the web app or API v1 as the owner."""
    kind = "tags" if isinstance(source, Tag) else "folders"
    if how == "web":
        resp = _put(t["owner"], f"/api/{kind}/{source.id}", body)
    else:
        resp = _put(t["owner"], f"/api/v1/{kind}/{source.id}", body, token=token_for(t["owner"]))
    assert resp.status_code == expect, resp.get_json()
    return resp


def _source(t, tag_on, kind, recording, group_id, auto=True, lead=True):
    if kind == "tag":
        return tag_on(group_id, recording, auto=auto, lead=lead)
    folder = t["folder"](auto=auto, lead=lead, group_id=group_id)
    recording.folder_id = folder.id
    db.session.commit()
    return folder


def _sources(kind, s):
    return {uid: (share.source_type, share.source_tag_id if kind == "tag" else share.source_folder_id)
            for uid, share in s.items()}


HOWS = ["web", "v1"]
KINDS = ["tag", "folder"]


# --- Gap 1: API v1 can change a tag's group, with the web app's effects ----

def test_v1_tag_regroup_shares_with_the_new_group(team, tag_on, token_for):
    t = team
    done, busy = t["recording"](), t["recording"]()
    busy.status = "PROCESSING"
    db.session.commit()
    tag = tag_on(None, done, busy)
    resp = _change(t, tag, {"group_id": t["group"].id}, "v1", token_for)
    body = resp.get_json()["tag"]
    assert (body["group_id"], body["is_group_tag"]) == (t["group"].id, True)
    shares = _shares(done.id)
    assert set(shares) == {t["other_admin"].id, t["member"].id}
    assert (shares[t["member"].id].source_type, shares[t["member"].id].source_tag_id,
            shares[t["member"].id].can_edit) == ("group_tag", tag.id, False)
    assert shares[t["other_admin"].id].can_edit is True
    # A recording still processing is shared when processing completes
    assert _shares(busy.id) == {}
    busy.status = "COMPLETED"
    db.session.commit()
    processing_module.apply_team_tag_auto_shares(busy.id)
    assert set(_shares(busy.id)) == {t["other_admin"].id, t["member"].id}


def test_v1_tag_moved_to_another_group_and_made_personal(team, tag_on, token_for):
    t = team
    r = t["recording"]()
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["member"].id,
                                 can_edit=False, can_reshare=False, source_type="manual"))
    db.session.commit()
    tag = tag_on(t["group"].id, r)
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["other_admin"].id,
                                 can_edit=True, can_reshare=False, source_type="group_tag", source_tag_id=tag.id))
    db.session.commit()
    other, (newcomer,) = _second_group(t)
    try:
        _change(t, tag, {"group_id": str(other.id)}, "v1", token_for)
        assert {uid: s.source_type for uid, s in _shares(r.id).items()} == {
            t["member"].id: "manual", newcomer.id: "group_tag"}
        assert not _can_read(t["other_admin"], r.id)
        _change(t, tag, {"group_id": None}, "v1", token_for)
        assert db.session.get(Tag, tag.id).group_id is None
        assert {uid: s.source_type for uid, s in _shares(r.id).items()} == {t["member"].id: "manual"}
    finally:
        _drop_second_group(other, [newcomer], [r.id])


@pytest.mark.parametrize("kind", KINDS)
def test_v1_regroup_needs_admin_of_the_new_group(team, tag_on, token_for, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, None)
    db.session.query(GroupMembership).filter_by(group_id=t["group"].id, user_id=t["owner"].id).update(
        {"role": "member"})
    db.session.commit()
    resp = _change(t, source, {"group_id": t["group"].id}, "v1", token_for, expect=403)
    assert resp.get_json()["code"] == ("tag_group_assign_admin_only" if kind == "tag"
                                       else "folder_group_assign_admin_only")
    db.session.expire_all()
    assert db.session.get(type(source), source.id).group_id is None
    assert _shares(r.id) == {}


@pytest.mark.parametrize("kind", KINDS)
def test_v1_change_that_shares_needs_the_share_scope(team, tag_on, token_for, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, t["group"].id, auto=False, lead=False)
    path = f"/api/v1/{'tags' if kind == 'tag' else 'folders'}/{source.id}"
    narrow = token_for(t["owner"], ["read", "write"])
    resp = _put(t["owner"], path, {"auto_share_on_apply": True}, token=narrow)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "insufficient_scope"
    db.session.expire_all()
    assert db.session.get(type(source), source.id).auto_share_on_apply is False
    assert _shares(r.id) == {}
    # A change that shares nothing needs no share scope
    assert _put(t["owner"], path, {"color": "#112233"}, token=narrow).status_code == 200
    wide = token_for(t["owner"], ["read", "write", "share"])
    assert _put(t["owner"], path, {"auto_share_on_apply": True}, token=wide).status_code == 200
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}
    # Ending shares needs no share scope
    assert _put(t["owner"], path, {"auto_share_on_apply": False}, token=narrow).status_code == 200
    assert set(_shares(r.id)) == set()


# --- #578/#579 on every path ------------------------------------------------

@pytest.fixture
def outsider_recording(team):
    """A recording of a user outside the group, shared by hand with the owner (read only)."""
    t = team
    s = uuid.uuid4().hex[:8]
    outsider = User(username=f"out_{s}", email=f"out_{s}@local.test", password="x")
    db.session.add(outsider)
    db.session.commit()
    r = Recording(user_id=outsider.id, title="r", audio_path="/tmp/r.mp3", status="COMPLETED")
    db.session.add(r)
    db.session.commit()
    share = InternalShare(recording_id=r.id, owner_id=outsider.id, shared_with_user_id=t["owner"].id,
                          can_edit=False, can_reshare=False, source_type="manual")
    db.session.add(share)
    db.session.commit()
    yield r, share
    db.session.rollback()
    for model in (InternalShare, SharedRecordingState, RecordingTag):
        model.query.filter_by(recording_id=r.id).delete()
    Recording.query.filter_by(id=r.id).delete()
    User.query.filter_by(id=outsider.id).delete()
    db.session.commit()


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("kind", KINDS)
def test_regroup_onto_another_users_recording_needs_edit_access(team, tag_on, token_for, outsider_recording,
                                                                how, kind):
    t = team
    r, share = outsider_recording
    source = _source(t, tag_on, kind, r, None)
    resp = _change(t, source, {"group_id": t["group"].id}, how, token_for, expect=403)
    assert resp.get_json()["code"] == ("group_tag_apply_denied" if kind == "tag" else "recording_modify_denied")
    db.session.expire_all()
    assert db.session.get(type(source), source.id).group_id is None
    assert set(_shares(r.id)) == {t["owner"].id}

    # With edit access, the owner outside the group gets no share and the group does
    share.can_edit = True
    db.session.commit()
    _change(t, source, {"group_id": t["group"].id}, how, token_for)
    shares = _shares(r.id)
    assert set(shares) == {t["owner"].id, t["other_admin"].id, t["member"].id}
    assert shares[t["owner"].id].source_type == "manual"
    assert all(s.owner_id == r.user_id for s in shares.values())


# --- Gap 2: share settings changed without a change of group --------------

@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("kind", KINDS)
def test_turning_auto_share_on_shares_the_recordings(team, tag_on, token_for, how, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, t["group"].id, auto=False, lead=False)
    assert _shares(r.id) == {}
    _change(t, source, {"auto_share_on_apply": True}, how, token_for)
    shares = _shares(r.id)
    assert set(shares) == {t["other_admin"].id, t["member"].id}
    assert shares[t["other_admin"].id].can_edit is True and shares[t["member"].id].can_edit is False
    expected_source = ("group_tag" if kind == "tag" else "group_folder", source.id)
    assert set(_sources(kind, shares).values()) == {expected_source}
    assert _can_read(t["member"], r.id)


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("kind", KINDS)
def test_narrowing_to_leads_only_ends_the_member_shares(team, tag_on, token_for, how, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, None)
    _change(t, source, {"group_id": t["group"].id}, how, token_for)
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}
    _change(t, source, {"auto_share_on_apply": False, "share_with_group_lead": True}, how, token_for)
    shares = _shares(r.id)
    assert set(shares) == {t["other_admin"].id}
    assert shares[t["other_admin"].id].can_edit is True
    assert not _can_read(t["member"], r.id)
    # The member's state row stays (personal notes), one row only
    assert len(_state(r.id, t["member"].id)) == 1


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("kind", KINDS)
def test_turning_both_settings_off_ends_every_share(team, tag_on, token_for, how, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, None)
    _change(t, source, {"group_id": t["group"].id}, how, token_for)
    _change(t, source, {"auto_share_on_apply": False, "share_with_group_lead": False}, how, token_for)
    assert _shares(r.id) == {}
    assert not _can_read(t["other_admin"], r.id)
    assert _can_read(t["owner"], r.id)


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("kind", KINDS)
def test_widening_from_leads_only_adds_the_members(team, tag_on, token_for, how, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, None, auto=False, lead=True)
    _change(t, source, {"group_id": t["group"].id}, how, token_for)
    assert set(_shares(r.id)) == {t["other_admin"].id}
    _leftover_state(r.id, t["member"].id)  # left by a revoked manual share (#571)
    _change(t, source, {"auto_share_on_apply": True}, how, token_for)
    shares = _shares(r.id)
    assert set(shares) == {t["other_admin"].id, t["member"].id}
    assert shares[t["member"].id].can_edit is False
    states = _state(r.id, t["member"].id)
    assert len(states) == 1 and states[0].personal_notes == "my notes"
    assert len(_state(r.id, t["other_admin"].id)) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_narrowing_keeps_manual_shares_and_other_reasons(team, tag_on, token_for, kind):
    t = team
    r = t["recording"]()
    s = uuid.uuid4().hex[:8]
    friend = User(username=f"fr_{s}", email=f"fr_{s}@local.test", password="x")
    db.session.add(friend)
    db.session.commit()
    db.session.add(GroupMembership(user_id=friend.id, group_id=t["group"].id, role="member"))
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=friend.id,
                                 can_edit=False, can_reshare=False, source_type="manual"))
    db.session.commit()
    try:
        source = _source(t, tag_on, kind, r, None)
        _change(t, source, {"group_id": t["group"].id}, "web", token_for)
        # Another reason for the member: a group tag of the same group (tag case)
        # or a group tag on a recording in the folder (folder case)
        other_tag = tag_on(t["group"].id, r)
        _change(t, source, {"auto_share_on_apply": False, "share_with_group_lead": False}, "v1", token_for)
        shares = _shares(r.id)
        assert shares[friend.id].source_type == "manual"
        assert (shares[t["member"].id].source_type, shares[t["member"].id].source_tag_id) == (
            "group_tag", other_tag.id)
        assert _can_read(t["member"], r.id)
    finally:
        db.session.rollback()
        InternalShare.query.filter_by(shared_with_user_id=friend.id).delete()
        SharedRecordingState.query.filter_by(user_id=friend.id).delete()
        GroupMembership.query.filter_by(user_id=friend.id).delete()
        User.query.filter_by(id=friend.id).delete()
        db.session.commit()


@pytest.mark.parametrize("how", HOWS)
def test_tag_setting_change_waits_for_processing(team, tag_on, token_for, how):
    t = team
    r = t["recording"]()
    r.status = "PROCESSING"
    db.session.commit()
    tag = tag_on(t["group"].id, r, auto=False, lead=False)
    _change(t, tag, {"share_with_group_lead": True}, how, token_for)
    assert _shares(r.id) == {}
    r.status = "COMPLETED"
    db.session.commit()
    processing_module.apply_team_tag_auto_shares(r.id)
    assert set(_shares(r.id)) == {t["other_admin"].id}


def test_folder_setting_change_shares_a_recording_still_processing(team, token_for):
    """A folder shares whatever the processing status, as a move into it does."""
    t = team
    f, r = t["folder"](auto=False, lead=False), t["recording"]()
    r.status = "PROCESSING"
    db.session.commit()
    _into_folder(t, r, f)
    assert _shares(r.id) == {}
    _change(t, f, {"auto_share_on_apply": True}, "v1", token_for)
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}


def test_setting_change_on_a_parent_folder_leaves_its_subfolder(team, token_for):
    """A subfolder has its own group settings; the parent's change covers its own recordings."""
    t = team
    parent = t["folder"]()
    child = Folder(name=f"c_{uuid.uuid4().hex[:8]}", user_id=t["owner"].id, group_id=t["group"].id,
                   parent_id=parent.id, auto_share_on_apply=True, share_with_group_lead=True)
    db.session.add(child)
    db.session.commit()
    in_parent, in_child = t["recording"](), t["recording"]()
    _into_folder(t, in_parent, parent)
    _into_folder(t, in_child, child)
    try:
        _change(t, parent, {"auto_share_on_apply": False, "share_with_group_lead": False}, "web", token_for)
        assert _shares(in_parent.id) == {}
        shares = _shares(in_child.id)
        assert set(shares) == {t["other_admin"].id, t["member"].id}
        assert all(s.source_folder_id == child.id for s in shares.values())
    finally:
        db.session.rollback()
        Recording.query.filter(Recording.id.in_([in_parent.id, in_child.id])).update({"folder_id": None})
        InternalShare.query.filter(InternalShare.recording_id.in_([in_parent.id, in_child.id])).delete(
            synchronize_session=False)
        Folder.query.filter_by(id=child.id).delete()
        db.session.commit()


@pytest.mark.parametrize("how", HOWS)
def test_settings_sent_for_a_personal_tag_change_nothing(team, tag_on, token_for, how):
    t = team
    r = t["recording"]()
    tag = tag_on(None, r, auto=False, lead=False)
    _change(t, tag, {"auto_share_on_apply": True, "share_with_group_lead": True}, how, token_for)
    db.session.expire_all()
    assert db.session.get(Tag, tag.id).auto_share_on_apply is False
    assert _shares(r.id) == {}


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("kind", KINDS)
def test_deleting_after_a_setting_change_ends_the_shares(team, tag_on, token_for, how, kind):
    t = team
    r = t["recording"]()
    source = _source(t, tag_on, kind, r, t["group"].id, auto=False, lead=True)
    _change(t, source, {"auto_share_on_apply": True}, how, token_for)
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}
    path = f"/api/{'v1/' if how == 'v1' else ''}{'tags' if kind == 'tag' else 'folders'}/{source.id}"
    if how == "web":
        resp = _client(t["owner"]).delete(path)
    else:
        resp = _Client(app, app.response_class).delete(
            path, headers={"Authorization": f"Bearer {token_for(t['owner'])}"})
    assert resp.status_code == 200, resp.get_json()
    assert _shares(r.id) == {}


# --- A lead-only share follows the member's role ---------------------------

def test_demoted_admin_loses_a_lead_only_share(team, tag_on, token_for):
    t = team
    lead_only, everyone = t["recording"](), t["recording"]()
    lead_tag = tag_on(None, lead_only, auto=False, lead=True)
    all_tag = tag_on(None, everyone)
    _change(t, lead_tag, {"group_id": t["group"].id}, "web", token_for)
    _change(t, all_tag, {"group_id": t["group"].id}, "web", token_for)
    assert t["other_admin"].id in _shares(lead_only.id)
    resp = _client(t["owner"]).put(f"/api/admin/groups/{t['group'].id}/members/{t['other_admin'].id}",
                                   json={"role": "member"})
    assert resp.status_code == 200, resp.get_json()
    assert t["other_admin"].id not in _shares(lead_only.id)
    share = _shares(everyone.id)[t["other_admin"].id]
    assert share.can_edit is False
