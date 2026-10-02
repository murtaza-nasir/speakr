"""Speaker email and aliases, and real speaker ids per recording (mailr spec G6).

SHARED-DB: users, tokens, shares, speakers and recordings are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, InternalShare, Recording, Speaker, User
from src.utils.token_auth import hash_token


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"spc_{s}", email=f"spc_{s}@local.test", password="x")
        guest = User(username=f"spcg_{s}", email=f"spcg_{s}@local.test", password="x")
        db.session.add_all([owner, guest])
        db.session.commit()
        dana = Speaker(name="Dana", user_id=owner.id)
        db.session.add(dana)
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        speaker_label_map={"SPEAKER_00": "Dana"},
                        transcription=json.dumps([
                            {"speaker": "Dana", "sentence": "Budget.", "start_time": 0.0, "end_time": 2.0},
                            {"speaker": "SPEAKER_01", "sentence": "Yes.", "start_time": 2.0, "end_time": 3.0},
                            {"speaker": "Dana", "sentence": "Good.", "start_time": 3.0, "end_time": 4.0}]))
        db.session.add(rec)
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=guest.id))
        tokens = {}
        for who in (owner, guest):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=who.id, token_hash=hash_token(p), name="spc"))
            tokens[who.id] = p
        db.session.commit()
        ids = dict(owner=owner.id, guest=guest.id, dana=dana.id, rec=rec.id, tokens=tokens)
    with patch("src.app.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        InternalShare.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        Speaker.query.filter(Speaker.user_id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        APIToken.query.filter(APIToken.user_id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        User.query.filter(User.id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        db.session.commit()


def _call(world, method, path, who="owner", **kw):
    with app.test_client() as c:
        return getattr(c, method)(path, headers={"Authorization": f"Bearer {world['tokens'][world[who]]}"}, **kw)


def test_set_read_filter_and_clear_email(world):
    put = _call(world, "put", f"/api/v1/speakers/{world['dana']}", json={"email": "Dana@Example.com"})
    assert put.status_code == 200 and put.get_json()["speaker"]["email"] == "dana@example.com"
    assert put.get_json()["speaker"]["name"] == "Dana"            # no name needed
    listed = _call(world, "get", "/api/v1/speakers?email=DANA@example.com").get_json()["speakers"]
    assert [s["id"] for s in listed] == [world["dana"]] and listed[0]["updated_at"].endswith("Z")
    assert _call(world, "get", "/api/v1/speakers?email=nobody@example.com").get_json()["speakers"] == []
    cleared = _call(world, "put", f"/api/v1/speakers/{world['dana']}", json={"email": ""})
    assert cleared.get_json()["speaker"]["email"] is None


@pytest.mark.parametrize("body", [{"email": "not-an-address"}, {"aliases": ["x" * 101]},
                                  {"aliases": [f"a{i}" for i in range(21)]}, {"aliases": "Dana L"}, {}])
def test_invalid_contact_fields(world, body):
    resp = _call(world, "put", f"/api/v1/speakers/{world['dana']}", json=body or {"other": 1})
    assert resp.status_code == 400
    with app.app_context():
        assert db.session.get(Speaker, world["dana"]).email is None


def test_aliases_are_trimmed_and_deduplicated(world):
    resp = _call(world, "put", f"/api/v1/speakers/{world['dana']}", json={"aliases": [" Dana L ", "dana l", "D. Lee", ""]})
    assert resp.get_json()["speaker"]["aliases"] == ["Dana L", "D. Lee"]
    created = _call(world, "post", "/api/v1/speakers", json={"name": "Omar", "email": "omar@example.org",
                                                             "aliases": ["O."]})
    assert created.status_code == 201 and created.get_json()["email"] == "omar@example.org"


def test_another_users_speaker_is_refused(world):
    assert _call(world, "put", f"/api/v1/speakers/{world['dana']}", who="guest",
                 json={"email": "x@example.com"}).status_code == 403


def test_a_rename_through_the_api_reaches_the_recordings(world):
    with patch("src.api.recordings.reindex_recording_chunks_async"), patch("src.file_exporter.export_recording"):
        resp = _call(world, "put", f"/api/v1/speakers/{world['dana']}", json={"name": "Dana Lee"})
    assert resp.status_code == 200
    with app.app_context():
        segs = json.loads(db.session.get(Recording, world["rec"]).transcription)
    assert [s["speaker"] for s in segs] == ["Dana Lee", "SPEAKER_01", "Dana Lee"]


def test_recording_speakers_report_ids_and_email_to_the_owner_only(world):
    _call(world, "put", f"/api/v1/speakers/{world['dana']}", json={"email": "dana@example.com"})
    owner_view = _call(world, "get", f"/api/v1/recordings/{world['rec']}/speakers").get_json()["speakers"]
    assert owner_view == [
        {"label": "SPEAKER_00", "identified_name": "Dana", "speaker_id": world["dana"], "email": "dana@example.com",
         "segment_count": 2},
        {"label": "SPEAKER_01", "identified_name": None, "speaker_id": None, "email": None, "segment_count": 1},
    ]
    guest_view = _call(world, "get", f"/api/v1/recordings/{world['rec']}/speakers", who="guest").get_json()["speakers"]
    assert guest_view[0]["speaker_id"] == world["dana"] and guest_view[0]["email"] is None
