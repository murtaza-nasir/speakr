"""A tag that changes group shares its recordings with the new group (#586 for tags).

Turning a personal tag into a group tag, moving a group tag to another group
or turning it back into a personal tag ended the shares the previous group had
through the tag, but the recordings that carry the tag were not shared with
the new group's members. A tag that changes group now shares them as adding
the tag would: completed recordings only, by the tag's share settings, and
never touching a share made by hand or one another tag or folder still gives.
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
from src.models import Folder, InternalShare, RecordingTag, SharedRecordingState, Tag, User

from tests.test_folder_group_shares import _client, _shares, team  # noqa: F401  (fixture)
from tests.test_auto_share_lifecycle import _can_read, _drop_second_group, _into_folder, _second_group


@pytest.fixture(autouse=True)
def _sharing_on(monkeypatch):
    for module in (app_module, groups_module, recordings_module, tags_module, api_v1_module):
        monkeypatch.setattr(module, "ENABLE_INTERNAL_SHARING", True, raising=False)


@pytest.fixture
def tag_on(team):
    """make(group_id, auto, lead, *recordings): a tag of the owner on those recordings."""
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
    RecordingTag.query.filter(RecordingTag.tag_id.in_(made)).delete(synchronize_session=False)
    Tag.query.filter(Tag.id.in_(made)).delete(synchronize_session=False)
    db.session.commit()


def _regroup(t, tag, group_id, **settings):
    resp = _client(t["owner"]).put(f"/api/tags/{tag.id}", json=dict(settings, group_id=group_id))
    assert resp.status_code == 200, resp.get_json()
    return resp


def test_personal_tag_made_group_shares_with_members(team, tag_on):
    t = team
    done, busy = t["recording"](), t["recording"]()
    busy.status = "PROCESSING"
    db.session.commit()
    tag = tag_on(None, done, busy)
    assert _shares(done.id) == {}
    _regroup(t, tag, t["group"].id)
    shares = _shares(done.id)
    assert set(shares) == {t["other_admin"].id, t["member"].id}
    member, admin = shares[t["member"].id], shares[t["other_admin"].id]
    assert (member.source_type, member.source_tag_id, member.can_edit) == ("group_tag", tag.id, False)
    assert admin.can_edit is True
    assert len(SharedRecordingState.query.filter_by(recording_id=done.id, user_id=t["member"].id).all()) == 1
    assert _can_read(t["member"], done.id)
    # Recordings still processing get their shares when processing completes
    assert _shares(busy.id) == {}


def test_group_id_sent_as_text_shares(team, tag_on):
    t = team
    r = t["recording"]()
    tag = tag_on(None, r)
    _regroup(t, tag, str(t["group"].id))
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}
    # The same group again, as text, changes nothing
    _regroup(t, tag, str(t["group"].id))
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}


def test_settings_sent_with_the_group_are_followed(team, tag_on):
    t = team
    r = t["recording"]()
    tag = tag_on(None, r)
    _regroup(t, tag, t["group"].id, auto_share_on_apply=False, share_with_group_lead=True)
    shares = _shares(r.id)
    assert set(shares) == {t["other_admin"].id}
    assert shares[t["other_admin"].id].can_edit is True


def test_group_tag_without_share_settings_shares_nothing(team, tag_on):
    t = team
    r = t["recording"]()
    tag = tag_on(None, r)
    _regroup(t, tag, t["group"].id, auto_share_on_apply=False, share_with_group_lead=False)
    assert _shares(r.id) == {}


def test_group_tag_moved_to_another_group_drops_old_and_adds_new(team, tag_on):
    t = team
    r = t["recording"]()
    tag = tag_on(None, r)
    _regroup(t, tag, t["group"].id)
    assert set(_shares(r.id)) == {t["other_admin"].id, t["member"].id}
    other, (newcomer,) = _second_group(t)
    try:
        _regroup(t, tag, other.id)
        shares = _shares(r.id)
        assert set(shares) == {newcomer.id}
        assert (shares[newcomer.id].source_type, shares[newcomer.id].source_tag_id) == ("group_tag", tag.id)
        assert _can_read(newcomer, r.id)
        assert not _can_read(t["member"], r.id)
        assert _can_read(t["owner"], r.id)
    finally:
        _drop_second_group(other, [newcomer], [r.id])


def test_group_tag_moved_follows_the_lead_only_setting(team, tag_on):
    t = team
    r = t["recording"]()
    tag = tag_on(t["group"].id, r, auto=False, lead=True)
    other, (member, lead) = _second_group(t, roles=("member", "admin"))
    try:
        _regroup(t, tag, other.id)
        shares = _shares(r.id)
        assert set(shares) == {lead.id}
        assert shares[lead.id].can_edit is True
    finally:
        _drop_second_group(other, [member, lead], [r.id])


def test_group_tag_made_personal_drops_the_group_shares(team, tag_on):
    t = team
    r = t["recording"]()
    tag = tag_on(None, r)
    _regroup(t, tag, t["group"].id)
    assert _can_read(t["member"], r.id)
    _regroup(t, tag, None)
    assert db.session.get(Tag, tag.id).group_id is None
    assert _shares(r.id) == {}
    assert not _can_read(t["member"], r.id)


def test_regroup_keeps_a_manual_share(team, tag_on):
    t = team
    r = t["recording"]()
    db.session.add(InternalShare(recording_id=r.id, owner_id=t["owner"].id, shared_with_user_id=t["member"].id,
                                 can_edit=False, can_reshare=False, source_type="manual"))
    db.session.commit()
    tag = tag_on(t["group"].id, r)
    other, (newcomer,) = _second_group(t)
    try:
        _regroup(t, tag, other.id)
        assert {uid: s.source_type for uid, s in _shares(r.id).items()} == {
            t["member"].id: "manual", newcomer.id: "group_tag"}
        _regroup(t, tag, None)
        assert {uid: s.source_type for uid, s in _shares(r.id).items()} == {t["member"].id: "manual"}
    finally:
        _drop_second_group(other, [newcomer], [r.id])


def test_regroup_keeps_a_share_a_group_folder_gives(team, tag_on):
    t = team
    f, r = t["folder"](), t["recording"]()
    _into_folder(t, r, f)
    tag = tag_on(t["group"].id, r)
    _regroup(t, tag, None)
    shares = _shares(r.id)
    assert set(shares) == {t["other_admin"].id, t["member"].id}
    assert all((s.source_type, s.source_folder_id) == ("group_folder", f.id) for s in shares.values())


def test_regroup_keeps_a_share_another_group_tag_gives(team, tag_on):
    t = team
    r = t["recording"]()
    first = tag_on(None, r)
    _regroup(t, first, t["group"].id)
    second = tag_on(t["group"].id, r)
    other, (newcomer,) = _second_group(t)
    try:
        _regroup(t, first, other.id)
        shares = _shares(r.id)
        assert set(shares) == {t["other_admin"].id, t["member"].id, newcomer.id}
        assert (shares[t["member"].id].source_type, shares[t["member"].id].source_tag_id) == ("group_tag", second.id)
        assert (shares[newcomer.id].source_type, shares[newcomer.id].source_tag_id) == ("group_tag", first.id)
    finally:
        _drop_second_group(other, [newcomer], [r.id])


def test_regroup_shares_nothing_with_sharing_off(team, tag_on, monkeypatch):
    t = team
    monkeypatch.setattr(tags_module, "ENABLE_INTERNAL_SHARING", False, raising=False)
    r = t["recording"]()
    tag = tag_on(None, r)
    _regroup(t, tag, t["group"].id)
    assert _shares(r.id) == {}


def test_regroup_shares_a_recording_whose_owner_is_not_in_the_group(team, tag_on):
    """As applying the group tag: the group admin who may apply it there shares it, owner excluded."""
    t = team
    s = uuid.uuid4().hex[:8]
    outsider = User(username=f"out_{s}", email=f"out_{s}@local.test", password="x")
    db.session.add(outsider)
    db.session.commit()
    from src.models import Recording
    r = Recording(user_id=outsider.id, title="r", audio_path="/tmp/r.mp3", status="COMPLETED")
    db.session.add(r)
    db.session.commit()
    try:
        db.session.add(InternalShare(recording_id=r.id, owner_id=outsider.id, shared_with_user_id=t["owner"].id,
                                     can_edit=True, can_reshare=False, source_type="manual"))
        db.session.commit()
        tag = tag_on(None, r)
        _regroup(t, tag, t["group"].id)
        shares = _shares(r.id)
        assert set(shares) == {t["owner"].id, t["other_admin"].id, t["member"].id}
        assert shares[t["owner"].id].source_type == "manual"
        assert all(s.owner_id == outsider.id for s in shares.values())
        assert outsider.id not in shares
        _regroup(t, tag, None)
        assert set(_shares(r.id)) == {t["owner"].id}
    finally:
        db.session.rollback()
        for model in (InternalShare, SharedRecordingState, RecordingTag):
            model.query.filter_by(recording_id=r.id).delete()
        Recording.query.filter_by(id=r.id).delete()
        User.query.filter_by(id=outsider.id).delete()
        db.session.commit()
