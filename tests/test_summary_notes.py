"""The owner's notes given to the summarizer (#440).

Off by default. When the owner turns it on, their notes on the recording are
added to the summary prompt's context, in both prompt layouts, and no other
user's notes are used.
"""

from unittest.mock import MagicMock, patch

import pytest

import src.tasks.processing as processing
from src.app import app, db
from src.models import Recording, User

NOTES = "Spell the client as Akme Corp. Agenda: launch date."


@pytest.fixture
def recording():
    with app.app_context():
        user = User(username="sumnotes_user", email="sumnotes@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="Notes test", status="COMPLETED", notes=NOTES,
                        transcription="Alice: acme corp agreed on the 14th. Bob: good, that works for us.")
        db.session.add(rec)
        db.session.commit()
        yield user, rec
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()


def _summary_prompt(user, rec):
    calls = []

    def fake(messages=None, **kwargs):
        calls.append(messages)
        stub = MagicMock()
        stub.choices = [MagicMock()]
        stub.choices[0].message.content = "Summary"
        stub.choices[0].message.reasoning = None
        stub.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return stub

    with patch("src.tasks.processing.call_llm_completion", side_effect=fake), \
            patch("src.tasks.processing.client", new=MagicMock()):
        processing.generate_summary_only_task(app.app_context(), rec.id, user_id=user.id)
    return "\n".join(m["content"] for m in calls[0])


@pytest.mark.parametrize("prefix_cache", [False, True])
def test_notes_are_left_out_by_default(recording, prefix_cache):
    user, rec = recording
    with patch.object(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", prefix_cache):
        assert NOTES not in _summary_prompt(user, rec)


@pytest.mark.parametrize("prefix_cache", [False, True])
def test_notes_are_given_when_the_owner_turns_it_on(recording, prefix_cache):
    user, rec = recording
    user.summary_include_notes = True
    db.session.commit()
    with patch.object(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", prefix_cache):
        assert NOTES in _summary_prompt(user, rec)


def test_account_form_saves_the_setting(recording):
    user, _ = recording
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client.post("/account", data={"summary_prompt": "", "summary_notes_section": "1",
                                  "summary_include_notes": "on"})
    db.session.expire_all()
    assert db.session.get(User, user.id).summary_include_notes is True
    client.post("/account", data={"summary_prompt": ""})
    db.session.expire_all()
    assert db.session.get(User, user.id).summary_include_notes is True
    client.post("/account", data={"summary_prompt": "", "summary_notes_section": "1"})
    db.session.expire_all()
    assert db.session.get(User, user.id).summary_include_notes is False
