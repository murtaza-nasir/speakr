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
