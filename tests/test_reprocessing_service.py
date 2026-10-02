"""One way to reprocess, whichever route asks (#412 audit P8, P9).

API v1 transcribe and summarize, the v1 batch and the web bulk action now use
the same service as the web reprocess buttons: the same checks (audio present,
not already processing, a usable transcript, an LLM client), the same clearing
of old transcript, summary and events, and the same job parameters.

SHARED-DB: users, recordings, events and tokens are removed afterwards.
"""

import os
import secrets
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Event, Recording, User
from src.services.job_queue import job_queue
from src.utils.token_auth import hash_token

TRANSCRIPT = "Speaker 1: We agreed to move the launch to next quarter."


@pytest.fixture
def world():
    with app.app_context():
        n = f"reproc_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                      audio_path="local://s.wav", transcription=TRANSCRIPT, summary="Old summary")
        plain = f"test-token-{secrets.token_urlsafe(16)}"
        t = APIToken(user_id=u.id, token_hash=hash_token(plain), name="reproc")
        db.session.add_all([r, t])
        db.session.commit()
        db.session.add(Event(recording_id=r.id, title="Old event", start_datetime=datetime(2026, 3, 9, 9)))
        db.session.commit()
        ids = dict(user=u.id, rec=r.id, token=t.id)
    yield ids, plain
    with app.app_context():
        Event.query.filter_by(recording_id=ids["rec"]).delete()
        for model, key in ((APIToken, "token"), (Recording, "rec"), (User, "user")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _storage(exists=True):
    s = MagicMock()
    s.exists.return_value = exists
    return patch("src.services.storage.get_storage_service", return_value=s)


def _post(plain, path, body=None):
    with app.test_client() as c:
        return c.post(path, json=body or {}, headers={"Authorization": f"Bearer {plain}"})


def test_v1_transcribe_clears_old_results_like_the_web_route(world):
    ids, plain = world
    with _storage(), patch.object(job_queue, "enqueue", return_value=7) as enq:
        resp = _post(plain, f"/api/v1/recordings/{ids['rec']}/transcribe", {"language": "de"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert enq.call_args.kwargs["job_type"] == "reprocess_transcription"
    assert enq.call_args.kwargs["params"]["language"] == "de"
    with app.app_context():
        r = db.session.get(Recording, ids["rec"])
        assert (r.transcription, r.summary, r.status) == (None, None, "QUEUED")
        assert Event.query.filter_by(recording_id=ids["rec"]).count() == 0


def test_v1_transcribe_refuses_a_recording_already_processing(world):
    ids, plain = world
    with app.app_context():
        db.session.get(Recording, ids["rec"]).status = "PROCESSING"
        db.session.commit()
    with _storage(), patch.object(job_queue, "enqueue") as enq:
        resp = _post(plain, f"/api/v1/recordings/{ids['rec']}/transcribe")
    assert resp.status_code == 400
    enq.assert_not_called()


def test_v1_transcribe_refuses_missing_audio(world):
    ids, plain = world
    with _storage(exists=False), patch.object(job_queue, "enqueue") as enq:
        resp = _post(plain, f"/api/v1/recordings/{ids['rec']}/transcribe")
    assert resp.status_code == 404
    enq.assert_not_called()


def test_v1_summarize_supports_append_mode_and_clears_old_results(world):
    ids, plain = world
    with patch("src.services.llm.client", object()), patch.object(job_queue, "enqueue", return_value=8) as enq:
        resp = _post(plain, f"/api/v1/recordings/{ids['rec']}/summarize",
                     {"custom_prompt": "Focus on dates", "prompt_mode": "append"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    params = enq.call_args.kwargs["params"]
    assert params["custom_prompt"] == "Focus on dates" and params["custom_prompt_append"] is True
    with app.app_context():
        assert db.session.get(Recording, ids["rec"]).summary is None
        assert Event.query.filter_by(recording_id=ids["rec"]).count() == 0


def test_v1_summarize_refuses_a_failed_transcription(world):
    ids, plain = world
    with app.app_context():
        db.session.get(Recording, ids["rec"]).transcription = "Transcription failed: ASR service unreachable"
        db.session.commit()
    with patch("src.services.llm.client", object()), patch.object(job_queue, "enqueue") as enq:
        resp = _post(plain, f"/api/v1/recordings/{ids['rec']}/summarize")
    assert resp.status_code == 400
    enq.assert_not_called()


def test_v1_batch_uses_the_same_checks(world):
    ids, plain = world
    with app.app_context():
        db.session.get(Recording, ids["rec"]).status = "SUMMARIZING"
        db.session.commit()
    with _storage(), patch.object(job_queue, "enqueue") as enq:
        resp = _post(plain, "/api/v1/recordings/batch/transcribe", {"recording_ids": [ids["rec"]]})
    assert resp.status_code == 200
    assert resp.get_json()["results"][0] == {"id": ids["rec"], "success": False,
                                             "error": "Recording is already being processed"}
    enq.assert_not_called()
