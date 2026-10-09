"""Reset stuck processing also covers QUEUED, and drops the job it was waiting on.

A recording whose job never ran (an enqueue that raised, a queue row lost
across an upgrade) sat QUEUED with no way out: reprocess refused it as
"already being processed" and reset_status did not accept QUEUED. Resetting
a QUEUED recording that does still have a queued job must also retire that
job, or the worker claims it later and overwrites the reset.

SHARED-DB: users, recordings and jobs are removed afterwards.
"""

import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def world():
    with app.app_context():
        n = f"reset_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Stuck", status="QUEUED", original_filename="s.wav",
                      audio_path="local://s.wav")
        db.session.add(r)
        db.session.commit()
        ids = dict(user=u.id, rec=r.id)
    yield ids
    with app.app_context():
        ProcessingJob.query.filter_by(recording_id=ids["rec"]).delete()
        for model, key in ((Recording, "rec"), (User, "user")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _reset(ids):
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["_user_id"] = str(ids["user"])
            sess["_fresh"] = True
        return c.post(f"/recording/{ids['rec']}/reset_status")


def test_queued_recording_with_no_job_can_be_reset(world):
    resp = _reset(world)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    with app.app_context():
        assert db.session.get(Recording, world["rec"]).status == "FAILED"


def test_reset_retires_the_queued_job_but_not_a_running_one(world):
    with app.app_context():
        db.session.add_all([
            ProcessingJob(user_id=world["user"], recording_id=world["rec"],
                          job_type="transcribe", status="queued"),
            ProcessingJob(user_id=world["user"], recording_id=world["rec"],
                          job_type="summarize", status="processing"),
        ])
        db.session.commit()
    assert _reset(world).status_code == 200
    with app.app_context():
        by_type = {j.job_type: j.status for j in ProcessingJob.query.filter_by(recording_id=world["rec"])}
        assert by_type == {"transcribe": "failed", "summarize": "processing"}
