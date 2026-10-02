"""Webhook signature V2, rotation grace and recording.updated from every path
(mailr spec section 5: W1 to W4).

SHARED-DB: users, tokens, tags, webhooks, deliveries and the recording are
removed afterwards.
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (APIToken, Recording, RecordingTag, Speaker, Tag, User, Webhook,
                        WebhookDelivery)
from src.services import webhook_dispatch as wd
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False
SEGMENTS = [{"speaker": "SPEAKER_00", "sentence": "Launch moves to Q3.", "start_time": 0.0, "end_time": 2.0}]


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"wh_{s}", email=f"wh_{s}@local.test", password="x")
        db.session.add(me)
        db.session.commit()
        rec = Recording(user_id=me.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        transcription=json.dumps(SEGMENTS))
        tag = Tag(name=f"t-{s}", user_id=me.id)
        hook = Webhook(user_id=me.id, name="mailr", url="https://example.com/hook", secret="a" * 40,
                       events=json.dumps(["recording.updated", "recording.transcription.completed"]))
        plain = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add_all([rec, tag, hook, APIToken(user_id=me.id, token_hash=hash_token(plain), name="wh")])
        db.session.commit()
        ids = dict(me=me.id, rec=rec.id, tag=tag.id, hook=hook.id, token=plain)
    yield ids
    with app.app_context():
        db.session.rollback()
        WebhookDelivery.query.filter_by(webhook_id=ids["hook"]).delete()
        Webhook.query.filter_by(user_id=ids["me"]).delete()
        RecordingTag.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(user_id=ids["me"]).delete()
        Tag.query.filter_by(user_id=ids["me"]).delete()
        Speaker.query.filter_by(user_id=ids["me"]).delete()
        APIToken.query.filter_by(user_id=ids["me"]).delete()
        User.query.filter_by(id=ids["me"]).delete()
        db.session.commit()


def _updates(world):
    with app.app_context():
        rows = (WebhookDelivery.query.filter_by(webhook_id=world["hook"], event_type="recording.updated")
                .order_by(WebhookDelivery.id).all())
        return [json.loads(d.payload) for d in rows]


def _session(uid):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


def _verify_v2(header, secret, body, now, window=300):
    parts = [p.split("=", 1) for p in header.split(",")]
    t = int(next(v for k, v in parts if k == "t"))
    sigs = [v for k, v in parts if k == "v1"]
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return abs(now - t) <= window and any(hmac.compare_digest(expected, s) for s in sigs)


# ------------------------------------------------------------ W1, W2

def _send(world, delivery_id, at):
    sent = {}

    def fake_post(url, data=None, headers=None, **kw):
        sent.update(body=data, headers=headers)
        return MagicMock(status_code=200, text="ok")

    with app.app_context(), patch.object(wd, "_http_post", side_effect=fake_post), \
            patch.object(wd, "is_url_safe_for_webhook", return_value=(True, "")), patch("time.time", return_value=at):
        d = db.session.get(WebhookDelivery, delivery_id)
        wd._post_delivery(d, db.session.get(Webhook, world["hook"]))
    return sent


def _test_fire(world):
    resp = _session(world["me"]).post(f"/api/v1/webhooks/{world['hook']}/test")
    assert resp.status_code == 202
    return resp.get_json()["id"]


def test_v2_signs_the_wire_bytes_and_the_send_time(world):
    delivery_id = _test_fire(world)
    first = _send(world, delivery_id, 1_790_000_000)
    with app.app_context():
        d = db.session.get(WebhookDelivery, delivery_id)
        d.attempt_count = 2
        db.session.commit()
    third = _send(world, delivery_id, 1_790_000_600)
    assert first["body"] == third["body"]                          # same bytes every attempt
    assert first["headers"]["Speakr-Signature-V2"].startswith("t=1790000000,")
    assert third["headers"]["Speakr-Signature-V2"].startswith("t=1790000600,")
    assert third["headers"]["Speakr-Attempt"] == "3"
    for sent, at in ((first, 1_790_000_000), (third, 1_790_000_600)):
        assert _verify_v2(sent["headers"]["Speakr-Signature-V2"], "a" * 40, sent["body"], at)
        assert not _verify_v2(sent["headers"]["Speakr-Signature-V2"], "a" * 40, sent["body"], at + 301)
    v1 = "sha256=" + hmac.new(("a" * 40).encode(), first["body"], hashlib.sha256).hexdigest()
    assert first["headers"]["Speakr-Signature"] == v1                # unchanged scheme
    envelope = json.loads(first["body"])
    assert envelope["occurred_at"] == envelope["timestamp"]


def test_replay_carries_v2(world):
    original = _test_fire(world)
    resp = _session(world["me"]).post(f"/api/v1/webhooks/{world['hook']}/deliveries/{original}/replay")
    assert resp.status_code == 202
    sent = _send(world, resp.get_json()["id"], 1_790_000_000)
    assert _verify_v2(sent["headers"]["Speakr-Signature-V2"], "a" * 40, sent["body"], 1_790_000_000)
    envelope = json.loads(sent["body"])
    assert envelope["replayed_from"] and envelope["occurred_at"] == envelope["timestamp"]


def test_rotation_keeps_the_old_secret_for_the_grace_period(world):
    resp = _session(world["me"]).post(f"/api/v1/webhooks/{world['hook']}/rotate-secret")
    assert resp.status_code == 200
    new_secret = resp.get_json()["secret"]
    assert new_secret != "a" * 40 and resp.get_json()["previous_secret_valid_until"]
    assert "previous_secret" not in resp.get_json()
    delivery_id = _test_fire(world)
    sent = _send(world, delivery_id, 1_790_000_000)
    header = sent["headers"]["Speakr-Signature-V2"]
    assert header.count("v1=") == 2
    assert _verify_v2(header, new_secret, sent["body"], 1_790_000_000)
    assert _verify_v2(header, "a" * 40, sent["body"], 1_790_000_000)
    assert sent["headers"]["Speakr-Signature"] == "sha256=" + hmac.new(
        new_secret.encode(), sent["body"], hashlib.sha256).hexdigest()

    with app.app_context():
        hook = db.session.get(Webhook, world["hook"])
        hook.previous_secret_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
    after = _send(world, delivery_id, 1_790_000_000)["headers"]["Speakr-Signature-V2"]
    assert after.count("v1=") == 1


# ------------------------------------------------------------ W3, W4

def _api(world):
    return {"Authorization": f"Bearer {world['token']}"}


@pytest.mark.parametrize("action,fields", [
    ("save_title", ["title"]),
    ("tag_add", ["tags"]),
    ("put_notes", ["notes"]),
    ("speaker_rename", ["participants", "transcript"]),   # voice profiles stubbed: no label map
    ("v1_patch", ["title"]),
])
def test_each_edit_fires_one_recording_updated(world, action, fields):
    c = _session(world["me"])
    with patch("src.api.recordings.reindex_recording_chunks_async"), patch("src.file_exporter.export_recording"), \
            patch("src.services.speaker.update_voice_profiles", return_value=(0, 0)):
        if action == "save_title":
            resp = c.post("/save", json={"id": world["rec"], "title": "Saved"})
        elif action == "tag_add":
            resp = c.post(f"/api/recordings/{world['rec']}/tags", json={"tag_id": world["tag"]})
        elif action == "put_notes":
            resp = c.put(f"/api/v1/recordings/{world['rec']}/notes", json={"notes": "n"}, headers=_api(world))
        elif action == "speaker_rename":
            resp = c.post(f"/recording/{world['rec']}/update_speakers",
                          json={"speaker_map": {"SPEAKER_00": {"name": "Jane", "isMe": False}}})
        else:
            resp = c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Patched"}, headers=_api(world))
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:300]
    updates = _updates(world)
    assert len(updates) == 1, [u["data"] for u in updates]
    data = updates[0]["data"]
    assert data["recording_id"] == world["rec"] and data["fields_changed"] == fields
    assert data["updated_at"].endswith("Z")


def test_processing_status_changes_fire_no_update(world):
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        r.status = "SUMMARIZING"
        db.session.commit()
        r.status = "COMPLETED"
        r.completed_at = datetime.utcnow()
        db.session.commit()
    assert _updates(world) == []


def test_quick_note_saves_merge_into_one_delivery(world):
    c = app.test_client()
    for text in ("first", "second", "third"):
        assert c.put(f"/api/v1/recordings/{world['rec']}/notes", json={"notes": text},
                     headers=_api(world)).status_code == 200
    updates = _updates(world)
    assert len(updates) == 1 and updates[0]["data"]["fields_changed"] == ["notes"]


def test_lifecycle_events_carry_updated_at(world):
    with app.app_context():
        wd.emit_webhook_event(user_id=world["me"], event_type="recording.transcription.completed",
                              data={"recording_id": world["rec"], "title": "Sync"})
        d = WebhookDelivery.query.filter_by(webhook_id=world["hook"],
                                            event_type="recording.transcription.completed").one()
        assert json.loads(d.payload)["data"]["updated_at"].endswith("Z")
