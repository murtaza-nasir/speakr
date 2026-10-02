"""Merging speakers renames them in the recordings.

Recordings keep speaker names, not speaker ids. A merge in Speaker Management
moved the voice samples and snippets to the kept speaker, but every recording
of a merged speaker went on showing the old name. Merge and rename now share
rename_speaker_in_recordings, and both rebuild the Inquire chunks and rewrite
the exports of the recordings they change, and send recording.updated for
each (mailr spec, note of 1 Oct 2026).

SHARED-DB: users, speakers, recordings and chunks are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, Speaker, TranscriptChunk, User, Webhook, WebhookDelivery

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _segments(*names):
    return json.dumps([{"speaker": n, "sentence": f"{n} spoke.", "start_time": float(i), "end_time": i + 1.0}
                       for i, n in enumerate(names)])


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"mrg_{s}", email=f"mrg_{s}@local.test", password="x")
        other = User(username=f"mrgo_{s}", email=f"mrgo_{s}@local.test", password="x")
        db.session.add_all([me, other])
        db.session.commit()
        keep = Speaker(name="Murtaza", user_id=me.id, use_count=3)
        old = Speaker(name="Murtaza Nasir", user_id=me.id, use_count=2)
        bob = Speaker(name="Bob", user_id=me.id, use_count=1)
        db.session.add_all([keep, old, bob])
        only_old = Recording(user_id=me.id, title="A", status="COMPLETED", original_filename="a.wav",
                             transcription=_segments("Murtaza Nasir", "Bob"), participants="Bob, Murtaza Nasir",
                             speaker_label_map={"SPEAKER_00": "Murtaza Nasir", "SPEAKER_01": "Bob"})
        both = Recording(user_id=me.id, title="B", status="COMPLETED", original_filename="b.wav",
                         transcription=_segments("murtaza nasir", "Murtaza"), participants="Murtaza, Murtaza Nasir")
        untouched = Recording(user_id=me.id, title="C", status="COMPLETED", original_filename="c.wav",
                              transcription=_segments("Bob"), participants="Bob")
        theirs = Recording(user_id=other.id, title="D", status="COMPLETED", original_filename="d.wav",
                           transcription=_segments("Murtaza Nasir"), participants="Murtaza Nasir")
        db.session.add_all([only_old, both, untouched, theirs])
        db.session.commit()
        chunk = TranscriptChunk(recording_id=only_old.id, user_id=me.id, chunk_index=0,
                                content="Murtaza Nasir: Murtaza Nasir spoke.", speaker_name="Murtaza Nasir")
        db.session.add(chunk)
        db.session.commit()
        ids = dict(me=me.id, other=other.id, keep=keep.id, old=old.id, bob=bob.id, only_old=only_old.id,
                   both=both.id, untouched=untouched.id, theirs=theirs.id, chunk=chunk.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        TranscriptChunk.query.filter(TranscriptChunk.user_id.in_([ids["me"], ids["other"]])).delete(
            synchronize_session=False)
        Recording.query.filter(Recording.user_id.in_([ids["me"], ids["other"]])).delete(synchronize_session=False)
        Speaker.query.filter(Speaker.user_id.in_([ids["me"], ids["other"]])).delete(synchronize_session=False)
        User.query.filter(User.id.in_([ids["me"], ids["other"]])).delete(synchronize_session=False)
        db.session.commit()


def _client(uid):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


def _effects():
    return (patch("src.api.recordings.reindex_recording_chunks_async"),
            patch("src.file_exporter.ENABLE_AUTO_EXPORT", True),
            patch("src.file_exporter.export_recording"))


def _speakers(rid):
    rec = db.session.get(Recording, rid)
    return [seg["speaker"] for seg in json.loads(rec.transcription)], rec.participants, rec.speaker_label_map


def test_a_merge_renames_the_merged_speaker_in_every_recording(world):
    with app.app_context():
        hook = Webhook(user_id=world["me"], name="t", url="https://example.com/h", secret="s" * 32,
                       events=json.dumps(["recording.updated"]))
        db.session.add(hook)
        db.session.commit()
        hook_id = hook.id
    reindex, flag, export = _effects()
    with reindex as m_reindex, flag, export as m_export:
        resp = _client(world["me"]).post("/speakers/merge",
                                         json={"target_id": world["keep"], "source_ids": [world["old"]]})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["recordings_updated"] == 2
    with app.app_context():
        updates = {}
        for d in WebhookDelivery.query.filter_by(webhook_id=hook_id, event_type="recording.updated"):
            data = json.loads(d.payload)["data"]
            updates[data["recording_id"]] = data["fields_changed"]
        WebhookDelivery.query.filter_by(webhook_id=hook_id).delete()
        db.session.delete(db.session.get(Webhook, hook_id))
        db.session.commit()
    assert updates == {world["only_old"]: ["participants", "speakers", "transcript"],
                       world["both"]: ["participants", "transcript"]}
    with app.app_context():
        segs, parts, label_map = _speakers(world["only_old"])
        assert segs == ["Murtaza", "Bob"] and parts == "Bob, Murtaza"
        assert label_map == {"SPEAKER_00": "Murtaza", "SPEAKER_01": "Bob"}
        segs, parts, _ = _speakers(world["both"])
        assert segs == ["Murtaza", "Murtaza"] and parts == "Murtaza"          # no duplicate
        assert db.session.get(TranscriptChunk, world["chunk"]).speaker_name == "Murtaza"
        assert _speakers(world["untouched"])[:2] == (["Bob"], "Bob")
        assert _speakers(world["theirs"])[:2] == (["Murtaza Nasir"], "Murtaza Nasir")   # another user
        assert db.session.get(Speaker, world["old"]) is None
    changed = sorted([world["only_old"], world["both"]])
    assert sorted(c.args[0] for c in m_reindex.call_args_list) == changed
    assert sorted(c.args[0] for c in m_export.call_args_list) == changed


def test_a_merge_preview_changes_nothing(world):
    resp = _client(world["me"]).post("/speakers/merge", json={"target_id": world["keep"],
                                                             "source_ids": [world["old"]], "preview": True})
    assert resp.status_code == 200
    with app.app_context():
        assert _speakers(world["only_old"])[0] == ["Murtaza Nasir", "Bob"]


def test_a_rename_reaches_every_recording_and_rebuilds_their_chunks(world):
    reindex, flag, export = _effects()
    with reindex as m_reindex, flag, export:
        resp = _client(world["me"]).put(f"/speakers/{world['old']}", json={"name": "M. Nasir"})
    assert resp.status_code == 200 and resp.get_json()["recordings_updated"] == 2
    with app.app_context():
        assert _speakers(world["only_old"])[0] == ["M. Nasir", "Bob"]
        assert _speakers(world["both"])[0] == ["M. Nasir", "Murtaza"]
        assert _speakers(world["theirs"])[0] == ["Murtaza Nasir"]
    assert sorted(c.args[0] for c in m_reindex.call_args_list) == sorted([world["only_old"], world["both"]])
