"""Shared Transcripts list (#416).

The list showed public links only; it predates internal sharing. The
overview has what the user shared (public links, and recordings shared with
people and groups, grouped by recording) and what was shared with them. A
group share follows its tag and is not revoked from the list.

SHARED-DB: users, shares, tags and recordings are removed afterwards.
"""

import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import InternalShare, Recording, Share, Tag, User
import src.api.shares as shares_api
from tests.test_recording_sessions import _login

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(shares_api, "ENABLE_INTERNAL_SHARING", True)
    monkeypatch.setattr(shares_api, "SHOW_USERNAMES_IN_UI", True)
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"me_{s}", email=f"me_{s}@local.test", password="x")
        ana = User(username=f"ana_{s}", email=f"ana_{s}@local.test", password="x")
        bo = User(username=f"bo_{s}", email=f"bo_{s}@local.test", password="x")
        db.session.add_all([me, ana, bo])
        db.session.commit()
        mine = Recording(user_id=me.id, title="Budget review", status="COMPLETED", original_filename="a.mp3")
        theirs = Recording(user_id=ana.id, title="Ana's call", status="COMPLETED", original_filename="b.mp3")
        db.session.add_all([mine, theirs])
        db.session.commit()
        tag = Tag(name=f"team_{s}", user_id=me.id)
        db.session.add(tag)
        db.session.commit()
        db.session.add_all([
            InternalShare(recording_id=mine.id, owner_id=me.id, shared_with_user_id=ana.id, can_edit=True),
            InternalShare(recording_id=mine.id, owner_id=me.id, shared_with_user_id=bo.id,
                          source_type="group_tag", source_tag_id=tag.id),
            InternalShare(recording_id=theirs.id, owner_id=ana.id, shared_with_user_id=me.id),
            Share(recording_id=mine.id, user_id=me.id, share_summary=True, share_notes=False),
        ])
        db.session.commit()
        ids = dict(me=me.id, ana=ana.id, bo=bo.id, mine=mine.id, theirs=theirs.id, tag=tag.id)
        yield ids
        db.session.rollback()
        InternalShare.query.filter(InternalShare.recording_id.in_([ids["mine"], ids["theirs"]])).delete(synchronize_session=False)
        Share.query.filter_by(user_id=ids["me"]).delete()
        Recording.query.filter(Recording.id.in_([ids["mine"], ids["theirs"]])).delete(synchronize_session=False)
        Tag.query.filter_by(id=ids["tag"]).delete()
        User.query.filter(User.id.in_([ids["me"], ids["ana"], ids["bo"]])).delete(synchronize_session=False)
        db.session.commit()


def _overview(uid):
    client = app.test_client()
    with app.app_context():
        _login(client, db.session.get(User, uid))
    resp = client.get("/api/shares/overview")
    assert resp.status_code == 200
    return resp.get_json(), client


def test_the_list_has_public_links_internal_shares_and_shared_with_me(world):
    body, _ = _overview(world["me"])
    assert body["internal_sharing_enabled"] is True
    assert [link["recording_id"] for link in body["public_links"]] == [world["mine"]]
    [entry] = body["shared_by_me"]
    assert entry["recording_title"] == "Budget review"
    people = {p["user_id"]: p for p in entry["recipients"]}
    assert people[world["ana"]]["can_edit"] is True and people[world["ana"]]["revocable"] is True
    assert people[world["bo"]]["source"]["type"] == "group_tag" and people[world["bo"]]["revocable"] is False
    assert people[world["bo"]]["source"]["name"].startswith("team_")
    [received] = body["shared_with_me"]
    assert received["recording_id"] == world["theirs"] and received["owner_username"].startswith("ana_")


def test_a_recipient_sees_it_under_shared_with_me(world):
    body, _ = _overview(world["ana"])
    assert [r["recording_id"] for r in body["shared_with_me"]] == [world["mine"]]
    assert [e["recording_id"] for e in body["shared_by_me"]] == [world["theirs"]]


def test_revoking_from_the_list_removes_the_person(world):
    body, client = _overview(world["me"])
    share_id = next(p["share_id"] for p in body["shared_by_me"][0]["recipients"] if p["user_id"] == world["ana"])
    assert client.delete(f"/api/internal-shares/{share_id}").status_code == 200
    body = client.get("/api/shares/overview").get_json()
    assert [p["user_id"] for p in body["shared_by_me"][0]["recipients"]] == [world["bo"]]


def test_without_internal_sharing_only_public_links(world, monkeypatch):
    monkeypatch.setattr(shares_api, "ENABLE_INTERNAL_SHARING", False)
    body, _ = _overview(world["me"])
    assert body["internal_sharing_enabled"] is False
    assert body["shared_by_me"] == [] and body["shared_with_me"] == []
    assert len(body["public_links"]) == 1


def test_owner_names_follow_the_username_setting(world, monkeypatch):
    monkeypatch.setattr(shares_api, "SHOW_USERNAMES_IN_UI", False)
    body, _ = _overview(world["me"])
    assert body["shared_with_me"][0]["owner_username"] is None


# The recording detail and the main recording list gave the owner's username
# of a shared recording whatever the setting said; they follow it now, like
# the shared-with-me list above.

def _shared_views(world, monkeypatch, show):
    """The shared recording as the detail and the main list return it to me."""
    import src.api.recordings as recordings_api
    import src.app as app_module
    for module in (recordings_api, app_module):
        monkeypatch.setattr(module, "ENABLE_INTERNAL_SHARING", True)
    monkeypatch.setattr(recordings_api, "SHOW_USERNAMES_IN_UI", show)
    client = app.test_client()
    with app.app_context():
        _login(client, db.session.get(User, world["me"]))
    detail = client.get(f"/api/recordings/{world['theirs']}")
    assert detail.status_code == 200, detail.get_json()
    listing = client.get("/api/recordings", query_string={"shared": "true", "per_page": 100})
    assert listing.status_code == 200
    [row] = [r for r in listing.get_json()["recordings"] if r["id"] == world["theirs"]]
    return detail, row


def test_the_detail_and_the_list_hide_the_owner_name_with_usernames_hidden(world, monkeypatch):
    detail, row = _shared_views(world, monkeypatch, show=False)
    for view in (detail.get_json(), row):
        assert view["is_shared"] is True and view["is_owner"] is False
        assert view["owner_username"] is None
        assert view["share_info"]["owner_username"] is None
        assert view["share_info"]["can_edit"] is False
    with app.app_context():
        owner_name = db.session.get(User, world["ana"]).username
    assert owner_name not in detail.get_data(as_text=True)


def test_the_detail_and_the_list_give_the_owner_name_with_usernames_shown(world, monkeypatch):
    detail, row = _shared_views(world, monkeypatch, show=True)
    for view in (detail.get_json(), row):
        assert view["owner_username"].startswith("ana_")
        assert view["share_info"]["owner_username"].startswith("ana_")
