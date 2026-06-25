"""Authorization regression tests for the recording *download* endpoints.

Mutation testing (2026-06-25) found that disabling the ``has_recording_access``
check in ``download_transcript_with_template`` broke no test, and a route-map
audit showed the four Word/transcript download endpoints
(``/download/{transcript,summary,chat,notes}``) had ZERO authorization coverage.
A real access-control regression there would leak another user's transcript,
summary, chat, or notes. These tests close that gap: a non-owner must get 403,
and the owner must clear the authorization gate (anything but 403).

NOTE on the harness: each client request is made OUTSIDE an outer app context
with a fresh test client, because Flask-Login caches ``current_user`` on the
app-context ``g``; reusing one context across two different logins would make
the second request run as the first user (the lesson from test_shares_authz.py).
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording

app.config["WTF_CSRF_ENABLED"] = False


def _mk_user(prefix):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"{prefix}_{s}", email=f"{prefix}_{s}@example.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


@pytest.mark.parametrize("method,suffix", [
    ("get", "/download/transcript"),
    ("get", "/download/summary"),
    ("post", "/download/chat"),
    ("get", "/download/notes"),
])
def test_download_endpoints_reject_non_owner(method, suffix):
    # --- setup in a short-lived context, then exit before any request ---
    with app.app_context():
        owner = _mk_user("dl_owner")
        other = _mk_user("dl_other")
        rec = Recording(
            user_id=owner.id, title="r", status="COMPLETED",
            transcription="hello world", summary="a summary",
        )
        db.session.add(rec)
        db.session.commit()
        rid, owner_id, other_id = rec.id, owner.id, other.id

    # --- a non-owner with no share must be denied BEFORE any content is served ---
    nc = app.test_client()
    _login(nc, other_id)
    resp = getattr(nc, method)(f"/recording/{rid}{suffix}")
    assert resp.status_code == 403, (
        f"{method.upper()} {suffix} returned {resp.status_code} for a non-owner; "
        f"expected 403 (auth-bypass / content leak)"
    )

    # --- the owner must clear the authorization gate (fresh client => fresh g) ---
    oc = app.test_client()
    _login(oc, owner_id)
    resp_owner = getattr(oc, method)(f"/recording/{rid}{suffix}")
    assert resp_owner.status_code != 403, (
        f"{method.upper()} {suffix} wrongly denied the owner ({resp_owner.status_code})"
    )

    # --- cleanup ---
    with app.app_context():
        r = db.session.get(Recording, rid)
        if r:
            db.session.delete(r)
        for uid in (owner_id, other_id):
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
        db.session.commit()
