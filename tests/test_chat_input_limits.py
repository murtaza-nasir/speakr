"""Limits on the question and the history the chat routes accept (#634, #633).

Every surface that answers a question: the Ask page (/api/inquire/chat),
POST /api/v1/inquire, the recording panel (/chat), the incognito chat and
POST /api/v1/recordings/<id>/chat. The model is mocked: no request leaves
the test.

SHARED-DB: the user, token and recording are removed afterwards.
"""

import json
import os
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, User
from src.utils.assistant_limits import MAX_QUESTION_CHARS
from src.utils.token_auth import hash_token

app.config['WTF_CSRF_ENABLED'] = False

SEGMENTS = [{"speaker": "Dana", "sentence": "Welcome back.", "start_time": 0.0, "end_time": 3.0},
            {"speaker": "Omar", "sentence": "The budget freeze applies to travel only.", "start_time": 754.2,
             "end_time": 761.0}]


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        user = User(username=f"lim_{s}", email=f"lim_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="Weekly sync", status="COMPLETED", original_filename="a.wav",
                        transcription=json.dumps(SEGMENTS))
        db.session.add(rec)
        token = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add(APIToken(user_id=user.id, token_hash=hash_token(token), name="lim",
                                scopes=json.dumps(["read", "process"])))
        db.session.commit()
        ids = dict(user=user.id, rec=rec.id, token=token)
    yield ids
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(id=ids["rec"]).delete()
        APIToken.query.filter_by(user_id=ids["user"]).delete()
        User.query.filter_by(id=ids["user"]).delete()
        db.session.commit()


def _web(world):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(world["user"])
        sess["_fresh"] = True
    return c


def _bearer(world):
    return {"Authorization": f"Bearer {world['token']}"}


def _llm(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)


def _stream(*texts):
    for text in texts:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)],
                              usage=None)


def _events(body):
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                out.append(line[6:])
    return out


def _deltas(body):
    return "".join(e["delta"] for e in _events(body) if isinstance(e, dict) and isinstance(e.get("delta"), str))


class _Answer:
    """call_chat_completion for every route: records the messages, then streams
    (or, for API v1's recording chat, returns) 'ok'."""

    def __init__(self):
        self.calls = []

    def __call__(self, messages=None, **kwargs):
        self.calls.append(messages if messages is not None else kwargs.get("messages"))
        if kwargs.get("stream"):
            return _stream("o", "k")
        return _llm("ok")


def _router(messages, **kwargs):
    return _llm("RAG" if kwargs.get("operation_type") == "query_routing" else "[]")


def _ask(world, body, answer, router=None):
    """POST /api/inquire/chat on the RAG path with no search results."""
    router = router or MagicMock(side_effect=_router)
    with patch("src.api.inquire.ENABLE_INQUIRE_MODE", True), \
            patch.dict(os.environ, {"ENABLE_INQUIRE_AGENT": "false"}), \
            patch("src.api.inquire.client", MagicMock()), \
            patch("src.api.inquire.EMBEDDINGS_AVAILABLE", True), \
            patch("src.api.inquire.call_llm_completion", router), \
            patch("src.api.inquire.semantic_search_chunks", return_value=[]), \
            patch("src.api.inquire.call_chat_completion", side_effect=answer):
        resp = _web(world).post("/api/inquire/chat", json=body)
        text = resp.get_data(as_text=True)
    return resp, text, router


def _v1_inquire(world, body):
    seen = []

    def fake_stream(user, data, mode="auto", tool_names=None):
        seen.append(data)
        return iter(['data: {"delta": "ok"}\n\n', 'data: {"end_of_stream": true}\n\n'])

    with patch.dict(os.environ, {"ENABLE_INQUIRE_MODE": "true"}), \
            patch("src.api.inquire.inquire_stream", side_effect=fake_stream):
        resp = app.test_client().post("/api/v1/inquire", json=body, headers=_bearer(world))
    return resp, seen


def _v1_chat(world, body, answer):
    with patch("src.services.llm.chat_client", MagicMock()), \
            patch("src.services.llm.call_chat_completion", side_effect=answer):
        return app.test_client().post(f"/api/v1/recordings/{world['rec']}/chat", json=body, headers=_bearer(world))


def _panel(world, body, answer):
    body = dict({"recording_id": world["rec"]}, **body)
    with patch("src.api.recordings.chat_client", MagicMock()), \
            patch("src.api.recordings._assistant_library_tools", return_value=False), \
            patch("src.api.recordings.call_chat_completion", side_effect=answer):
        resp = _web(world).post("/chat", json=body)
        text = resp.get_data(as_text=True)
    return resp, text


