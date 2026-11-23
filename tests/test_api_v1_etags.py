"""Conditional GETs on one recording (mailr spec G11).

The six read routes send a weak ETag and answer If-None-Match with 304 and no
body. The tag changes when the recording changes and when anything else in
the answer changes (here: the user's transcript template). A caller without
access gets 403 or 404, never 304.

SHARED-DB: users, tokens, templates and the recording are removed afterwards.
"""

import json
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, TranscriptTemplate, User
from src.utils.token_auth import hash_token

SEGMENTS = [{"speaker": "Jane", "sentence": "Launch moves to Q3.", "start_time": 0.0, "end_time": 2.0}]
ROUTES = ["", "/transcript", "/transcript?format=text", "/summary", "/notes", "/events", "/speakers"]


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"etag_{s}", email=f"etag_{s}@local.test", password="x")
        other = User(username=f"etago_{s}", email=f"etago_{s}@local.test", password="x")
        db.session.add_all([me, other])
        db.session.commit()
        rec = Recording(user_id=me.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        transcription=json.dumps(SEGMENTS), summary="## Summary", notes="n")
        db.session.add(rec)
        tokens = {}
        for who in (me, other):
            plain = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=who.id, token_hash=hash_token(plain), name="etag"))
            tokens[who.id] = plain
        db.session.commit()
        ids = dict(me=me.id, other=other.id, rec=rec.id, tokens=tokens)
    yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["me"], ids["other"]]
        TranscriptTemplate.query.filter(TranscriptTemplate.user_id.in_(users)).delete(synchronize_session=False)
        Recording.query.filter_by(id=ids["rec"]).delete()
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _h(world, who="me", etag=None):
    headers = {"Authorization": f"Bearer {world['tokens'][world[who]]}"}
    if etag:
        headers["If-None-Match"] = etag
    return headers


@pytest.mark.parametrize("route", ROUTES)
def test_unchanged_answers_304_and_a_change_answers_200(world, route):
    url = f"/api/v1/recordings/{world['rec']}{route}"
    with app.test_client() as c:
        first = c.get(url, headers=_h(world))
        etag = first.headers.get("ETag")
        assert first.status_code == 200 and etag and etag.startswith('W/"')
        assert first.headers["Cache-Control"] == "private, no-cache"
        again = c.get(url, headers=_h(world, etag=etag))
        assert again.status_code == 304 and again.get_data() == b"" and again.headers["ETag"] == etag
        assert c.patch(f"/api/v1/recordings/{world['rec']}", json={"summary": "## New", "notes": "m",
                                                                   "participants": "Jane"},
                       headers=_h(world)).status_code == 200
        after = c.get(url, headers=_h(world, etag=etag))
    if route in ("/events",):
        assert after.status_code in (200, 304)   # unaffected content may keep its body
    else:
        assert after.status_code == 200 and after.headers["ETag"] != etag


def test_the_text_transcript_follows_the_users_template(world):
    url = f"/api/v1/recordings/{world['rec']}/transcript?format=text"
    with app.test_client() as c:
        etag = c.get(url, headers=_h(world)).headers["ETag"]
        with app.app_context():
            db.session.add(TranscriptTemplate(user_id=world["me"], name="Mine", template="{{speaker}} said {{text}}",
                                              is_default=True))
            db.session.commit()
        changed = c.get(url, headers=_h(world, etag=etag))
    assert changed.status_code == 200 and changed.headers["ETag"] != etag


@pytest.mark.parametrize("route", ROUTES)
def test_no_access_never_gets_304(world, route):
    url = f"/api/v1/recordings/{world['rec']}{route}"
    with app.test_client() as c:
        etag = c.get(url, headers=_h(world)).headers["ETag"]
        resp = c.get(url, headers=_h(world, who="other", etag=etag))
    assert resp.status_code in (403, 404) and "ETag" not in resp.headers


# ---- the recordings list (D#662) ---------------------------------------------

LIST = "/api/v1/recordings"


def test_the_list_answers_304_until_a_recording_on_the_page_changes(world):
    with app.test_client() as c:
        first = c.get(LIST, headers=_h(world))
        etag = first.headers.get("ETag")
        assert first.status_code == 200 and etag and etag.startswith('W/"')
        assert first.headers["Cache-Control"] == "private, no-cache"
        again = c.get(LIST, headers=_h(world, etag=etag))
        assert again.status_code == 304 and again.get_data() == b""
        assert again.headers["ETag"] == etag and again.headers["Cache-Control"] == "private, no-cache"
        assert c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Sync, renamed"},
                       headers=_h(world)).status_code == 200
        after = c.get(LIST, headers=_h(world, etag=etag))
    assert after.status_code == 200 and after.headers["ETag"] != etag
    assert [r["title"] for r in after.get_json()["recordings"]] == ["Sync, renamed"]


def test_the_list_tag_follows_the_parameters_and_the_total(world):
    with app.test_client() as c:
        tags = {query: c.get(LIST + query, headers=_h(world)).headers["ETag"]
                for query in ("", "?page=1", "?per_page=10", "?sort_order=asc", "?status=completed",
                              "?q=Sync", "?scope=all")}
        assert len(set(tags.values())) == len(tags), tags
        # A parameter that changes the page answers 200 with the other tag
        resp = c.get(LIST + "?per_page=10", headers=_h(world, etag=tags[""]))
        assert resp.status_code == 200 and resp.headers["ETag"] == tags["?per_page=10"]
        with app.app_context():
            db.session.add(Recording(user_id=world["me"], title="Second", status="COMPLETED",
                                     original_filename="t.wav"))
            db.session.commit()
        try:
            grown = c.get(LIST, headers=_h(world, etag=tags[""]))
            assert grown.status_code == 200 and grown.get_json()["pagination"]["total"] == 2
        finally:
            with app.app_context():
                Recording.query.filter_by(user_id=world["me"], title="Second").delete()
                db.session.commit()


def test_the_list_tag_is_the_callers_own(world):
    with app.test_client() as c:
        mine = c.get(LIST, headers=_h(world)).headers["ETag"]
        theirs = c.get(LIST, headers=_h(world, who="other", etag=mine))
    assert theirs.status_code == 200 and theirs.headers["ETag"] != mine
    assert theirs.get_json()["recordings"] == []


def test_the_list_checks_scope_auth_and_parameters_before_304(world):
    with app.test_client() as c:
        etag = c.get(LIST, headers=_h(world)).headers["ETag"]
        anonymous = c.get(LIST, headers={"If-None-Match": etag})
        bad = c.get(LIST + "?scope=nobody", headers=_h(world, etag=etag))
        with app.app_context():
            plain = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=world["me"], token_hash=hash_token(plain), name="etag-write",
                                    scopes=json.dumps(["write"])))
            db.session.commit()
        unscoped = c.get(LIST, headers={"Authorization": f"Bearer {plain}", "If-None-Match": etag})
    assert anonymous.status_code == 401 and "ETag" not in anonymous.headers
    assert bad.status_code == 400 and "ETag" not in bad.headers
    assert unscoped.status_code == 403 and "ETag" not in unscoped.headers
