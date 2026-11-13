"""Deleting an event is an edit of the recording (#598).

DELETE /api/event/<id> checked view access only, so a recipient of a
view-only share deleted the owner's calendar event for everyone. Reading the
events and downloading them as ICS stay open to that recipient.

SHARED-DB: the users, recordings, shares and events are removed afterwards.
"""

import os
import secrets
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
from src.app import app, db
from src.models import Event, InternalShare, Recording, SharedRecordingState, User

from tests.test_folder_group_shares import _client

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(app_module, "ENABLE_INTERNAL_SHARING", True)
    with app.app_context():
        s = secrets.token_hex(4)
        owner, viewer, editor, outsider = (User(username=f"ed{k}_{s}", email=f"ed{k}_{s}@local.test", password="x")
                                           for k in ("o", "v", "e", "x"))
        db.session.add_all([owner, viewer, editor, outsider])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav")
        db.session.add(rec)
        db.session.commit()
        db.session.add_all([
            InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=viewer.id, can_edit=False),
            InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=editor.id, can_edit=True),
        ])
        events = [Event(recording_id=rec.id, title=f"Review {i}", start_datetime=datetime(2026, 11, 20, 9 + i))
                  for i in range(3)]
        db.session.add_all(events)
        db.session.commit()
        yield dict(owner=owner, viewer=viewer, editor=editor, outsider=outsider, rec=rec.id,
                   events=[e.id for e in events])
        db.session.rollback()
        Event.query.filter_by(recording_id=rec.id).delete()
        InternalShare.query.filter_by(recording_id=rec.id).delete()
        SharedRecordingState.query.filter_by(recording_id=rec.id).delete()
        Recording.query.filter_by(id=rec.id).delete()
        User.query.filter(User.id.in_([owner.id, viewer.id, editor.id, outsider.id])).delete(synchronize_session=False)
        db.session.commit()


def _exists(event_id):
    db.session.expire_all()
    return db.session.get(Event, event_id) is not None


def test_view_only_delete_leaves_the_event(world):
    w = world
    event = w["events"][0]
    resp = _client(w["viewer"]).delete(f"/api/event/{event}")
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "recording_modify_denied"
    assert _exists(event)
    # Reading stays open to the viewer
    viewer = _client(w["viewer"])
    listed = viewer.get(f"/api/recording/{w['rec']}/events")
    assert listed.status_code == 200
    assert event in [e["id"] for e in listed.get_json()["events"]]
    assert viewer.get(f"/api/event/{event}/ics").status_code == 200


def test_outsider_delete_is_not_found(world):
    w = world
    event = w["events"][0]
    resp = _client(w["outsider"]).delete(f"/api/event/{event}")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "event_not_found"
    assert _exists(event)


def test_owner_and_editor_delete_remove_the_event(world):
    w = world
    for user, event in ((w["owner"], w["events"][1]), (w["editor"], w["events"][2])):
        resp = _client(user).delete(f"/api/event/{event}")
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True}
        assert not _exists(event)
    assert _exists(w["events"][0])
