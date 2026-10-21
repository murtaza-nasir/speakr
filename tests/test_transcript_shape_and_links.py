"""One transcript JSON shape and speaker links (mailr spec G12; speaker ids).

GET /transcript?format=json returns every segment with index, speaker,
speaker_label, speaker_id, text, sentence, start_time and end_time, whatever
key set the transcript was stored with. Named segments store speaker_id, so a
rename or merge follows the saved speaker, and the API shows its current name.

SHARED-DB: the user, token, speakers and recordings are removed afterwards.
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
from src.models import APIToken, Recording, Speaker, User
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False
KEYS = {"index", "speaker", "speaker_label", "speaker_id", "text", "sentence", "start_time", "end_time"}


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"ts_{s}", email=f"ts_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        dana = Speaker(name="Dana", user_id=u.id)
        omar = Speaker(name="Omar Haddad", user_id=u.id)
        db.session.add_all([dana, omar])
        db.session.commit()
        new = Recording(user_id=u.id, title="New keys", status="COMPLETED", original_filename="a.wav",
                        speaker_label_map={"SPEAKER_00": "Dana", "SPEAKER_01": "Omar Haddad"},
                        transcription=json.dumps([
                            {"speaker": "Dana", "sentence": "Budget first.", "start_time": 0.0, "end_time": 3.4},
                            {"speaker": "omar haddad", "sentence": "Agreed.", "start_time": 3.4, "end_time": None},
                            {"speaker": "SPEAKER_02", "sentence": "Who am I?", "start_time": 5.0, "end_time": 6.0},
                            {"speaker": "Visitor", "sentence": "Hello.", "start_time": 7.0, "end_time": 8.0}]))
        old = Recording(user_id=u.id, title="Old keys", status="COMPLETED", original_filename="b.wav",
                        transcription=json.dumps([
                            {"speaker": "Dana", "text": "Old style.", "start": "00:12.5", "end": "0:15"},
                            {"speaker": "Dana", "text": "Second.", "start": 20, "end": 22.5}]))
        plain = Recording(user_id=u.id, title="Plain", status="COMPLETED", original_filename="c.wav",
                          transcription="Just text, no segments.")
        plain_token = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add_all([new, old, plain, APIToken(user_id=u.id, token_hash=hash_token(plain_token), name="ts")])
        db.session.commit()
        ids = dict(user=u.id, dana=dana.id, omar=omar.id, new=new.id, old=old.id, plain=plain.id, token=plain_token)
    yield ids
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(user_id=ids["user"]).delete()
        Speaker.query.filter_by(user_id=ids["user"]).delete()
        APIToken.query.filter_by(user_id=ids["user"]).delete()
        User.query.filter_by(id=ids["user"]).delete()
        db.session.commit()


def _transcript(world, rid, query=""):
    with app.test_client() as c:
        resp = c.get(f"/api/v1/recordings/{rid}/transcript?format=json{query}",
                     headers={"Authorization": f"Bearer {world['token']}"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_every_segment_has_every_key(world):
    for rid in (world["new"], world["old"]):
        body = _transcript(world, rid)
        assert body["kind"] == "segments"
        for seg in body["segments"]:
            assert set(seg) == KEYS
            assert seg["text"] == seg["sentence"] and isinstance(seg["index"], int)
            assert seg["start_time"] is None or isinstance(seg["start_time"], float)


def test_values_are_normalised(world):
    new = _transcript(world, world["new"])["segments"]
    assert [(s["speaker"], s["speaker_id"], s["speaker_label"]) for s in new] == [
        ("Dana", world["dana"], "SPEAKER_00"),
        ("Omar Haddad", world["omar"], "SPEAKER_01"),          # current name, any case in the transcript
        ("SPEAKER_02", None, "SPEAKER_02"),
        ("Visitor", None, None),
    ]
    assert new[1]["end_time"] is None
    old = _transcript(world, world["old"])["segments"]
    assert [(s["text"], s["start_time"], s["end_time"]) for s in old] == [("Old style.", 12.5, 15.0), ("Second.", 20.0, 22.5)]


def test_plain_text(world):
    body = _transcript(world, world["plain"])
    assert body["kind"] == "plain" and body["segments"] == [] and body["raw"] == "Just text, no segments."


def test_window(world):
    body = _transcript(world, world["new"], "&start=3&end=6")
    assert [s["index"] for s in body["segments"]] == [0, 1, 2]
    capped = _transcript(world, world["new"], "&start=0&max_segments=2")
    assert [s["index"] for s in capped["segments"]] == [0, 1] and capped["next_start"] == 5.0
    assert "next_start" not in _transcript(world, world["new"], "&max_segments=10")


# ------------------------------------------------------------ links

def _stored(rid):
    with app.app_context():
        return json.loads(db.session.get(Recording, rid).transcription)


def test_the_backfill_links_names_without_moving_updated_at(world):
    from src.services.speaker_links import backfill_all
    with app.app_context():
        r = db.session.get(Recording, world["new"])
        r.updated_at = datetime(2020, 1, 1)
        db.session.commit()
        backfill_all(db.engine)
    segs = _stored(world["new"])
    assert [s.get("speaker_id") for s in segs] == [world["dana"], world["omar"], None, None]
    with app.app_context():
        assert db.session.get(Recording, world["new"]).updated_at == datetime(2020, 1, 1)


def test_saving_names_links_them(world):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(world["user"])
    with patch("src.api.recordings.reindex_recording_chunks_async"), patch("src.file_exporter.export_recording"), \
            patch("src.services.speaker.update_voice_profiles", return_value=(0, 0)):
        resp = c.post(f"/recording/{world['new']}/update_speakers",
                      json={"speaker_map": {"SPEAKER_02": {"name": "Visitor", "isMe": False}}})
    assert resp.status_code == 200
    with app.app_context():
        visitor = Speaker.query.filter_by(user_id=world["user"], name="Visitor").one()
    assert [s.get("speaker_id") for s in _stored(world["new"])][2:] == [visitor.id, visitor.id]


def test_a_rename_follows_the_link_even_when_the_name_drifted(world):
    from src.services.speaker_links import backfill_all
    with app.app_context():
        backfill_all(db.engine)
        r = db.session.get(Recording, world["new"])
        segs = json.loads(r.transcription)
        segs[0]["speaker"] = "Dana L."                # an old spelling, still linked
        r.transcription = json.dumps(segs)
        db.session.commit()
    assert _transcript(world, world["new"])["segments"][0]["speaker"] == "Dana"   # linked: current name
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(world["user"])
    with patch("src.api.recordings.reindex_recording_chunks_async"), patch("src.file_exporter.export_recording"):
        assert c.put(f"/speakers/{world['dana']}", json={"name": "Dana Lee"}).status_code == 200
    segs = _stored(world["new"])
    assert (segs[0]["speaker"], segs[0]["speaker_id"]) == ("Dana Lee", world["dana"])
    assert _stored(world["old"])[0]["speaker"] == "Dana Lee"           # unlinked: matched by name


def test_a_merge_moves_the_links(world):
    from src.services.speaker_links import backfill_all
    from src.services.speaker_merge import merge_speakers
    with app.app_context():
        backfill_all(db.engine)
        with patch("src.api.recordings.reindex_recording_chunks_async"), patch("src.file_exporter.export_recording"):
            merge_speakers(world["dana"], [world["omar"]], world["user"])
    segs = _stored(world["new"])
    assert [(s["speaker"], s.get("speaker_id")) for s in segs[:2]] == [("Dana", world["dana"]), ("Dana", world["dana"])]


def test_asr_save_relinks_split_parts_and_reports_server_transcript(world):
    # A split inherits metadata; assigning a different person must not retain the old link.
    parts = [
        {"speaker": "Dana", "speaker_id": world["dana"], "sentence": "First.", "start_time": 0, "end_time": 2, "extra": {"keep": True}},
        {"speaker": "Omar Haddad", "speaker_id": world["dana"], "sentence": "Reply.", "start_time": 2, "end_time": 4},
        {"speaker": "Visitor", "speaker_id": world["dana"], "sentence": "Unknown.", "start_time": 4, "end_time": 6},
    ]
    with _Client(app) as client:
        with client.session_transaction() as session:
            session["_user_id"] = str(world["user"])
            session["_fresh"] = True
        with patch("src.api.recordings.export_recording"), patch("src.services.webhook_dispatch._global_enabled", return_value=True), patch("src.services.webhook_dispatch.enqueue_event") as emit:
            response = client.post(f"/recording/{world['new']}/update_transcription", json={"transcription": json.dumps(parts)})
            assert response.status_code == 200, response.get_json()
            stored = json.loads(response.json["recording"]["transcription"])
            assert stored[0]["speaker_id"] == world["dana"]
            assert stored[1]["speaker_id"] == world["omar"]
            assert "speaker_id" not in stored[2]
            assert stored[0]["extra"] == {"keep": True}
            events = [c for c in emit.call_args_list if len(c.args) > 2 and c.args[2] == "recording.updated"]
            assert len(events) == 1
    canonical = _transcript(world, world["new"])["segments"]
    assert [(p["speaker"], p["speaker_id"]) for p in canonical] == [("Dana", world["dana"]), ("Omar Haddad", world["omar"]), ("Visitor", None)]