def _incognito(world, body, answer):
    body = dict({"transcription": "Dana: the budget is frozen."}, **body)
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True), \
            patch("src.api.recordings.chat_client", MagicMock()), \
            patch("src.api.recordings.call_chat_completion", side_effect=answer):
        resp = _web(world).post("/api/recordings/incognito/chat", json=body)
        text = resp.get_data(as_text=True)
    return resp, text


# ---------------------------------------------------------------------------
# The question (#634)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [MAX_QUESTION_CHARS + 1, 50_000])
def test_the_ask_page_refuses_a_question_over_the_limit(world, length):
    answer = _Answer()
    resp, _, router = _ask(world, {"message": "x" * length}, answer)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "message_too_long"
    assert "10,000" in body["error"]
    router.assert_not_called()
    assert answer.calls == []


@pytest.mark.parametrize("message", ["x" * MAX_QUESTION_CHARS, "é" * MAX_QUESTION_CHARS, "What did we decide?"])
def test_the_ask_page_answers_a_question_at_the_limit(world, message):
    """The limit counts characters, not UTF-8 bytes: 10,000 x 'é' is 20,000 bytes."""
    answer = _Answer()
    resp, text, _ = _ask(world, {"message": message}, answer)
    assert resp.status_code == 200
    assert "ok" in _deltas(text)
    assert answer.calls and answer.calls[0][-1] == {"role": "user", "content": message}


def test_api_v1_inquire_refuses_a_question_over_the_limit(world):
    resp, seen = _v1_inquire(world, {"question": "x" * (MAX_QUESTION_CHARS + 1)})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_parameter"
    assert "10,000" in resp.get_json()["error"]
    assert seen == []


def test_api_v1_inquire_answers_a_question_at_the_limit(world):
    question = "é" * MAX_QUESTION_CHARS
    resp, seen = _v1_inquire(world, {"question": f"  {question}  "})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["answer"] == "ok"
    assert seen[0]["message"] == question


def test_api_v1_recording_chat_refuses_a_question_over_the_limit(world):
    answer = _Answer()
    resp = _v1_chat(world, {"message": "x" * (MAX_QUESTION_CHARS + 1)}, answer)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_parameter"
    assert answer.calls == []


def test_api_v1_recording_chat_answers_a_question_at_the_limit(world):
    answer = _Answer()
    resp = _v1_chat(world, {"message": "x" * MAX_QUESTION_CHARS}, answer)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["response"] == "ok"


@pytest.mark.parametrize("surface", ["panel", "incognito"])
def test_the_recording_chats_refuse_a_question_over_the_limit(world, surface):
    answer = _Answer()
    send = _panel if surface == "panel" else _incognito
    resp, _ = send(world, {"message": "x" * (MAX_QUESTION_CHARS + 1)}, answer)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "message_too_long"
    assert answer.calls == []


@pytest.mark.parametrize("surface", ["panel", "incognito"])
def test_the_recording_chats_answer_a_question_at_the_limit(world, surface):
    answer = _Answer()
    send = _panel if surface == "panel" else _incognito
    resp, text = send(world, {"message": "x" * MAX_QUESTION_CHARS}, answer)
    assert resp.status_code == 200
    assert "ok" in _deltas(text)
    assert answer.calls[0][-1]["content"] == "x" * MAX_QUESTION_CHARS


def test_the_refusal_is_in_the_interface_language(world):
    with app.app_context():
        db.session.get(User, world["user"]).ui_language = "de"
        db.session.commit()
    answer = _Answer()
    with patch("src.api.inquire.ENABLE_INQUIRE_MODE", True), \
            patch("src.api.inquire.client", MagicMock()), \
            patch("src.api.inquire.call_chat_completion", side_effect=answer):
        resp = _web(world).post("/api/inquire/chat", json={"message": "x" * (MAX_QUESTION_CHARS + 1)})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "message_too_long"
    assert body["error"] == "Die Frage ist zu lang (höchstens 10.000 Zeichen)."


def test_the_composer_stops_typing_at_the_limit():
    with app.test_request_context():
        from flask import render_template_string
        html = render_template_string(
            "{% from 'components/assistant/chat.html' import assistant_chat with context %}"
            "{{ assistant_chat() }}")
    assert f'maxlength="{MAX_QUESTION_CHARS}"' in html
