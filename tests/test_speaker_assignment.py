"""Speaker renames run the same steps from the web app and API v1 (#412 audit P10).

API v1 /speakers/assign skipped rebuilding the Inquire chunks, and neither route
rewrote the auto-export when no summary followed. Both now use
apply_speaker_names.

SHARED-DB: the user, recording and token are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, User
from src.services.job_queue import job_queue
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False
SEGMENTS = [{"speaker": "SPEAKER_00", "sentence": "Launch moves to Q3.", "start_time": 0.0, "end_time": 2.0},
            {"speaker": "SPEAKER_01", "sentence": "Agreed.", "start_time": 2.0, "end_time": 3.0}]


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    with app.app_context():
        n = f"spk_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x", name="Jane")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                      transcription=json.dumps(SEGMENTS))
        plain = f"test-token-{secrets.token_urlsafe(16)}"
        t = APIToken(user_id=u.id, token_hash=hash_token(plain), name="spk")
        db.session.add_all([r, t])
        db.session.commit()
        ids = dict(user=u.id, rec=r.id, token=t.id)
    yield ids, plain
    with app.app_context():
        for model, key in ((APIToken, "token"), (Recording, "rec"), (User, "user")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _effects():
    return (patch("src.api.recordings.reindex_recording_chunks_async"),
            patch("src.file_exporter.ENABLE_AUTO_EXPORT", True),
            patch("src.file_exporter.export_recording"),
            patch("src.services.speaker.update_voice_profiles", return_value=(0, 0)))


@pytest.mark.parametrize("via", ["api_v1", "web"])
def test_rename_reindexes_and_rewrites_the_export(world, via):
    ids, plain = world
    reindex, flag, export, voices = _effects()
    with reindex as m_reindex, flag, export as m_export, voices:
        if via == "api_v1":
            with app.test_client() as c:
                resp = c.put(f"/api/v1/recordings/{ids['rec']}/speakers/assign",
                             json={"speaker_map": {"SPEAKER_00": "Jane Doe"}},
                             headers={"Authorization": f"Bearer {plain}"})
        else:
            c = _Client(app, app.response_class, use_cookies=True)
            with c.session_transaction() as sess:
                sess["_user_id"] = str(ids["user"])
            resp = c.post(f"/recording/{ids['rec']}/update_speakers",
                          json={"speaker_map": {"SPEAKER_00": {"name": "Jane Doe", "isMe": False}}})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    m_reindex.assert_called_once_with(ids["rec"])
    m_export.assert_called_once_with(ids["rec"])
    with app.app_context():
        assert "Jane Doe" in db.session.get(Recording, ids["rec"]).transcription


def test_rename_with_a_new_summary_exports_after_the_summary(world):
    ids, plain = world
    reindex, flag, export, voices = _effects()
    with reindex, flag, export as m_export, voices, patch.object(job_queue, "enqueue", return_value=3) as enq:
        with app.test_client() as c:
            resp = c.put(f"/api/v1/recordings/{ids['rec']}/speakers/assign",
                         json={"speaker_map": {"SPEAKER_01": "Bob"}, "regenerate_summary": True},
                         headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 200 and resp.get_json()["summary_queued"] is True
    assert enq.call_args.kwargs["job_type"] == "summarize"
    m_export.assert_not_called()
