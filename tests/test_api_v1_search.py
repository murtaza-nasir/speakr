"""GET /api/v1/search (mailr spec G3).

Keyword search reads every recording the caller owns, without semantic
search: a word spoken once a year ago is found with its timestamp and
speaker. Semantic mode needs Inquire mode and embeddings.

SHARED-DB: users, tokens, tags and recordings are removed afterwards.
"""

import json
import os
import secrets
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, RecordingTag, SharedRecordingState, Tag, User
from src.utils.token_auth import hash_token

NEW_KEYS = [{"speaker": "Dana", "sentence": "Welcome back everyone.", "start_time": 0.0, "end_time": 3.0},
            {"speaker": "Dana", "sentence": "So the budget freeze applies to travel only.", "start_time": 754.2, "end_time": 761.0},
            {"speaker": "Omar", "sentence": "Understood, 100% clear_now.", "start_time": 762.0, "end_time": 765.0}]
OLD_KEYS = [{"speaker": "Lee", "text": "The zeppelin order shipped late.", "start": 12.5, "end": 15.0}]


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"srch_{s}", email=f"srch_{s}@local.test", password="x")
        other = User(username=f"srcho_{s}", email=f"srcho_{s}@local.test", password="x")
        db.session.add_all([me, other])
        db.session.commit()
        tag = Tag(name=f"t-{s}", user_id=me.id)
        sync = Recording(user_id=me.id, title="Weekly sync", status="COMPLETED", original_filename="a.wav",
                         meeting_date=datetime(2026, 9, 29, 15), participants="Dana, Omar",
                         transcription=json.dumps(NEW_KEYS), summary="## Budget\n- The budget freeze ends in January.",
                         notes="Ask about the budget freeze exceptions.")
        old = Recording(user_id=me.id, title="Ops call", status="COMPLETED", original_filename="b.wav",
                        meeting_date=datetime(2025, 9, 1, 10), transcription=json.dumps(OLD_KEYS))
        plain = Recording(user_id=me.id, title="Phone note", status="COMPLETED", original_filename="c.wav",
                          created_at=datetime(2026, 1, 5, 9), transcription="A plain text transcript about the zeppelin.")
        theirs = Recording(user_id=other.id, title="Budget freeze secrets", status="COMPLETED",
                           original_filename="d.wav", transcription=json.dumps(NEW_KEYS))
        db.session.add_all([tag, sync, old, plain, theirs])
        db.session.commit()
        db.session.add(RecordingTag(recording_id=old.id, tag_id=tag.id, order=0))
        tokens = {}
        for who in (me, other):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=who.id, token_hash=hash_token(p), name="s", scopes=json.dumps(["read"])))
            tokens[who.id] = p
        db.session.commit()
        ids = dict(me=me.id, other=other.id, tag=tag.id, sync=sync.id, old=old.id, plain=plain.id,
                   theirs=theirs.id, tokens=tokens)
    yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["me"], ids["other"]]
        rec_ids = [ids[k] for k in ("sync", "old", "plain", "theirs")]
        RecordingTag.query.filter(RecordingTag.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        SharedRecordingState.query.filter(SharedRecordingState.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
        Tag.query.filter(Tag.user_id.in_(users)).delete(synchronize_session=False)
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _search(world, who="me", **params):
    from urllib.parse import urlencode
    with app.test_client() as c:
        return c.get(f"/api/v1/search?{urlencode(params)}",
                     headers={"Authorization": f"Bearer {world['tokens'][world[who]]}"})


def _ok(world, **params):
    resp = _search(world, **params)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_a_word_spoken_once_is_found_with_time_and_speaker(world):
    body = _ok(world, q="zeppelin")
    assert body["mode_used"] == "keyword"
    hit = next(h for h in body["results"] if h["recording_id"] == world["old"])
    assert (hit["field"], hit["segment_index"], hit["start_time"], hit["end_time"], hit["speaker"]) == \
        ("transcript", 0, 12.5, 15.0, "Lee")                       # old key set: text/start/end
    plain = next(h for h in body["results"] if h["recording_id"] == world["plain"])
    assert plain["start_time"] is None and plain["segment_index"] is None


def test_each_field_gives_its_own_hit(world):
    by_field = {}
    for h in _ok(world, q="budget freeze")["results"]:          # best hit first
        if h["recording_id"] == world["sync"]:
            by_field.setdefault(h["field"], h)
    assert set(by_field) == {"transcript", "summary", "notes"}
    t = by_field["transcript"]
    assert (t["segment_index"], t["start_time"], t["speaker"]) == (1, 754.2, "Dana")
    assert t["text"][t["match_spans"][0][0]:t["match_spans"][0][1]].lower() in ("budget", "freeze")
    assert by_field["summary"]["text"] == "- The budget freeze ends in January."
    assert _ok(world, q="Weekly")["results"][0]["field"] == "title"
    assert {h["field"] for h in _ok(world, q="Omar")["results"] if h["recording_id"] == world["sync"]} >= {"participants"}


def test_terms_are_anded_and_phrases_kept(world):
    assert _ok(world, q="budget zeppelin")["results"] == []
    assert _ok(world, q='"freeze applies"')["results"][0]["field"] == "transcript"
    assert _ok(world, q='"applies freeze"')["results"] == []


def test_percent_and_underscore_are_literal(world):
    hits = _ok(world, q="100%")["results"]
    assert [h["speaker"] for h in hits] == ["Omar"]
    assert [h["speaker"] for h in _ok(world, q="clear_now")["results"]] == ["Omar"]
    assert _ok(world, q="clear%now")["results"] == []


def test_json_keys_are_not_matches(world):
    assert _ok(world, q="sentence")["results"] == []
    assert _ok(world, q="start_time")["results"] == []


def test_filters(world):
    assert {h["speaker"] for h in _ok(world, q="budget", speaker="dana")["results"]} == {"Dana"}
    assert _ok(world, q="budget", speaker="omar")["results"] == []
    assert {h["recording_id"] for h in _ok(world, q="zeppelin", recording_ids=str(world["plain"]))["results"]} == {world["plain"]}
    assert {h["recording_id"] for h in _ok(world, q="zeppelin", tag_id=world["tag"])["results"]} == {world["old"]}
    by_meeting = _ok(world, q="zeppelin", date_from="2025-09-01", date_to="2025-09-01")["results"]
    assert {h["recording_id"] for h in by_meeting} == {world["old"]}
    by_created = _ok(world, q="zeppelin", date_field="created_at", date_from="2026-01-05", date_to="2026-01-05")["results"]
    assert {h["recording_id"] for h in by_created} == {world["plain"]}
    assert {h["field"] for h in _ok(world, q="budget", fields="summary")["results"]} == {"summary"}


def test_another_users_recording_never_appears(world):
    assert world["theirs"] not in {h["recording_id"] for h in _ok(world, q="budget")["results"]}
    theirs = _ok(world, who="other", q="budget")["results"]
    assert {h["recording_id"] for h in theirs} == {world["theirs"]}


def test_limit_and_page(world):
    first = _ok(world, q="budget", limit=2)
    second = _ok(world, q="budget", limit=2, page=2)
    assert len(first["results"]) == 2 and first["has_more"] is True
    keys = lambda r: [(h["recording_id"], h["field"], h["text"]) for h in r["results"]]
    assert not set(keys(first)) & set(keys(second))


@pytest.mark.parametrize("params", [{"q": "a"}, {"q": "x" * 501}, {"q": "budget", "mode": "fuzzy"},
                                    {"q": "budget", "fields": "title,body"}, {"q": "budget", "date_from": "soon"},
                                    {"q": "budget", "limit": 51}])
def test_bad_parameters(world, params):
    resp = _search(world, **params)
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_parameter"


def test_semantic_mode(world):
    from src.services import search_v1
    with patch.object(search_v1, "semantic_available", return_value=False):
        resp = _search(world, q="budget", mode="semantic")
        assert resp.status_code == 409 and resp.get_json()["code"] == "semantic_unavailable"
        assert _ok(world, q="budget", mode="auto")["mode_used"] == "keyword"

    class Chunk:
        recording_id = world["sync"]
        start_time, end_time, speaker_name = 754.2, 761.0, "Dana"
        content = "So the budget freeze applies to travel only."

        @property
        def recording(self):
            return db.session.get(Recording, world["sync"])

    with patch.object(search_v1, "semantic_available", return_value=True), \
            patch("src.services.embeddings.semantic_search_chunks", return_value=[(Chunk(), 0.82)]) as sem:
        body = _ok(world, q="spending pause", mode="auto")
    assert body["mode_used"] == "semantic"
    assert body["results"][0]["score"] == 0.82 and body["results"][0]["speaker"] == "Dana"
    assert sem.call_args.args[2]["recording_ids"] and world["theirs"] not in sem.call_args.args[2]["recording_ids"]
