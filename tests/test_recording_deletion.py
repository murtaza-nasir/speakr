"""One deletion path for the web app, API v1 and full-recording retention.

API v1 used to join UPLOAD_FOLDER with the storage locator, which never
matches a locator such as local://recordings/..., so the media stayed on
disk; it also left processing jobs behind. Retention left speaker snippets
behind. All three now call delete_recording_completely().
"""

import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Speaker, APIToken
from src.models.processing_job import ProcessingJob
from src.models.speaker_snippet import SpeakerSnippet
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def ctx():
    with app.app_context():
        yield


def _user():
    suffix = uuid.uuid4().hex[:8]
    u = User(username=f"del_{suffix}", email=f"del_{suffix}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _rec_with_children(user, created_days_ago=0):
    rec = Recording(user_id=user.id, title="t", status="COMPLETED",
                    audio_path="local://recordings/2026/09/x.webm", original_filename="x.webm",
                    transcription=json.dumps([{"speaker": "Ana", "sentence": "hello there", "start_time": 0, "end_time": 1}]),
                    created_at=datetime.utcnow() - timedelta(days=created_days_ago))
    db.session.add(rec)
    db.session.commit()
    spk = Speaker(name=f"Ana_{uuid.uuid4().hex[:4]}", user_id=user.id)
    db.session.add(spk)
    db.session.commit()
    db.session.add(SpeakerSnippet(speaker_id=spk.id, recording_id=rec.id, segment_index=0, text_snippet="hello there"))
    db.session.add(ProcessingJob(user_id=user.id, recording_id=rec.id, job_type="summarize", status="completed"))
    db.session.commit()
    return rec


def _gone(rec_id):
    db.session.expire_all()
    return (db.session.get(Recording, rec_id) is None
            and SpeakerSnippet.query.filter_by(recording_id=rec_id).count() == 0
            and ProcessingJob.query.filter_by(recording_id=rec_id).count() == 0)


def _storage():
    storage = MagicMock()
    storage.exists.return_value = True
    return storage


def test_api_v1_delete_removes_media_through_storage_and_children(ctx):
    user = _user()
    rec = _rec_with_children(user)
    raw = f"tok_{uuid.uuid4().hex}"
    db.session.add(APIToken(user_id=user.id, token_hash=hash_token(raw), name="t"))
    db.session.commit()
    storage = _storage()
    with patch("src.services.storage.get_storage_service", return_value=storage):
        r = _Client(app, app.response_class).delete(f'/api/v1/recordings/{rec.id}',
                                                      headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.get_data(as_text=True)
    storage.delete.assert_called_once_with("local://recordings/2026/09/x.webm", missing_ok=True)
    assert _gone(rec.id)


def test_web_delete_still_works(ctx):
    user = _user()
    rec = _rec_with_children(user)
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    storage = _storage()
    with patch("src.services.storage.get_storage_service", return_value=storage):
        r = c.delete(f'/recording/{rec.id}')
    assert r.status_code == 200, r.get_data(as_text=True)
    storage.delete.assert_called_once()
    assert _gone(rec.id)


def test_full_recording_retention_deletes_recordings_with_snippets(ctx):
    from src.services import retention
    user = _user()
    rec = _rec_with_children(user, created_days_ago=400)
    storage = _storage()
    with patch.object(retention, "ENABLE_AUTO_DELETION", True), \
         patch.object(retention, "GLOBAL_RETENTION_DAYS", 30), \
         patch.object(retention, "DELETION_MODE", "full_recording"), \
         patch("src.services.storage.get_storage_service", return_value=storage):
        stats = retention.process_auto_deletion()
    assert stats['errors'] == 0, stats
    assert _gone(rec.id)
