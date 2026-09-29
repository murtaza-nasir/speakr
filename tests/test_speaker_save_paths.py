"""Both speaker save routes apply names the same way (#395).

The speaker modal saves through update_speakers, or through update_transcript
when a line edit is staged. update_transcript used to rename the segments but
skip the voice profiles and snippets, and both routes matched saved speakers
by exact name, so "john" created a second profile next to "John".

SHARED-DB: every assertion is scoped to the user and recording the test made.
"""

import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Speaker

app.config["WTF_CSRF_ENABLED"] = False

EMB = [0.1] * 256


def _user():
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"spk_{suffix}", email=f"spk_{suffix}@local.test", password="x", name="Owner Person")
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user, segments, embeddings=None):
    rec = Recording(user_id=user.id, title="r", status="COMPLETED",
                    audio_path="local://recordings/x.mp3", original_filename="x.mp3",
                    transcription=json.dumps(segments), speaker_embeddings=embeddings)
    db.session.add(rec)
    db.session.commit()
    return rec


def _client(user):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


SEGMENTS = [
    {"speaker": "SPEAKER_00", "sentence": "Good morning everyone, let us begin.", "start_time": 0.0, "end_time": 3.0},
    {"speaker": "SPEAKER_01", "sentence": "Thanks, I have the quarterly numbers ready.", "start_time": 3.0, "end_time": 6.0},
    {"speaker": "SPEAKER_02", "sentence": "Yes.", "start_time": 6.0, "end_time": 6.5},
]


@pytest.fixture
def ctx():
    with app.app_context():
        yield


def test_update_speakers_reuses_the_saved_spelling(ctx):
    user = _user()
    db.session.add(Speaker(name="John Smith", user_id=user.id, use_count=3))
    db.session.commit()
    rec = _recording(user, SEGMENTS)

    r = _client(user).post(f"/recording/{rec.id}/update_speakers", json={
        "speaker_map": {"SPEAKER_00": {"name": "john smith"}, "SPEAKER_01": {"name": "Ana"}},
    })
    assert r.status_code == 200
    saved = json.loads(db.session.get(Recording, rec.id).transcription)
    assert [s["speaker"] for s in saved] == ["John Smith", "Ana", "SPEAKER_02"]
    rows = Speaker.query.filter_by(user_id=user.id).all()
    assert sorted(s.name for s in rows) == ["Ana", "John Smith"]
    assert next(s for s in rows if s.name == "John Smith").use_count == 4
    assert db.session.get(Recording, rec.id).participants == "Ana, John Smith"


def test_two_labels_with_one_name_merge_into_one_speaker(ctx):
    user = _user()
    rec = _recording(user, SEGMENTS)
    r = _client(user).post(f"/recording/{rec.id}/update_speakers", json={
        "speaker_map": {"SPEAKER_00": {"name": "Ana"}, "SPEAKER_02": {"name": "ana"}},
    })
    assert r.status_code == 200
    saved = json.loads(db.session.get(Recording, rec.id).transcription)
    # The first spelling creates the profile; the second resolves to it.
    assert {saved[0]["speaker"], saved[2]["speaker"]} == {"Ana"}
    assert Speaker.query.filter_by(user_id=user.id).count() == 1


@pytest.mark.parametrize("route", ["update_speakers", "update_transcript"])
def test_both_routes_update_voice_profiles_and_snippets(ctx, route):
    user = _user()
    rec = _recording(user, SEGMENTS, embeddings={"SPEAKER_00": EMB, "SPEAKER_01": EMB})
    body = {"speaker_map": {"SPEAKER_00": {"name": "Ana"}}}
    if route == "update_transcript":
        staged = json.loads(rec.transcription)
        staged[2]["speaker"] = "SPEAKER_00"   # a staged per-line reassignment
        body["transcript_data"] = staged

    with patch("src.services.speaker_embedding_matcher.update_speaker_embedding", return_value=None) as upd, \
         patch("src.services.speaker_snippets.create_speaker_snippets", return_value=2) as snip:
        r = _client(user).post(f"/recording/{rec.id}/{route}", json=body)

    assert r.status_code == 200
    assert upd.call_count == 1
    speaker, embedding, rec_id = upd.call_args.args
    assert speaker.name == "Ana" and rec_id == rec.id and len(embedding) == 256
    snip.assert_called_once()
    assert snip.call_args.args[1] == {"SPEAKER_00": {"name": "Ana"}}
    saved = json.loads(db.session.get(Recording, rec.id).transcription)
    if route == "update_transcript":
        assert saved[2]["speaker"] == "Ana"


def test_is_me_without_a_name_uses_the_account_name(ctx):
    user = _user()
    rec = _recording(user, SEGMENTS)
    r = _client(user).post(f"/recording/{rec.id}/update_transcript", json={
        "transcript_data": json.loads(rec.transcription),
        "speaker_map": {"SPEAKER_01": {"name": "", "isMe": True}},
    })
    assert r.status_code == 200
    assert json.loads(db.session.get(Recording, rec.id).transcription)[1]["speaker"] == "Owner Person"


def test_search_top_lists_most_used_without_a_query(ctx):
    user = _user()
    for name, count in (("Rare", 1), ("Frequent", 9), ("Middle", 4)):
        db.session.add(Speaker(name=name, user_id=user.id, use_count=count))
    db.session.commit()
    c = _client(user)
    assert c.get("/speakers/search?q=").get_json() == []
    names = [s["name"] for s in c.get("/speakers/search?top=1").get_json()]
    assert names == ["Frequent", "Middle", "Rare"]
    assert [s["name"] for s in c.get("/speakers/search?q=mid&top=1").get_json()] == ["Middle"]
