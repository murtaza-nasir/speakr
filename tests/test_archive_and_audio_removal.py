"""Archive (#394) and manual audio removal.

Archive hides a recording from the main list without deleting anything, per
user like inbox and star. Search still finds archived recordings. "Delete
audio" removes only the media file and leaves the recording in the same
"Audio removed" state that audio-only retention produces.

SHARED-DB: assertions are scoped to the recordings each test creates.
"""

import json
from contextlib import ExitStack
import os
import sys
import uuid
from unittest.mock import patch, MagicMock

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording
from src.models.sharing import InternalShare, SharedRecordingState

app.config["WTF_CSRF_ENABLED"] = False


def _internal_sharing_on():
    """Enable internal sharing for the test, whatever the environment says.

    Each module copies ENABLE_INTERNAL_SHARING at import time; CI has no .env,
    so sharing is off there unless the tests switch it on in every copy.
    """
    stack = ExitStack()
    for name, module in list(sys.modules.items()):
        if name.startswith("src") and module is not None and hasattr(module, "ENABLE_INTERNAL_SHARING"):
            stack.enter_context(patch.object(module, "ENABLE_INTERNAL_SHARING", True))
    return stack


_created = []


@pytest.fixture
def ctx():
    """App context, and removal of every recording the test created: later
    files (test_cov_admin) assume no eligible completed recordings exist."""
    with app.app_context(), _internal_sharing_on():
        yield
        db.session.rollback()
        for rec_id in _created:
            InternalShare.query.filter_by(recording_id=rec_id).delete()
            SharedRecordingState.query.filter_by(recording_id=rec_id).delete()
            Recording.query.filter_by(id=rec_id).delete()
        db.session.commit()
        _created.clear()


def _user():
    suffix = uuid.uuid4().hex[:8]
    u = User(username=f"ar_{suffix}", email=f"ar_{suffix}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _rec(user, title=None, status="COMPLETED"):
    r = Recording(user_id=user.id, title=title or f"arch_{uuid.uuid4().hex[:10]}", status=status,
                  audio_path="local://recordings/x.mp3", original_filename="x.mp3",
                  transcription=json.dumps([{"speaker": "A", "sentence": "hello", "start_time": 0, "end_time": 1}]),
                  summary="A summary")
    db.session.add(r)
    db.session.commit()
    _created.append(r.id)
    return r


class _Client(FlaskClient):
    """Each test holds one app context open across requests, and Flask-Login
    caches the loaded user on g for that context. Clear it so a request from
    one user's client never runs as the user of an earlier request."""

    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _client(user):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _ids(client, **params):
    params.setdefault('per_page', 100)
    body = client.get('/api/recordings', query_string=params).get_json()
    return {r['id'] for r in body['recordings']}


def _share(rec, owner, other):
    db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=other.id))
    db.session.commit()


# --------------------------------------------------------------------- archive

def test_archived_recordings_leave_the_main_list_and_come_back(ctx):
    user = _user()
    kept, archived = _rec(user), _rec(user)
    c = _client(user)
    r = c.post(f'/recording/{archived.id}/toggle_archive')
    assert r.get_json()['is_archived'] is True

    assert kept.id in _ids(c) and archived.id not in _ids(c)
    assert _ids(c, archived='true') & {kept.id, archived.id} == {archived.id}

    c.post(f'/recording/{archived.id}/toggle_archive')
    assert archived.id in _ids(c)


def test_search_still_finds_archived_recordings(ctx):
    user = _user()
    rec = _rec(user, title=f"Quarterly zebra review {uuid.uuid4().hex[:6]}")
    c = _client(user)
    c.post(f'/recording/{rec.id}/toggle_archive')
    assert rec.id in _ids(c, q=rec.title)


def test_rows_with_null_archive_flag_stay_visible(ctx):
    user = _user()
    rec = _rec(user)
    db.session.execute(db.text("UPDATE recording SET is_archived = NULL WHERE id = :id"), {"id": rec.id})
    db.session.commit()
    assert rec.id in _ids(_client(user))


def test_archiving_a_shared_recording_is_personal(ctx):
    owner, other = _user(), _user()
    rec = _rec(owner)
    _share(rec, owner, other)
    other_client = _client(other)
    assert other_client.post(f'/recording/{rec.id}/toggle_archive').get_json()['is_archived'] is True
    assert rec.id not in _ids(other_client)
    assert rec.id in _ids(other_client, archived='true')
    # The owner's list is unaffected.
    assert rec.id in _ids(_client(owner))
    db.session.expire_all()
    assert not db.session.get(Recording, rec.id).is_archived


def test_archive_needs_access(ctx):
    owner, stranger = _user(), _user()
    rec = _rec(owner)
    assert _client(stranger).post(f'/recording/{rec.id}/toggle_archive').status_code == 403
    assert SharedRecordingState.query.filter_by(recording_id=rec.id, user_id=stranger.id).count() == 0


def test_bulk_archive_and_save_field(ctx):
    user = _user()
    a, b = _rec(user), _rec(user)
    c = _client(user)
    r = c.post('/api/recordings/bulk-toggle', json={'recording_ids': [a.id, b.id], 'field': 'archive', 'value': True})
    assert r.status_code == 200 and set(r.get_json()['affected_ids']) == {a.id, b.id}
    assert not ({a.id, b.id} & _ids(c))
    c.post(f'/save', json={'id': a.id, 'is_archived': False})
    assert a.id in _ids(c)


