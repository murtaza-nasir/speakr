"""Public share links in API v1 (mailr spec G10) and their webhooks (W5).

SHARED-DB: users, tokens, shares, webhooks and the recording are removed afterwards.
"""

import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, Share, User, Webhook, WebhookDelivery
from src.utils.token_auth import hash_token

@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"ps_{s}", email=f"ps_{s}@local.test", password="x", can_share_publicly=True)
        other = User(username=f"pso_{s}", email=f"pso_{s}@local.test", password="x")
        db.session.add_all([owner, other])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        summary="## Summary", notes="PRIVATE NOTES")
        db.session.add(rec)
        db.session.commit()
        tokens = {}
        for key, user, scopes in (("share", owner, ["read", "share"]), ("write", owner, ["read", "write"]),
                                  ("other", other, None)):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=user.id, token_hash=hash_token(p), name=key, scopes=json.dumps(scopes) if scopes else None))
            tokens[key] = p
        hook = Webhook(user_id=owner.id, name="h", url="https://example.com/h", secret="s" * 40,
                       events=json.dumps(["recording.share.created", "recording.share.revoked"]))
        db.session.add(hook)
        db.session.commit()
        ids = dict(owner=owner.id, other=other.id, rec=rec.id, tokens=tokens, hook=hook.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        WebhookDelivery.query.filter_by(webhook_id=ids["hook"]).delete()
        Webhook.query.filter_by(id=ids["hook"]).delete()
        Share.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        APIToken.query.filter(APIToken.user_id.in_([ids["owner"], ids["other"]])).delete(synchronize_session=False)
        User.query.filter(User.id.in_([ids["owner"], ids["other"]])).delete(synchronize_session=False)
        db.session.commit()


def _post(world, body=None, token="share", secure=True):
    with app.test_client() as c:
        return c.post(f"/api/v1/recordings/{world['rec']}/share", json=body or {},
                      headers={"Authorization": f"Bearer {world['tokens'][token]}"},
                      base_url="https://localhost" if secure else "http://localhost")


def test_create_then_reuse_without_changing_flags(world):
    first = _post(world)
    body = first.get_json()
    assert first.status_code == 201 and body["existing"] is False
    assert (body["share"]["share_summary"], body["share"]["share_notes"]) == (True, False)
    assert body["share_url"].startswith("https://") and "/share/" in body["share_url"]
    again = _post(world, {"share_notes": True})
    assert again.status_code == 200 and again.get_json()["existing"] is True
    assert again.get_json()["flags_differ"] is True and again.get_json()["share"]["share_notes"] is False
    with app.app_context():
        assert Share.query.filter_by(recording_id=world["rec"]).one().share_notes is False


def test_update_existing_and_force_new(world):
    _post(world)
    updated = _post(world, {"share_notes": True, "update_existing": True})
    assert updated.status_code == 200 and updated.get_json()["share"]["share_notes"] is True
    assert updated.get_json()["flags_differ"] is False
    fresh = _post(world, {"force_new": True})
    assert fresh.status_code == 201 and fresh.get_json()["share"]["id"] != updated.get_json()["share"]["id"]
    with app.test_client() as c:
        listed = c.get(f"/api/v1/recordings/{world['rec']}/shares",
                       headers={"Authorization": f"Bearer {world['tokens']['share']}"}).get_json()["shares"]
    assert len(listed) == 2 and all(s["share_url"] for s in listed)


@pytest.mark.parametrize("case,code", [("plain_http", "https_required"), ("off", "feature_disabled"),
                                       ("no_permission", "not_permitted")])
def test_refusals(world, case, code):
    if case == "off":
        with patch.dict(os.environ, {"ENABLE_PUBLIC_SHARING": "false"}):
            resp = _post(world)
    elif case == "no_permission":
        with app.app_context():
            db.session.get(User, world["owner"]).can_share_publicly = False
            db.session.commit()
        resp = _post(world)
    else:
        resp = _post(world, secure=False)
    assert resp.status_code == 403 and resp.get_json()["code"] == code


def test_non_owner_and_scope(world):
    assert _post(world, token="other").status_code == 404
    resp = _post(world, token="write")
    assert resp.status_code == 403 and resp.get_json()["code"] == "insufficient_scope"


def test_revoke_and_webhooks(world):
    share_id = _post(world).get_json()["share"]["id"]
    with app.test_client() as c:
        assert c.delete(f"/api/v1/shares/{share_id}", headers={"Authorization": f"Bearer {world['tokens']['other']}"}).status_code == 404
        assert c.delete(f"/api/v1/shares/{share_id}", headers={"Authorization": f"Bearer {world['tokens']['share']}"}).status_code == 204
    with app.app_context():
        events = [(d.event_type, json.loads(d.payload)["data"]["share_id"])
                  for d in WebhookDelivery.query.filter_by(webhook_id=world["hook"]).order_by(WebhookDelivery.id)]
    assert events == [("recording.share.created", share_id), ("recording.share.revoked", share_id)]


def test_an_expired_link_is_gone(world):
    body = _post(world, {"expires_in_days": 7}).get_json()
    assert body["share"]["expires_at"].endswith("Z")
    path = "/share/" + body["share_url"].rsplit("/share/", 1)[1]
    with app.test_client() as c:
        assert c.get(path).status_code == 200
        with app.app_context():
            share = db.session.get(Share, body["share"]["id"])
            share.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.session.commit()
        assert c.get(path).status_code == 404
    assert _post(world, {"expires_in_days": 0}).status_code == 400
