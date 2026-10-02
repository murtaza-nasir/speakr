"""External references and the new upload fields (mailr spec G8).

A reference links a recording to an item in another system and belongs to
one user: the owner and a share recipient each see only their own. The API
upload takes participants, references, an idempotency key and strict mode.

SHARED-DB: users, tokens, tags, folders, shares, webhooks, recordings and
stored media are removed afterwards.
"""

import io
import json
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (APIToken, Folder, InternalShare, Recording, RecordingExternalRef, RecordingTag, Tag,
                        User, Webhook, WebhookDelivery)
from src.utils.token_auth import hash_token

EVENT = {"system": "mailr", "kind": "event", "ref": "5f1c@example.com@2026-10-06T15:00:00Z",
         "url": "https://mail.example.com/calendar/event/5f1c", "label": "Weekly sync (6 Oct)"}


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"xr_{s}", email=f"xr_{s}@local.test", password="x")
        guest = User(username=f"xrg_{s}", email=f"xrg_{s}@local.test", password="x")
        db.session.add_all([owner, guest])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav")
        mine = Tag(name=f"mine-{s}", user_id=owner.id)
        theirs = Tag(name=f"theirs-{s}", user_id=guest.id)
        folder = Folder(name=f"f-{s}", user_id=owner.id)
        db.session.add_all([rec, mine, theirs, folder])
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=guest.id, can_edit=True))
        tokens = {}
        for who in (owner, guest):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=who.id, token_hash=hash_token(p), name="xr"))
            tokens[who.id] = p
        db.session.commit()
        ids = dict(owner=owner.id, guest=guest.id, rec=rec.id, mine=mine.id, theirs=theirs.id, folder=folder.id,
                   tokens=tokens)
    with patch("src.app.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["owner"], ids["guest"]]
        recs = [r.id for r in Recording.query.filter(Recording.user_id.in_(users))]
        from src.services.storage import get_storage_service
        for r in Recording.query.filter(Recording.id.in_(recs)):
            if r.audio_path:
                try:
                    get_storage_service().delete(r.audio_path, missing_ok=True)
                except Exception:
                    pass
        RecordingExternalRef.query.filter(RecordingExternalRef.user_id.in_(users)).delete(synchronize_session=False)
        RecordingTag.query.filter(RecordingTag.recording_id.in_(recs)).delete(synchronize_session=False)
        InternalShare.query.filter(InternalShare.recording_id.in_(recs)).delete(synchronize_session=False)
        hooks = [w.id for w in Webhook.query.filter(Webhook.user_id.in_(users))]
        WebhookDelivery.query.filter(WebhookDelivery.webhook_id.in_(hooks)).delete(synchronize_session=False)
        Webhook.query.filter(Webhook.id.in_(hooks)).delete(synchronize_session=False)
        from src.models import ProcessingJob
        ProcessingJob.query.filter(ProcessingJob.recording_id.in_(recs)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        Tag.query.filter(Tag.user_id.in_(users)).delete(synchronize_session=False)
        Folder.query.filter(Folder.user_id.in_(users)).delete(synchronize_session=False)
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _h(world, who="owner"):
    return {"Authorization": f"Bearer {world['tokens'][world[who]]}"}


def _url(world, suffix=""):
    return f"/api/v1/recordings/{world['rec']}/external-refs{suffix}"


# ------------------------------------------------------------ refs

def test_create_is_idempotent_and_listed(world):
    with app.test_client() as c:
        first = c.post(_url(world), json=EVENT, headers=_h(world))
        again = c.post(_url(world), json=EVENT, headers=_h(world))
        listed = c.get(_url(world), headers=_h(world)).get_json()["external_refs"]
        detail = c.get(f"/api/v1/recordings/{world['rec']}", headers=_h(world)).get_json()
    assert first.status_code == 201 and again.status_code == 200
    assert again.get_json()["id"] == first.get_json()["id"]
    assert [r["ref"] for r in listed] == [EVENT["ref"]] and listed[0]["created_at"].endswith("Z")
    assert [r["id"] for r in detail["external_refs"]] == [first.get_json()["id"]]


def test_replace_by_system_and_delete(world):
    with app.test_client() as c:
        c.post(_url(world), json=dict(EVENT, system="other"), headers=_h(world))
        c.post(_url(world), json=EVENT, headers=_h(world))
        replaced = c.put(_url(world, "?system=mailr"), headers=_h(world), json={"external_refs": [
            {"system": "mailr", "kind": "conversation", "ref": "conv-1"},
            {"system": "mailr", "kind": "conversation", "ref": "conv-1"}]})
        refs = replaced.get_json()["external_refs"]
        assert replaced.status_code == 200
        assert sorted((r["system"], r["ref"]) for r in refs) == [("mailr", "conv-1"), ("other", EVENT["ref"])]
        mixed = c.put(_url(world, "?system=mailr"), headers=_h(world),
                      json={"external_refs": [dict(EVENT, system="other")]})
        assert mixed.status_code == 400
        conv = next(r for r in refs if r["ref"] == "conv-1")
        assert c.delete(_url(world, f"/{conv['id']}"), headers=_h(world)).status_code == 204
        assert c.delete(_url(world, f"/{conv['id']}"), headers=_h(world)).status_code == 404


def test_list_filter_by_ref(world):
    with app.test_client() as c:
        c.post(_url(world), json=EVENT, headers=_h(world))
        found = c.get(f"/api/v1/recordings?external_system=mailr&external_ref={EVENT['ref']}",
                      headers=_h(world)).get_json()["recordings"]
        kind = c.get(f"/api/v1/recordings?external_system=mailr&external_ref={EVENT['ref']}&external_kind=conversation",
                     headers=_h(world)).get_json()["recordings"]
        lone = c.get("/api/v1/recordings?external_system=mailr", headers=_h(world))
    assert [r["id"] for r in found] == [world["rec"]] and found[0]["external_refs"][0]["ref"] == EVENT["ref"]
    assert kind == [] and lone.status_code == 400


def test_refs_are_private_per_user(world):
    with app.test_client() as c:
        assert c.post(_url(world), json=EVENT, headers=_h(world)).status_code == 201
        assert c.get(_url(world), headers=_h(world, "guest")).get_json()["external_refs"] == []
        mine = c.post(_url(world), json=dict(EVENT, ref="guest-own"), headers=_h(world, "guest")).get_json()
        assert [r["ref"] for r in c.get(_url(world), headers=_h(world)).get_json()["external_refs"]] == [EVENT["ref"]]
        owner_ref = c.get(_url(world), headers=_h(world)).get_json()["external_refs"][0]["id"]
        assert c.delete(_url(world, f"/{owner_ref}"), headers=_h(world, "guest")).status_code == 404
        assert mine["ref"] == "guest-own"


def test_the_limit_is_fifty(world):
    with app.app_context():
        for i in range(50):
            db.session.add(RecordingExternalRef(recording_id=world["rec"], user_id=world["owner"],
                                                system="mailr", kind="k", ref=f"r{i}"))
        db.session.commit()
    with app.test_client() as c:
        resp = c.post(_url(world), json=EVENT, headers=_h(world))
    assert resp.status_code == 409 and resp.get_json()["code"] == "conflict"


@pytest.mark.parametrize("bad", [
    dict(EVENT, url="javascript:alert(1)"), dict(EVENT, url="ftp://x"), dict(EVENT, system="Mailr"),
    dict(EVENT, kind=""), dict(EVENT, ref=""), dict(EVENT, ref="x" * 501), dict(EVENT, label="x" * 201),
])
def test_validation(world, bad):
    with app.test_client() as c:
        resp = c.post(_url(world), json=bad, headers=_h(world))
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_parameter"


def test_a_ref_change_moves_updated_at_and_fires_a_webhook(world):
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        r.updated_at = datetime(2020, 1, 1)
        hook = Webhook(user_id=world["owner"], name="m", url="https://example.com/h", secret="s" * 40,
                       events=json.dumps(["recording.updated"]))
        db.session.add(hook)
        db.session.commit()
        hook_id = hook.id
    with app.test_client() as c:
        c.post(_url(world), json=EVENT, headers=_h(world))
    with app.app_context():
        assert db.session.get(Recording, world["rec"]).updated_at > datetime(2025, 1, 1)
        payloads = [json.loads(d.payload)["data"] for d in WebhookDelivery.query.filter_by(webhook_id=hook_id)]
    assert [p["fields_changed"] for p in payloads] == [["external_refs"]]
    assert [x["ref"] for x in payloads[0]["external_refs"]] == [EVENT["ref"]]


# ------------------------------------------------------------ upload

@contextmanager
def _no_media_tools():
    def _convert(filepath, **kwargs):
        r = MagicMock()
        r.output_path = filepath
        r.was_converted = r.was_compressed = False
        r.original_codec = r.final_codec = "opus"
        r.size_reduction_percent = 0.0
        return r
    with patch("src.api.recordings.convert_if_needed", side_effect=_convert), \
            patch("src.api.recordings.get_codec_info",
                  return_value={"has_video": False, "audio_codec": "opus", "video_codec": None, "duration": 5.0}), \
            patch("src.api.recordings.get_duration", return_value=5.0), \
            patch("src.api.recordings.get_creation_date", return_value=None), \
            patch("src.services.job_queue.job_queue.enqueue", return_value=1) as enqueue:
        yield enqueue


def _upload(world, who="owner", payload=b"audio", **fields):
    data = {"file": (io.BytesIO(payload + secrets.token_bytes(8)), "memo.webm")}
    data.update(fields)
    with app.test_client() as c:
        return c.post("/api/v1/recordings/upload", data=data, headers=_h(world, who),
                      content_type="multipart/form-data")


def test_an_upload_with_every_field(world):
    with _no_media_tools():
        resp = _upload(world, title="Weekly sync", participants="Dana, Omar", meeting_date="2026-10-06T15:00:00Z",
                       folder_id=str(world["folder"]), **{"tag_ids[0]": str(world["mine"])},
                       external_refs=json.dumps([EVENT]), idempotency_key="msg-1:part-2")
    body = resp.get_json()
    assert resp.status_code == 202, body
    assert body["ignored"] == {"tag_ids": [], "folder_id": None}
    assert [r["ref"] for r in body["external_refs"]] == [EVENT["ref"]]
    with app.test_client() as c:
        listed = c.get(f"/api/v1/recordings?external_system=mailr&external_ref={EVENT['ref']}",
                       headers=_h(world)).get_json()["recordings"]
    item = next(r for r in listed if r["id"] == body["id"])
    assert (item["title"], item["participants"], item["folder_id"]) == ("Weekly sync", "Dana, Omar", world["folder"])
    assert item["meeting_date"].startswith("2026-10-06T15:00:00") and [t["id"] for t in item["tags"]] == [world["mine"]]


def test_a_repeat_with_the_same_key_creates_nothing(world):
    with _no_media_tools() as enqueue:
        first = _upload(world, idempotency_key="msg-9")
        second = _upload(world, idempotency_key="msg-9")
        assert enqueue.call_count == 1
    assert second.status_code == 200 and second.get_json()["idempotent_replay"] is True
    assert second.get_json()["id"] == first.get_json()["id"]
    with app.app_context():
        assert Recording.query.filter_by(user_id=world["owner"], upload_idempotency_key="msg-9").count() == 1
    with _no_media_tools():
        other_user = _upload(world, who="guest", idempotency_key="msg-9")
    assert other_user.get_json()["id"] != first.get_json()["id"]


def test_an_old_key_does_not_replay(world):
    with _no_media_tools():
        first = _upload(world, idempotency_key="msg-old")
    with app.app_context():
        r = db.session.get(Recording, first.get_json()["id"])
        r.created_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
    with _no_media_tools():
        second = _upload(world, idempotency_key="msg-old")
    assert second.status_code == 202 and second.get_json()["id"] != first.get_json()["id"]


def test_strict_and_lenient_tags(world):
    with _no_media_tools():
        strict = _upload(world, strict="true", **{"tag_ids[0]": str(world["theirs"])})
        lenient = _upload(world, **{"tag_ids[0]": str(world["theirs"]), "tag_ids[1]": str(world["mine"])})
    assert strict.status_code == 400 and strict.get_json()["ignored"]["tag_ids"] == [world["theirs"]]
    assert lenient.status_code == 202 and lenient.get_json()["ignored"]["tag_ids"] == [world["theirs"]]


@pytest.mark.parametrize("fields", [{"participants": "x" * 501}, {"external_refs": "not json"},
                                    {"external_refs": json.dumps([dict(EVENT, url="javascript:x")])},
                                    {"idempotency_key": "k" * 101}])
def test_bad_upload_fields(world, fields):
    with _no_media_tools():
        resp = _upload(world, **fields)
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_parameter"
