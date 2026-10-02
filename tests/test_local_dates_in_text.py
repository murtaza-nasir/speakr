"""Dates the server writes into text use the owner's local time (#412, Df).

Export filenames, exported text and the dates given to the LLM in the summary
and event prompts were written in UTC. For an owner in Tokyo, a meeting at
2026-03-04 23:30 UTC happened on 5 March.

SHARED-DB: the user and recording are removed afterwards.
"""

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
import src.tasks.processing as proc
from src.file_exporter import render_export_filename


@pytest.fixture
def rec():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"loc_{s}", email=f"loc_{s}@local.test", password="x",
                 timezone="Asia/Tokyo", timezone_mode="fixed", extract_events=True,
                 export_filename_template="{{date}} {{title}}")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Standup", original_filename="s.wav", status="PROCESSING",
                      transcription="Speaker 1: " + "We meet again tomorrow at ten. " * 5,
                      meeting_date=datetime(2026, 3, 4, 23, 30), created_at=datetime(2026, 3, 4, 23, 40))
        db.session.add(r)
        db.session.commit()
        ids = (u.id, r.id)
    yield ids
    with app.app_context():
        for model, oid in ((Recording, ids[1]), (User, ids[0])):
            obj = db.session.get(model, oid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _capture():
    seen = []

    def fake(messages, **kwargs):
        seen.append("\n".join(m["content"] for m in messages))
        msg = MagicMock(content='{"events": []}', reasoning=None)
        return MagicMock(choices=[MagicMock(message=msg)])
    return seen, fake


def test_export_filename_date_is_local(rec):
    uid, rid = rec
    with app.app_context():
        name = render_export_filename(db.session.get(Recording, rid), db.session.get(User, uid))
        assert "2026-03-05" in name and "2026-03-04" not in name


def test_summary_prompt_recording_date_is_local(rec):
    uid, rid = rec
    seen, fake = _capture()
    with app.app_context(), patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion", side_effect=fake), \
         patch.object(proc, "extract_events_from_transcript"), \
         patch.object(proc, "apply_team_tag_auto_shares"), patch.object(proc, "export_recording"), \
         patch.object(proc, "process_recording_chunks"):
        proc.generate_summary_only_task(app.app_context(), rid)
    assert any("Recording date: March 05, 2026" in p for p in seen)


def test_event_prompt_reference_dates_are_local(rec):
    uid, rid = rec
    seen, fake = _capture()
    with app.app_context(), patch.object(proc, "call_llm_completion", side_effect=fake), \
         patch.object(proc, "client", MagicMock()):
        proc.extract_events_from_transcript(rid, "We meet again tomorrow at ten.", "")
    prompt = seen[0]
    assert "MEETING DATE (use this for relative date calculations): Thursday, March 05, 2026" in prompt
    assert "Recording uploaded on: March 05, 2026 at 08:40 AM" in prompt
