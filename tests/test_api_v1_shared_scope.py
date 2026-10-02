"""scope=own|shared|all on the list, the changes feed and search (mailr spec G4).

SHARED-DB: users, tokens, shares, states and recordings are removed afterwards.
"""

import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (APIToken, InternalShare, Recording, RecordingTombstone, SharedRecordingState, User)
from src.services import recording_changes as rc
from src.utils.token_auth import hash_token


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"sc_{s}", email=f"sc_{s}@local.test", password="x", name="Me Myself")
        evan = User(username=f"evan_{s}", email=f"evan_{s}@local.test", password="x", name="Evan Ross")
        db.session.add_all([me, evan])
        db.session.commit()
        mine = Recording(user_id=me.id, title="Mine budget", status="COMPLETED", original_filename="a.wav",
                         transcription="We discussed the budget.")
        manual = Recording(user_id=evan.id, title="Manual budget", status="COMPLETED", original_filename="b.wav",
                           transcription="The budget freeze.")
        by_tag = Recording(user_id=evan.id, title="Tag share", status="COMPLETED", original_filename="c.wav")
        by_folder = Recording(user_id=evan.id, title="Folder share", status="COMPLETED", original_filename="d.wav")
        busy = Recording(user_id=evan.id, title="Still processing", status="PROCESSING", original_filename="e.wav")
        private = Recording(user_id=evan.id, title="Evan only", status="COMPLETED", original_filename="f.wav")
        db.session.add_all([mine, manual, by_tag, by_folder, busy, private])
        db.session.commit()
        for rec, source in ((manual, "manual"), (by_tag, "group_tag"), (by_folder, "group_folder"), (busy, "manual")):
            db.session.add(InternalShare(recording_id=rec.id, owner_id=evan.id, shared_with_user_id=me.id,
                                         can_edit=(source == "manual"), source_type=source))
        db.session.add(SharedRecordingState(recording_id=manual.id, user_id=me.id, is_inbox=False,
                                            is_highlighted=True, is_archived=False))
        plain = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add(APIToken(user_id=me.id, token_hash=hash_token(plain), name="sc"))
        db.session.commit()
        ids = dict(me=me.id, evan=evan.id, mine=mine.id, manual=manual.id, by_tag=by_tag.id,
                   by_folder=by_folder.id, busy=busy.id, private=private.id, token=plain)
    with patch("src.app.ENABLE_INTERNAL_SHARING", True):
        yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["me"], ids["evan"]]
        recs = [r.id for r in Recording.query.filter(Recording.user_id.in_(users))]
        SharedRecordingState.query.filter(SharedRecordingState.recording_id.in_(recs)).delete(synchronize_session=False)
        InternalShare.query.filter(InternalShare.recording_id.in_(recs)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        RecordingTombstone.query.filter(RecordingTombstone.user_id.in_(users)).delete(synchronize_session=False)
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _get(world, path):
    with app.test_client() as c:
        resp = c.get(path, headers={"Authorization": f"Bearer {world['token']}"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _ids(body):
    return {r["id"] for r in body["recordings"]}


def test_shared_lists_every_kind_of_share_but_not_processing(world):
    body = _get(world, "/api/v1/recordings?scope=shared")
    assert _ids(body) == {world["manual"], world["by_tag"], world["by_folder"]}
    item = next(r for r in body["recordings"] if r["id"] == world["manual"])
    assert item["is_shared"] is True and item["owner"]["id"] == world["evan"]
    assert item["share"]["source"] == "manual" and item["share"]["can_edit"] is True
    assert item["share"]["shared_at"].endswith("Z")
    assert (item["is_inbox"], item["is_highlighted"]) == (False, True)        # the recipient's own flags
    sources = {r["id"]: r["share"]["source"] for r in body["recordings"]}
    assert sources[world["by_tag"]] == "group_tag" and sources[world["by_folder"]] == "group_folder"


def test_own_is_the_default_and_all_merges(world):
    own = _get(world, "/api/v1/recordings")
    assert _ids(own) == {world["mine"]} and own["recordings"][0]["is_shared"] is False
    assert own["recordings"][0]["owner"] is None and own["recordings"][0]["share"] is None
    every = _get(world, "/api/v1/recordings?scope=all")
    assert _ids(every) == {world["mine"], world["manual"], world["by_tag"], world["by_folder"]}
    pages, seen = 0, set()
    for page in (1, 2, 3, 4):
        body = _get(world, f"/api/v1/recordings?scope=all&per_page=1&page={page}")
        seen |= _ids(body)
    assert seen == _ids(every) and body["pagination"]["total"] == 4


def test_filters_on_the_shared_list(world):
    assert _ids(_get(world, "/api/v1/recordings?scope=all&starred=true")) == {world["manual"]}
    assert world["manual"] not in _ids(_get(world, "/api/v1/recordings?scope=all&inbox=true"))
    assert _ids(_get(world, f"/api/v1/recordings?scope=all&owner_id={world['me']}")) == {world["mine"]}


def test_sharing_off_gives_nothing_shared(world):
    with patch("src.app.ENABLE_INTERNAL_SHARING", False):
        assert _get(world, "/api/v1/recordings?scope=shared")["recordings"] == []
        assert _ids(_get(world, "/api/v1/recordings?scope=all")) == {world["mine"]}


def test_owner_names_follow_the_ui_setting(world):
    with patch.dict(os.environ, {"SHOW_USERNAMES_IN_UI": "false"}):
        hidden = _get(world, "/api/v1/recordings?scope=shared")["recordings"][0]["owner"]
    assert hidden["username"] is None and hidden["name"] is None
    with patch.dict(os.environ, {"SHOW_USERNAMES_IN_UI": "true"}):
        shown = next(r for r in _get(world, "/api/v1/recordings?scope=shared")["recordings"]
                     if r["id"] == world["manual"])["owner"]
    assert shown["name"] == "Evan Ross" and shown["username"].startswith("evan_")


def test_the_feed_reports_a_revoked_share(world):
    with patch.object(rc, "SETTLE_SECONDS", 0):
        start = _get(world, "/api/v1/recordings/changes?scope=all")
        assert {c["recording"]["id"] for c in start["changes"]} == {world["mine"], world["manual"], world["by_tag"],
                                                                   world["by_folder"]}
        with app.app_context():
            db.session.delete(InternalShare.query.filter_by(recording_id=world["by_tag"]).one())
            db.session.commit()
        after = _get(world, f"/api/v1/recordings/changes?scope=all&cursor={start['next_cursor']}")
        own = _get(world, f"/api/v1/recordings/changes?scope=own&cursor={start['next_cursor']}")
    revoked = [c for c in after["changes"] if c["type"] == "delete"]
    assert [(c["id"], c["reason"]) for c in revoked] == [(world["by_tag"], "access_revoked")]
    assert not [c for c in own["changes"] if c["type"] == "delete"]


def test_search_scope(world):
    own = _get(world, "/api/v1/search?q=budget")
    shared = _get(world, "/api/v1/search?q=budget&scope=shared")
    every = _get(world, "/api/v1/search?q=budget&scope=all")
    assert {h["recording_id"] for h in own["results"]} == {world["mine"]}
    assert {h["recording_id"] for h in shared["results"]} == {world["manual"]}
    assert {h["recording_id"] for h in every["results"]} == {world["mine"], world["manual"]}
