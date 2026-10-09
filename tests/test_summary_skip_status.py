"""A summary job skipped for want of an LLM client must not leave SUMMARIZING.

enqueue sets the recording SUMMARIZING before the job runs. When the job
found no LLM client it wrote a skip note into the summary and returned
without touching the status, so the recording stayed SUMMARIZING for good:
the job ended "completed", nothing re-ran it, and the reprocess routes
refused it as already being processed.

SHARED-DB: users and recordings are removed afterwards.
"""

import os
import sys
import uuid
from unittest.mock import patch

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


def test_skipped_summary_returns_the_recording_to_completed(made):
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"sk_{s}", email=f"sk_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        made.append((User, u.id))
        r = Recording(user_id=u.id, title="t", original_filename="a.mp3", status="SUMMARIZING",
                      transcription="word " * 50)
        db.session.add(r)
        db.session.commit()
        made.append((Recording, r.id))
        with patch.object(proc, "client", None), patch.object(proc, "emit_summary_result", create=True):
            proc.generate_summary_only_task(app.app_context(), r.id)
        db.session.expire_all()
        r = db.session.get(Recording, r.id)
        assert r.status == "COMPLETED"
        assert "Summary skipped" in (r.summary or "")
