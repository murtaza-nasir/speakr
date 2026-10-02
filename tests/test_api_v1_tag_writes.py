"""Tag writes in API v1 (mailr spec section 8).

POST needs edit access; PUT sets the tags exactly; a group tag that shares
the recording shares it the same way as the web app, and a scoped token
needs the share scope for that. GET /tags?name= finds a tag by name.

SHARED-DB: users, group, tags, shares, tokens, webhooks and the recording
are removed afterwards.
"""

import json
import os
import secrets
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (APIToken, Group, GroupMembership, InternalShare, Recording, RecordingTag,
                        SharedRecordingState, Tag, User, Webhook, WebhookDelivery)
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"tw_{s}", email=f"tw_{s}@local.test", password="x")
        viewer = User(username=f"twv_{s}", email=f"twv_{s}@local.test", password="x")
        member = User(username=f"twm_{s}", email=f"twm_{s}@local.test", password="x")
        db.session.add_all([owner, viewer, member])
        db.session.commit()
        group = Group(name=f"team-{s}")
        db.session.add(group)
        db.session.commit()
        db.session.add_all([GroupMembership(group_id=group.id, user_id=owner.id, role="admin"),
                            GroupMembership(group_id=group.id, user_id=member.id, role="member")])
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav")
        a = Tag(name=f"Followed up {s}", user_id=owner.id)
        b = Tag(name=f"b-{s}", user_id=owner.id)
        c = Tag(name=f"c-{s}", user_id=owner.id)
        foreign = Tag(name=f"f-{s}", user_id=viewer.id)
        team = Tag(name=f"team-tag-{s}", user_id=owner.id, group_id=group.id, auto_share_on_apply=True)
        db.session.add_all([rec, a, b, c, foreign, team])
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=viewer.id,
                                     can_edit=False))
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=a.id, order=1))
        tokens = {}
        for key, user, scopes in (("full", owner, None), ("write", owner, ["read", "write"]),
                                  ("share", owner, ["read", "write", "share"]), ("viewer", viewer, None)):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=user.id, token_hash=hash_token(p), name=key,
                                    scopes=None if scopes is None else json.dumps(scopes)))
            tokens[key] = p
        db.session.commit()
        ids = dict(owner=owner.id, viewer=viewer.id, member=member.id, group=group.id, rec=rec.id, a=a.id,
                   b=b.id, c=c.id, foreign=foreign.id, team=team.id, tokens=tokens, suffix=s)
    with patch("src.app.ENABLE_INTERNAL_SHARING", True), patch("src.api.recordings.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["owner"], ids["viewer"], ids["member"]]
        hooks = [w.id for w in Webhook.query.filter(Webhook.user_id.in_(users))]
        WebhookDelivery.query.filter(WebhookDelivery.webhook_id.in_(hooks)).delete(synchronize_session=False)
        Webhook.query.filter(Webhook.id.in_(hooks)).delete(synchronize_session=False)
        RecordingTag.query.filter_by(recording_id=ids["rec"]).delete()
        SharedRecordingState.query.filter_by(recording_id=ids["rec"]).delete()
        InternalShare.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        Tag.query.filter(Tag.user_id.in_(users)).delete(synchronize_session=False)
        GroupMembership.query.filter_by(group_id=ids["group"]).delete()
        Group.query.filter_by(id=ids["group"]).delete()
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _h(world, key="full"):
    return {"Authorization": f"Bearer {world['tokens'][key]}"}


def _tags(world):
    with app.app_context():
        return [rt.tag_id for rt in RecordingTag.query.filter_by(recording_id=world["rec"]).order_by(RecordingTag.order)]


def _shared_with(world):
    with app.app_context():
        return {(s.shared_with_user_id, s.source_type) for s in InternalShare.query.filter_by(recording_id=world["rec"])}


def test_a_view_only_recipient_cannot_add_tags(world):
    with app.test_client() as c:
        resp = c.post(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["foreign"]]},
                      headers=_h(world, "viewer"))
    assert resp.status_code == 403 and world["foreign"] not in _tags(world)


