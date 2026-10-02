"""Lifecycle webhooks from every path (#412 audit P11, P12).

P11: recording.created fired only for /upload, upload join and the v1/ASR
uploads. The share target, watch folder, merge and recording-session finalize
now call the same emit_recording_created helper.
P12: the summary step reports its own result. The first summary of an upload
(run inside the transcription job) now fires recording.summary.completed, and
a failed summary fires recording.summary.failed instead of "completed".

SHARED-DB: users and recordings are removed afterwards.
"""

import ast
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
import src.tasks.processing as proc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def rec():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"lc_{s}", email=f"lc_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Sync", original_filename="s.wav", status="PROCESSING",
                      transcription="Speaker 1: " + "we agreed on the plan. " * 5)
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


def _quiet():
    return (patch.object(proc, "apply_team_tag_auto_shares"), patch.object(proc, "export_recording"),
            patch.object(proc, "process_recording_chunks"), patch.object(proc, "extract_events_from_transcript"))


def _completion(text):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text, reasoning=None))])


def test_a_summary_fires_summary_completed(rec):
    _, rid = rec
    a, b, c, d = _quiet()
    with app.app_context(), a, b, c, d, patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion", return_value=_completion("## Summary")), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        proc.generate_summary_only_task(app.app_context(), rid)
    types = [call.kwargs["event_type"] for call in emit.call_args_list]
    assert types.count("recording.summary.completed") == 1
    assert "recording.summary.failed" not in types


def test_a_failed_summary_fires_summary_failed(rec):
    _, rid = rec
    a, b, c, d = _quiet()
    with app.app_context(), a, b, c, d, patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "call_llm_completion", side_effect=RuntimeError("model unavailable")), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
        proc.generate_summary_only_task(app.app_context(), rid)
    calls = [call for call in emit.call_args_list if call.kwargs["event_type"].startswith("recording.summary")]
    assert [c.kwargs["event_type"] for c in calls] == ["recording.summary.failed"]
    assert calls[0].kwargs["data"]["recording_id"] == rid


def test_every_creation_path_emits_recording_created():
    """Each module that constructs a Recording row must call emit_recording_created."""
    creators = {
        "src/api/recordings.py": ("upload_file/ingest", "share target"),
        "src/file_monitor.py": ("watch folder",),
        "src/services/recording_merge.py": ("merge",),
        "src/api/recording_sessions.py": ("session finalize",),
        "src/services/upload_join.py": (),
    }
    missing = []
    for rel in creators:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        constructs = any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Recording"
                         for n in ast.walk(ast.parse(src)))
        if constructs and rel != "src/services/upload_join.py" and "emit_recording_created(" not in src:
            missing.append(rel)
    assert not missing, f"Recording created without recording.created: {missing}"
    # upload join emits from the /upload route after create_joined_recording
    assert "emit_recording_created(recording)" in open(os.path.join(ROOT, "src/api/recordings.py")).read()


def test_merge_emits_recording_created(rec):
    uid, rid = rec
    from src.services import recording_merge
    with app.app_context():
        user = db.session.get(User, uid)
        a = db.session.get(Recording, rid)
        b = Recording(user_id=uid, title="Sync 2", original_filename="t.wav", status="COMPLETED")
        db.session.add(b)
        db.session.commit()
        with patch.object(recording_merge, "_validate_sources", return_value=[a, b]), \
             patch("src.services.webhook_dispatch.emit_webhook_event") as emit:
            merged = recording_merge.create_merge_recording(user, [a.id, b.id])
        types = [c.kwargs["event_type"] for c in emit.call_args_list]
        assert "recording.created" in types
        for obj in (merged, b):
            db.session.delete(db.session.merge(obj))
        db.session.commit()
