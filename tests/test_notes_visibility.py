"""Notes are per user in API v1 (mailr spec, open question 2).

The owner reads and writes the recording's notes. A user the recording is
shared with reads and writes only their own personal notes: never the
owner's, in the notes route, the detail, PATCH or search.

SHARED-DB: users, tokens, shares and the recording are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, InternalShare, Recording, SharedRecordingState, User
from src.utils.token_auth import hash_token


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"nown_{s}", email=f"nown_{s}@local.test", password="x")
        guest = User(username=f"ngst_{s}", email=f"ngst_{s}@local.test", password="x")
        db.session.add_all([owner, guest])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        notes="OWNER PRIVATE: salary numbers", transcription="hello")
        db.session.add(rec)
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=guest.id,
                                     can_edit=True))
        tokens = {}
        for who in (owner, guest):
            p = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=who.id, token_hash=hash_token(p), name="n"))
            tokens[who.id] = p
        db.session.commit()
        ids = dict(owner=owner.id, guest=guest.id, rec=rec.id, tokens=tokens)
    with patch("src.app.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        SharedRecordingState.query.filter_by(recording_id=ids["rec"]).delete()
        InternalShare.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        APIToken.query.filter(APIToken.user_id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        User.query.filter(User.id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        db.session.commit()


def _h(world, who):
    return {"Authorization": f"Bearer {world['tokens'][world[who]]}"}


def _owner_notes(world):
    with app.app_context():
        return db.session.get(Recording, world["rec"]).notes


def test_a_recipient_never_sees_the_owners_notes(world):
    with app.test_client() as c:
        assert c.get(f"/api/v1/recordings/{world['rec']}/notes", headers=_h(world, "guest")).get_json()["notes"] is None
        assert c.get(f"/api/v1/recordings/{world['rec']}", headers=_h(world, "guest")).get_json()["notes"] is None
        assert c.get(f"/api/v1/recordings/{world['rec']}/notes",
                     headers=_h(world, "owner")).get_json()["notes"] == "OWNER PRIVATE: salary numbers"


def test_a_recipient_writes_personal_notes(world):
    with app.test_client() as c:
        put = c.put(f"/api/v1/recordings/{world['rec']}/notes", json={"notes": "my own take"}, headers=_h(world, "guest"))
        assert put.status_code == 200 and put.get_json()["notes"] == "my own take"
        patched = c.patch(f"/api/v1/recordings/{world['rec']}", json={"notes": "edited take"}, headers=_h(world, "guest"))
        assert patched.get_json()["recording"]["notes"] == "edited take"
        assert c.get(f"/api/v1/recordings/{world['rec']}/notes", headers=_h(world, "guest")).get_json()["notes"] == "edited take"
    assert _owner_notes(world) == "OWNER PRIVATE: salary numbers"


def test_the_owner_still_writes_the_recording_notes(world):
    with app.test_client() as c:
        assert c.put(f"/api/v1/recordings/{world['rec']}/notes", json={"notes": "new"},
                     headers=_h(world, "owner")).status_code == 200
    assert _owner_notes(world) == "new"


def test_search_matches_only_the_notes_the_caller_can_see(world):
    from src.models import User as U
    from src.services.search_v1 import keyword_search
    with app.app_context():
        owner = db.session.get(U, world["owner"])
        hits, _ = keyword_search(owner, "salary", fields=("notes",))
        assert [h["field"] for h in hits] == ["notes"]
