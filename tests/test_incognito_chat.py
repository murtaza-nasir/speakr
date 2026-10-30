"""Chat on an incognito recording (#527).

The handler passed an undefined name to build_chat_messages, so every
question returned 500. The transcript, participants and notes come from the
request, and no Recording row is written.

SHARED-DB: the user is removed afterwards.
"""

import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User

app.config["WTF_CSRF_ENABLED"] = False

URL = "/api/recordings/incognito/chat"


@pytest.fixture
def client():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"incchat_{s}", email=f"incchat_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    yield c
    with app.app_context():
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _chunks(text):
    return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])]


def _recording_count():
    with app.app_context():
        return db.session.query(Recording).count()


def test_incognito_chat_streams_and_saves_nothing(client):
    before = _recording_count()
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True), \
         patch("src.api.recordings.chat_client", object()), \
         patch("src.api.recordings.call_chat_completion", return_value=_chunks("pong")) as call:
        r = client.post(URL, json={"transcription": "The budget is 12.", "message": "What is the budget?",
                                   "message_history": [], "participants": "", "notes": ""})
        body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert r.content_type.startswith("text/event-stream")
    assert "pong" in body and "data: [DONE]" in body
    system = call.call_args.args[0][0]["content"]
    assert "The budget is 12." in system
    assert "No specific participants information provided." in system
    assert call.call_args.args[0][-1] == {"role": "user", "content": "What is the budget?"}
    assert _recording_count() == before


def test_incognito_chat_prompt_has_participants_notes_and_history(client):
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True), \
         patch("src.api.recordings.chat_client", object()), \
         patch("src.api.recordings.call_chat_completion", return_value=_chunks("ok")) as call:
        r = client.post(URL, json={"transcription": "Text.", "message": "Who spoke?",
                                   "message_history": history, "participants": "Ada, Bo",
                                   "notes": "Spell the product as Widget"})
        r.get_data()
    assert r.status_code == 200
    messages = call.call_args.args[0]
    assert "Ada, Bo" in messages[0]["content"]
    assert "Spell the product as Widget" in messages[0]["content"]
    assert messages[1:3] == history


def test_incognito_chat_ignores_malformed_history(client):
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True), \
         patch("src.api.recordings.chat_client", object()), \
         patch("src.api.recordings.call_chat_completion", return_value=_chunks("ok")) as call:
        r = client.post(URL, json={"transcription": "Text.", "message": "Q?", "message_history": "nope"})
        r.get_data()
    assert r.status_code == 200
    assert len(call.call_args.args[0]) == 2


def test_incognito_chat_disabled_is_403(client):
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", False), \
         patch("src.api.recordings.call_chat_completion") as call:
        r = client.post(URL, json={"transcription": "Text.", "message": "Q?"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "incognito_disabled"
    call.assert_not_called()


@pytest.mark.parametrize("payload, code", [
    ({"message": "hi"}, "no_transcription_provided"),
    ({"transcription": "Text."}, "no_message_provided"),
    ({"transcription": ["not", "text"], "message": "hi"}, "no_transcription_provided"),
])
def test_incognito_chat_missing_fields_are_400(client, payload, code):
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True), \
         patch("src.api.recordings.chat_client", object()), \
         patch("src.api.recordings.call_chat_completion") as call:
        r = client.post(URL, json=payload)
    assert r.status_code == 400
    assert r.get_json()["code"] == code
    call.assert_not_called()


def test_incognito_chat_requires_login():
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True):
        r = app.test_client().post(URL, json={"transcription": "Text.", "message": "Q?"})
    assert r.status_code in (302, 401)