def test_recording_payloads_carry_the_flag(ctx):
    user = _user()
    rec = _rec(user)
    c = _client(user)
    c.post(f'/recording/{rec.id}/toggle_archive')
    body = c.get('/api/recordings', query_string={'archived': 'true', 'per_page': 100}).get_json()
    row = next(r for r in body['recordings'] if r['id'] == rec.id)
    assert row['is_archived'] is True


# ----------------------------------------------------------------- audio removal

def _fake_storage():
    storage = MagicMock()
    storage.exists.return_value = True
    return storage


def test_delete_audio_keeps_the_recording(ctx):
    user = _user()
    rec = _rec(user)
    storage = _fake_storage()
    with patch("src.services.retention.get_storage_service", return_value=storage):
        r = _client(user).post(f'/recording/{rec.id}/delete_audio')
    assert r.status_code == 200, r.get_data(as_text=True)
    storage.delete.assert_called_once_with("local://recordings/x.mp3", missing_ok=True)
    db.session.expire_all()
    saved = db.session.get(Recording, rec.id)
    assert saved.audio_deleted_at is not None
    assert saved.summary == "A summary" and "hello" in saved.transcription
    assert r.get_json()['recording']['audio_deleted_at']


def test_delete_audio_refusals(ctx):
    owner, other = _user(), _user()
    done, busy = _rec(owner), _rec(owner, status="PROCESSING")
    storage = _fake_storage()
    with patch("src.services.retention.get_storage_service", return_value=storage):
        assert _client(other).post(f'/recording/{done.id}/delete_audio').status_code == 403
        assert _client(owner).post(f'/recording/{busy.id}/delete_audio').status_code == 409
        assert _client(owner).post(f'/recording/{done.id}/delete_audio').status_code == 200
        assert _client(owner).post(f'/recording/{done.id}/delete_audio').status_code == 409
        with patch("src.api.recordings.USERS_CAN_DELETE", False):
            assert _client(owner).post(f'/recording/{busy.id}/delete_audio').status_code == 403
    storage.delete.assert_called_once()


def test_audio_removed_filter(ctx):
    user = _user()
    kept, removed = _rec(user), _rec(user)
    with patch("src.services.retention.get_storage_service", return_value=_fake_storage()):
        _client(user).post(f'/recording/{removed.id}/delete_audio')
    c = _client(user)
    assert _ids(c, audio_removed='true') & {kept.id, removed.id} == {removed.id}
    # Removing audio does not archive: the recording stays in the main list.
    assert removed.id in _ids(c)


def test_retention_audio_only_path_uses_the_shared_helper(ctx):
    from src.services import retention
    user = _user()
    rec = _rec(user)
    storage = _fake_storage()
    with patch("src.services.retention.get_storage_service", return_value=storage):
        assert retention.remove_recording_audio(rec) is True
    assert rec.audio_deleted_at is not None
    storage.exists.return_value = False
    with patch("src.services.retention.get_storage_service", return_value=storage):
        assert retention.remove_recording_audio(rec) is False


# -------------------------------------------------------------------- API v1

def _token_client(user):
    from src.models import APIToken
    from src.utils.token_auth import hash_token
    raw = f"tok_{uuid.uuid4().hex}"
    db.session.add(APIToken(user_id=user.id, token_hash=hash_token(raw), name="t"))
    db.session.commit()
    return app.test_client(), {"Authorization": f"Bearer {raw}"}


def test_api_v1_archive_and_delete_audio(ctx):
    user = _user()
    rec = _rec(user)
    c, h = _token_client(user)
    r = c.patch(f'/api/v1/recordings/{rec.id}', json={'is_archived': True}, headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    listed = c.get('/api/v1/recordings', query_string={'archived': 'true', 'per_page': 100}, headers=h).get_json()
    rows = listed.get('recordings', listed.get('data', []))
    assert any(row['id'] == rec.id and row['is_archived'] for row in rows)
    unarchived = c.get('/api/v1/recordings', query_string={'archived': 'false', 'per_page': 100}, headers=h).get_json()
    assert all(row['id'] != rec.id for row in unarchived.get('recordings', unarchived.get('data', [])))

    with patch("src.services.retention.get_storage_service", return_value=_fake_storage()):
        r = c.post(f'/api/v1/recordings/{rec.id}/delete-audio', headers=h)
    assert r.status_code == 200 and r.get_json()['audio_deleted_at']



def test_delete_audio_removes_a_retained_video(ctx):
    """With video retention the stored media file IS the video; it goes too."""
    user = _user()
    rec = _rec(user)
    rec.audio_path = "local://recordings/meeting.mp4"
    rec.mime_type = "video/mp4"
    db.session.commit()
    storage = _fake_storage()
    with patch("src.services.retention.get_storage_service", return_value=storage):
        r = _client(user).post(f'/recording/{rec.id}/delete_audio')
    assert r.status_code == 200
    storage.delete.assert_called_once_with("local://recordings/meeting.mp4", missing_ok=True)
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).audio_deleted_at is not None
