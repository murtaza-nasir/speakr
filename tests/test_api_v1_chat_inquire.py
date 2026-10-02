"""Chat sources and v1 Inquire (mailr spec G9), and the shared chat prompt (#412 C1).

The model is mocked: no request leaves the test.

SHARED-DB: users, tokens, shares, states and recordings are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, InternalShare, Recording, SharedRecordingState, User
from src.utils.token_auth import hash_token

SEGMENTS = [{"speaker": "Dana", "sentence": "Welcome back.", "start_time": 0.0, "end_time": 3.0},
            {"speaker": "Omar", "sentence": "So the budget freeze applies to travel only.", "start_time": 754.2,
             "end_time": 761.0}]


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"ch_{s}", email=f"ch_{s}@local.test", password="x")
        guest = User(username=f"chg_{s}", email=f"chg_{s}@local.test", password="x")
        db.session.add_all([owner, guest])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Weekly sync", status="COMPLETED", original_filename="a.wav",
                        transcription=json.dumps(SEGMENTS), notes="OWNER SECRET NOTE")
        other = Recording(user_id=guest.id, title="Guest only", status="COMPLETED", original_filename="b.wav")
        db.session.add_all([rec, other])
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=guest.id))
        db.session.add(SharedRecordingState(recording_id=rec.id, user_id=guest.id, personal_notes="GUEST NOTE"))
        tokens = {}
        for key, user, scopes in (("process", owner, ["read", "process"]), ("read", owner, ["read"])):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=user.id, token_hash=hash_token(p), name=key, scopes=json.dumps(scopes)))
            tokens[key] = p
        db.session.commit()
        ids = dict(owner=owner.id, guest=guest.id, rec=rec.id, other=other.id, tokens=tokens)
    yield ids
    with app.app_context():
        db.session.rollback()
        SharedRecordingState.query.filter_by(recording_id=ids["rec"]).delete()
        InternalShare.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter(Recording.id.in_([ids["rec"], ids["other"]])).delete(synchronize_session=False)
        APIToken.query.filter_by(user_id=ids["owner"]).delete()
        User.query.filter(User.id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        db.session.commit()


def _completion(text):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


def _chat(world, body, reply):
    seen = {}

    def fake(messages, **kw):
        seen["messages"] = messages
        return _completion(reply)

    with patch("src.services.llm.chat_client", MagicMock()), patch("src.services.llm.call_chat_completion", side_effect=fake):
        with app.test_client() as c:
            resp = c.post(f"/api/v1/recordings/{world['rec']}/chat", json=body,
                          headers={"Authorization": f"Bearer {world['tokens']['process']}"})
    return resp, seen.get("messages")


def test_with_sources_marks_segments_and_checks_quotes(world):
    reply = 'Travel only [S1 "budget freeze applies to travel"], not hiring [S1 "freeze on hiring"] [S9 "x"].'
    resp, messages = _chat(world, {"message": "What does the freeze cover?", "with_sources": True}, reply)
    assert resp.status_code == 200, resp.get_json()
    system = messages[0]["content"]
    assert "[S1 12:34 Omar] So the budget freeze applies to travel only." in system and '[S12 "exact words"]' in system
    body = resp.get_json()
    assert body["response"] == "Travel only [1], not hiring [2] [3]."
    first, invented, missing = body["sources"]
    assert (first["segment_index"], first["start_time"], first["end_time"], first["speaker"], first["verified"]) == \
        (1, 754.2, 761.0, "Omar", True)
    assert invented["verified"] is False and invented["segment_index"] == 1
    assert missing["segment_index"] is None and missing["verified"] is False


def test_without_sources_nothing_changes(world):
    resp, messages = _chat(world, {"message": "Summarise"}, "A summary [S1].")
    assert resp.get_json() == {"response": "A summary [S1].", "sources": []}
    assert "[S1 " not in messages[0]["content"]


def test_the_prompt_carries_the_notes_this_user_can_see(world):
    from src.services.recording_chat import build_chat_messages
    with app.app_context():
        rec = db.session.get(Recording, world["rec"])
        guest_prompt = build_chat_messages(rec, db.session.get(User, world["guest"]), "hi")[0][0]["content"]
        owner_prompt = build_chat_messages(rec, db.session.get(User, world["owner"]), "hi")[0][0]["content"]
    assert "GUEST NOTE" in guest_prompt and "OWNER SECRET NOTE" not in guest_prompt
    assert "OWNER SECRET NOTE" in owner_prompt


def _inquire(world, body, token="process", env="true"):
    with patch.dict(os.environ, {"ENABLE_INQUIRE_MODE": env}):
        with app.test_client() as c:
            return c.post("/api/v1/inquire", json=body, headers={"Authorization": f"Bearer {world['tokens'][token]}"})


def test_inquire_refusals(world):
    off = _inquire(world, {"question": "x"}, env="false")
    assert off.status_code == 403 and off.get_json()["code"] == "feature_disabled"
    read = _inquire(world, {"question": "x"}, token="read")
    assert read.status_code == 403 and read.get_json()["code"] == "insufficient_scope"
    assert _inquire(world, {"question": ""}).status_code == 400


def test_inquire_parses_agent_links_into_citations(world):
    events = [
        {"agent_step": {"id": 1, "tool": "search"}},
        {"delta": f"The freeze covers travel [Weekly sync @ 12:34](/recordings/{world['rec']}?t=754) "},
        {"delta": f"and again [Weekly sync @ 12:34](/recordings/{world['rec']}?t=754); "
                  f"see [Weekly sync](/recordings/{world['rec']}) and [Guest only](/recordings/{world['other']})."},
        {"agent_summary": {"steps": 3, "line": "3 searches"}},
        {"end_of_stream": True},
    ]

    def fake_stream(user, data, mode="auto"):
        assert data["filter_recording_ids"] == [world["rec"]]          # own scope only
        for e in events:
            yield f"data: {json.dumps(e)}\n\n"

    with patch("src.api.inquire.inquire_stream", side_effect=fake_stream), \
            patch("src.services.inquire_agent.agent_enabled", return_value=True):
        resp = _inquire(world, {"question": "What about the freeze?"})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["answer"] == "The freeze covers travel [1] and again [1]; see [2] and Guest only."
    assert [(c["n"], c["recording_id"], c["start_time"], c["url"]) for c in body["citations"]] == [
        (1, world["rec"], 754.0, f"/recordings/{world['rec']}?t=754"),
        (2, world["rec"], None, f"/recordings/{world['rec']}")]
    assert body["citations"][0]["title"] == "Weekly sync"
    assert body["mode_used"] == "agent" and body["steps"] == 3
