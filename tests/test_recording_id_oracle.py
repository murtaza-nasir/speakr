"""A recording the caller cannot see answers like an id that does not exist (#504).

API v1 used to answer 403 for another account's recording and 404 for a
missing id, so a caller could tell which ids belong to other accounts.
403 now means the caller can see the recording and the request needs more
(edit access or ownership). Permissions themselves are unchanged.

SHARED-DB: users, tokens, shares and recordings are removed afterwards.
"""

import os
import secrets
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, InternalShare, Recording, User
from src.utils.token_auth import hash_token

MISSING_ID = 2_000_000_000


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"or_{s}", email=f"or_{s}@local.test", password="x")
        evan = User(username=f"orev_{s}", email=f"orev_{s}@local.test", password="x")
        admin = User(username=f"orad_{s}", email=f"orad_{s}@local.test", password="x", is_admin=True)
        db.session.add_all([me, evan, admin])
        db.session.commit()
        mine = Recording(user_id=me.id, title="Mine", status="COMPLETED", original_filename="a.wav",
                         transcription="Hello.")
        foreign = Recording(user_id=evan.id, title="Evan only", status="COMPLETED", original_filename="b.wav",
                            transcription="Private words.")
        viewable = Recording(user_id=evan.id, title="Shared to read", status="COMPLETED", original_filename="c.wav",
                             transcription="Shared words.")
        db.session.add_all([mine, foreign, viewable])
        db.session.commit()
        db.session.add(InternalShare(recording_id=viewable.id, owner_id=evan.id, shared_with_user_id=me.id,
                                     can_edit=False, source_type="manual"))
        tokens = {}
        for who in (me, admin):
            plain = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=who.id, token_hash=hash_token(plain), name="oracle"))
            tokens[who.id] = plain
        db.session.commit()
        ids = dict(me=me.id, evan=evan.id, admin=admin.id, mine=mine.id, foreign=foreign.id,
                   viewable=viewable.id, token=tokens[me.id], admin_token=tokens[admin.id])
    with patch("src.app.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["me"], ids["evan"], ids["admin"]]
        recs = [r.id for r in Recording.query.filter(Recording.user_id.in_(users))]
        InternalShare.query.filter(InternalShare.recording_id.in_(recs)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _call(world, method, path, token=None, **kw):
    with app.test_client() as c:
        return c.open(path, method=method, headers={"Authorization": f"Bearer {token or world['token']}"}, **kw)


def _exists(rec_id):
    with app.app_context():
        return db.session.get(Recording, rec_id) is not None


READS = ["", "/transcript", "/summary", "/notes", "/status", "/audio", "/events", "/speakers"]


@pytest.mark.parametrize("suffix", READS)
def test_foreign_and_missing_reads_match(world, suffix):
    foreign = _call(world, "GET", f"/api/v1/recordings/{world['foreign']}{suffix}")
    missing = _call(world, "GET", f"/api/v1/recordings/{MISSING_ID}{suffix}")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.get_json()["code"] == missing.get_json()["code"]
    assert "Evan only" not in foreign.get_data(as_text=True)


def test_own_and_shared_recordings_still_read(world):
    assert _call(world, "GET", f"/api/v1/recordings/{world['mine']}").status_code == 200
    assert _call(world, "GET", f"/api/v1/recordings/{world['viewable']}").status_code == 200


@pytest.mark.parametrize("method,suffix,body", [
    ("PATCH", "", {"title": "taken"}),
    ("PUT", "/summary", {"summary": "taken"}),
    ("PUT", "/notes", {"notes": "taken"}),
    ("POST", "/tags", {"tag_ids": [1]}),
    ("PUT", "/speakers/assign", {"speaker_map": {"SPEAKER_00": "Alice"}}),
    ("DELETE", "", None),
    ("POST", "/delete-audio", None),
])
def test_foreign_and_missing_writes_match(world, method, suffix, body):
    kw = {"json": body} if body is not None else {}
    foreign = _call(world, method, f"/api/v1/recordings/{world['foreign']}{suffix}", **kw)
    missing = _call(world, method, f"/api/v1/recordings/{MISSING_ID}{suffix}", **kw)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.get_json()["code"] == missing.get_json()["code"]
    assert _exists(world["foreign"])
    with app.app_context():
        assert db.session.get(Recording, world["foreign"]).title == "Evan only"


def test_view_without_edit_stays_forbidden(world):
    rid = world["viewable"]
    r = _call(world, "PATCH", f"/api/v1/recordings/{rid}", json={"title": "edited"})
    assert r.status_code == 403 and r.get_json()["code"] == "permission_denied"
    r = _call(world, "DELETE", f"/api/v1/recordings/{rid}")
    assert r.status_code == 403 and r.get_json()["code"] == "delete_owner_only"
    assert _exists(rid)


def test_batch_update_uses_one_code_for_hidden_and_missing(world):
    r = _call(world, "PATCH", "/api/v1/recordings/batch",
              json={"recording_ids": [world["foreign"], MISSING_ID, world["viewable"]],
                    "updates": {"is_highlighted": True}})
    assert r.status_code == 200
    codes = {item["id"]: item.get("code") for item in r.get_json()["results"]}
    assert codes[world["foreign"]] == codes[MISSING_ID] == "not_found"
    assert codes[world["viewable"]] == "permission_denied"


def test_batch_transcribe_uses_one_code_for_hidden_and_missing(world):
    r = _call(world, "POST", "/api/v1/recordings/batch/transcribe",
              json={"recording_ids": [world["foreign"], MISSING_ID]})
    assert r.status_code == 200
    codes = [item.get("code") for item in r.get_json()["results"]]
    assert codes == ["not_found", "not_found"]


def test_batch_delete_hides_ids_but_keeps_admin_rights(world):
    r = _call(world, "DELETE", "/api/v1/recordings/batch",
              json={"recording_ids": [world["foreign"], MISSING_ID, world["viewable"]]})
    assert r.status_code == 200
    codes = {item["id"]: item.get("code") for item in r.get_json()["results"]}
    assert codes[world["foreign"]] == codes[MISSING_ID] == "not_found"
    assert codes[world["viewable"]] == "permission_denied"
    assert _exists(world["foreign"]) and _exists(world["viewable"])

    # An administrator could batch-delete another account's recording before
    # this change and still can.
    with patch("src.services.recording_deletion.cleanup_orphaned_speakers_quietly"):
        r = _call(world, "DELETE", "/api/v1/recordings/batch", token=world["admin_token"],
                  json={"recording_ids": [world["foreign"]]})
    assert r.status_code == 200 and r.get_json()["deleted"] == 1
    assert not _exists(world["foreign"])


@pytest.mark.parametrize("path", ["/api/recordings/{id}", "/recording/{id}/status",
                                  "/recording/{id}/download/transcript", "/audio/{id}"])
def test_web_routes_answer_the_same_way(world, path):
    """The web app's routes accept a full token too, so they follow the same rule."""
    foreign = _call(world, "GET", path.format(id=world["foreign"]))
    missing = _call(world, "GET", path.format(id=MISSING_ID))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.get_json()["code"] == missing.get_json()["code"]
