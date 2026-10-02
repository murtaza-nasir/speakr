"""Events are extracted by the finishing step, with or without a summary (#412, Db).

Before #412 event extraction ran only inside the summary task, so with
auto-summarization off it never ran, even when the owner had it on. It now
runs in finish_processing: from the summary plus a transcript excerpt when a
summary exists (as before), else from the transcript up to the admin length
limit.

SHARED-DB: the users and recordings created here are removed afterwards.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
import src.tasks.processing as proc


@pytest.fixture
def made():
    ids = []
    yield ids
    with app.app_context():
        db.session.rollback()
        for model, oid in reversed(ids):
            obj = db.session.get(model, oid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _setup(made, extract=True, transcription="word " * 50, title="Board meeting"):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"ev_{s}", email=f"ev_{s}@local.test", password="x", extract_events=extract)
    db.session.add(u)
    db.session.commit()
    made.append((User, u.id))
    r = Recording(user_id=u.id, title=title, original_filename="ev.mp3", status="PROCESSING",
                  transcription=transcription)
    db.session.add(r)
    db.session.commit()
    made.append((Recording, r.id))
    return u, r


def _quiet():
    return (patch.object(proc, "apply_team_tag_auto_shares"), patch.object(proc, "export_recording"),
            patch.object(proc, "process_recording_chunks"))


def _completion(text):
    msg = MagicMock(content=text, reasoning=None)
    return MagicMock(choices=[MagicMock(message=msg)])


def test_events_are_extracted_when_auto_summarization_is_off(made):
    with app.app_context():
        _, r = _setup(made)
        a, b, c = _quiet()
        with a, b, c, patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "extract_events_from_transcript") as ev:
            proc.generate_title_task(app.app_context(), r.id, will_auto_summarize=False)
        ev.assert_called_once()
        rid, transcript, summary = ev.call_args.args
        assert rid == r.id and summary == "" and "word" in transcript


def test_no_events_when_the_owner_has_extraction_off(made):
    with app.app_context():
        _, r = _setup(made, extract=False)
        a, b, c = _quiet()
        with a, b, c, patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "extract_events_from_transcript") as ev:
            proc.finish_processing(r.id)
        ev.assert_not_called()


def test_the_summary_path_extracts_events_once_with_the_summary(made):
    with app.app_context():
        _, r = _setup(made)
        a, b, c = _quiet()
        with a, b, c, patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "call_llm_completion", return_value=_completion("## Summary\nLaunch moved.")), \
             patch.object(proc, "extract_events_from_transcript") as ev:
            proc.generate_summary_only_task(app.app_context(), r.id)
        ev.assert_called_once()
        assert ev.call_args.args[2].startswith("## Summary")


def test_without_a_summary_the_prompt_carries_the_transcript_past_8000_chars(made):
    with app.app_context():
        long = "Speaker 1: " + ("We meet on Friday at noon. " * 900)   # ~24k chars
        _, r = _setup(made, transcription=long)
        seen = {}

        def fake_llm(messages, **kwargs):
            seen["prompt"] = "\n".join(m["content"] for m in messages)
            return _completion('{"events": []}')

        with patch.object(proc, "call_llm_completion", side_effect=fake_llm), \
             patch.object(proc, "client", MagicMock()):
            proc.extract_events_from_transcript(r.id, long, "")
        prompt = seen["prompt"]
        assert "Transcript Summary" not in prompt
        assert "Transcript:\n" in prompt
        assert prompt.count("We meet on Friday at noon.") > 8000 // len("We meet on Friday at noon. ")
