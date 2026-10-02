"""Recipients' webhooks for shared recordings (mailr spec W6).

A webhook with include_shared receives the recording.* events of recordings
shared with its owner, with data.owner_user_id; without it, nothing.

SHARED-DB: users, shares, refs, webhooks, deliveries and the recording are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (InternalShare, Recording, RecordingExternalRef, User, Webhook, WebhookDelivery)
from src.services import webhook_dispatch as wd


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"ws_{s}", email=f"ws_{s}@local.test", password="x")
        guest = User(username=f"wsg_{s}", email=f"wsg_{s}@local.test", password="x")
        db.session.add_all([owner, guest])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav")
        db.session.add(rec)
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=guest.id))
        db.session.add_all([RecordingExternalRef(recording_id=rec.id, user_id=owner.id, system="mailr", kind="event", ref="owner-ref"),
                            RecordingExternalRef(recording_id=rec.id, user_id=guest.id, system="mailr", kind="event", ref="guest-ref")])
        db.session.commit()      # before the webhooks exist
        events = json.dumps(["recording.updated", "recording.summary.completed"])
        hooks = {
            "owner": Webhook(user_id=owner.id, name="o", url="https://example.com/o", secret="s" * 40, events=events),
            "guest_in": Webhook(user_id=guest.id, name="g", url="https://example.com/g", secret="s" * 40, events=events,
                                include_shared=True),
            "guest_out": Webhook(user_id=guest.id, name="g2", url="https://example.com/g2", secret="s" * 40, events=events),
        }
        db.session.add_all(hooks.values())
        db.session.commit()
        ids = dict(owner=owner.id, guest=guest.id, rec=rec.id, hooks={k: h.id for k, h in hooks.items()})
    with patch("src.app.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        WebhookDelivery.query.filter(WebhookDelivery.webhook_id.in_(ids["hooks"].values())).delete(synchronize_session=False)
        Webhook.query.filter(Webhook.id.in_(ids["hooks"].values())).delete(synchronize_session=False)
        RecordingExternalRef.query.filter_by(recording_id=ids["rec"]).delete()
        InternalShare.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        User.query.filter(User.id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        db.session.commit()


def _payloads(world, key):
    with app.app_context():
        return [json.loads(d.payload) for d in WebhookDelivery.query.filter_by(webhook_id=world["hooks"][key])]


def test_a_recipient_webhook_gets_shared_events_when_asked(world):
    with app.app_context():
        wd.emit_webhook_event(user_id=world["owner"], event_type="recording.summary.completed",
                              data={"recording_id": world["rec"], "title": "Sync"})
    owner = _payloads(world, "owner")
    guest = _payloads(world, "guest_in")
    assert len(owner) == 1 and "owner_user_id" not in owner[0]["data"]
    assert [r["ref"] for r in owner[0]["data"]["external_refs"]] == ["owner-ref"]
    assert len(guest) == 1 and guest[0]["data"]["owner_user_id"] == world["owner"]
    assert guest[0]["user_id"] == world["guest"]
    assert [r["ref"] for r in guest[0]["data"]["external_refs"]] == ["guest-ref"]   # each sees only their own
    assert _payloads(world, "guest_out") == []


def test_an_edit_reaches_the_recipient_too(world):
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        r.title = "Renamed"
        db.session.commit()
    assert [p["data"]["fields_changed"] for p in _payloads(world, "guest_in")] == [["title"]]


def test_no_sharing_no_recipient_events(world):
    with patch("src.app.ENABLE_INTERNAL_SHARING", False), app.app_context():
        wd.emit_webhook_event(user_id=world["owner"], event_type="recording.summary.completed",
                              data={"recording_id": world["rec"], "title": "Sync"})
    assert len(_payloads(world, "owner")) == 1 and _payloads(world, "guest_in") == []


def test_include_shared_is_set_through_the_api(world):
    from flask import g, has_app_context
    from flask.testing import FlaskClient

    class _Client(FlaskClient):
        def open(self, *args, **kwargs):
            if has_app_context():
                g.pop('_login_user', None)
            return super().open(*args, **kwargs)

    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(world["guest"])
    resp = c.patch(f"/api/v1/webhooks/{world['hooks']['guest_out']}", json={"include_shared": True})
    assert resp.status_code == 200 and resp.get_json()["include_shared"] is True


def test_a_recipients_reference_tells_the_owner_nothing(world):
    with app.app_context():
        db.session.add(RecordingExternalRef(recording_id=world["rec"], user_id=world["guest"], system="mailr",
                                            kind="conversation", ref="guest-2"))
        db.session.commit()
    assert _payloads(world, "owner") == [] and _payloads(world, "guest_in") == []
    with app.app_context():
        db.session.add(RecordingExternalRef(recording_id=world["rec"], user_id=world["owner"], system="mailr",
                                            kind="conversation", ref="owner-2"))
        db.session.commit()
    assert [p["data"]["fields_changed"] for p in _payloads(world, "owner")] == [["external_refs"]]