def test_put_replaces_in_order_and_reports(world):
    with app.test_client() as c:
        resp = c.put(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["c"], world["b"]]},
                     headers=_h(world, "write"))
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert [t["id"] for t in body["tags"]] == [world["c"], world["b"]]
    assert sorted(body["added"]) == sorted([world["b"], world["c"]]) and body["removed"] == [world["a"]]
    assert _tags(world) == [world["c"], world["b"]]


def test_put_with_a_foreign_tag_changes_nothing(world):
    with app.test_client() as c:
        resp = c.put(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["b"], world["foreign"], 999999]},
                     headers=_h(world))
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_parameter"
    assert sorted(resp.get_json()["tag_ids"]) == sorted([world["foreign"], 999999])
    assert _tags(world) == [world["a"]]


@pytest.mark.parametrize("route", ["web", "v1_post", "v1_put"])
def test_a_group_tag_shares_the_same_way_everywhere(world, route):
    if route == "web":
        c = _Client(app, app.response_class, use_cookies=True)
        with c.session_transaction() as sess:
            sess["_user_id"] = str(world["owner"])
        resp = c.post(f"/api/recordings/{world['rec']}/tags", json={"tag_id": world["team"]})
    else:
        with app.test_client() as c:
            if route == "v1_post":
                resp = c.post(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["team"]]}, headers=_h(world, "share"))
            else:
                resp = c.put(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["a"], world["team"]]},
                             headers=_h(world, "share"))
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert (world["member"], "group_tag") in _shared_with(world)
    with app.app_context():
        share = InternalShare.query.filter_by(recording_id=world["rec"], shared_with_user_id=world["member"]).one()
        assert share.can_edit is False and share.source_tag_id == world["team"]
        assert SharedRecordingState.query.filter_by(recording_id=world["rec"], user_id=world["member"]).count() == 1


@pytest.mark.parametrize("method", ["post", "put"])
def test_a_write_token_without_share_cannot_share_by_tagging(world, method):
    with app.test_client() as c:
        if method == "post":
            resp = c.post(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["team"]]}, headers=_h(world, "write"))
        else:
            resp = c.put(f"/api/v1/recordings/{world['rec']}/tags", json={"tag_ids": [world["team"]]}, headers=_h(world, "write"))
    assert resp.status_code == 403 and resp.get_json()["code"] == "insufficient_scope"
    assert resp.get_json()["required_scopes"] == ["share"]
    assert _tags(world) == [world["a"]] and (world["member"], "group_tag") not in _shared_with(world)


@pytest.mark.parametrize("call", ["post", "put", "delete"])
def test_each_route_moves_updated_at_and_fires_a_webhook(world, call):
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        r.updated_at = datetime(2020, 1, 1)
        hook = Webhook(user_id=world["owner"], name="m", url="https://example.com/h", secret="s" * 40,
                       events=json.dumps(["recording.updated"]))
        db.session.add(hook)
        db.session.commit()
        hook_id = hook.id
    with app.test_client() as c:
        url = f"/api/v1/recordings/{world['rec']}/tags"
        if call == "post":
            resp = c.post(url, json={"tag_ids": [world["b"]]}, headers=_h(world))
        elif call == "put":
            resp = c.put(url, json={"tag_ids": [world["b"]]}, headers=_h(world))
        else:
            resp = c.delete(f"{url}/{world['a']}", headers=_h(world))
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Recording, world["rec"]).updated_at > datetime(2025, 1, 1)
        fields = [json.loads(d.payload)["data"]["fields_changed"] for d in WebhookDelivery.query.filter_by(webhook_id=hook_id)]
    assert fields == [["tags"]]


def test_find_a_tag_by_name(world):
    with app.test_client() as c:
        found = c.get(f"/api/v1/tags?name=followed UP {world['suffix']}", headers=_h(world, "write")).get_json()["tags"]
        none = c.get("/api/v1/tags?name=nothing-like-this", headers=_h(world, "write")).get_json()["tags"]
    assert [t["id"] for t in found] == [world["a"]] and none == []
