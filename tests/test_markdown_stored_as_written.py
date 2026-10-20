"""Summaries and notes are Markdown: they are stored as written and cleaned
only when converted to HTML (#439).

The HTML cleaner used to run on the Markdown when it was saved from the web
editor, which turned "> quote" into "&gt; quote" and "a -> b" into
"a -&gt; b". Saving through API v1 or the summarizer never cleaned it. Every
place that shows a summary or notes as HTML uses md_to_html, which cleans the
HTML it produces, so a tag in the stored text is never rendered.
"""
import re
import secrets

import pytest

from src.app import app, db
from src.models import APIToken, Recording, Share, User
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False

MARKDOWN = "> quoted line\n\n- a -> b\n- x < y\n"
HOSTILE = "Summary <details open ontoggle=alert(1)></details> <iframe srcdoc=x></iframe>"


@pytest.fixture
def world():
    with app.app_context():
        n = f"mdw_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x", can_share_publicly=True)
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="t", status="COMPLETED", original_filename="s.wav",
                      transcription="Speaker 1: hello")
        db.session.add(r)
        db.session.commit()
        plain = f"tok-{secrets.token_urlsafe(16)}"
        t = APIToken(user_id=u.id, token_hash=hash_token(plain), name="t", scopes=None)
        db.session.add(t)
        db.session.commit()
        ids = {"user": u.id, "rec": r.id, "token": plain}
    yield ids
    with app.app_context():
        db.session.rollback()
        Share.query.filter_by(recording_id=ids["rec"]).delete()
        APIToken.query.filter_by(user_id=ids["user"]).delete()
        db.session.delete(db.session.get(Recording, ids["rec"]))
        db.session.delete(db.session.get(User, ids["user"]))
        db.session.commit()


def _web(user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def _stored(rec_id):
    with app.app_context():
        r = db.session.get(Recording, rec_id)
        return r.summary, r.notes


def test_web_editor_stores_markdown_as_written(world):
    resp = _web(world["user"]).post("/save", json={"id": world["rec"], "summary": MARKDOWN, "notes": MARKDOWN})
    assert resp.status_code == 200
    assert _stored(world["rec"]) == (MARKDOWN, MARKDOWN)


def test_api_stores_markdown_as_written(world):
    resp = app.test_client().put(f"/api/v1/recordings/{world['rec']}/summary", json={"summary": MARKDOWN},
                                 headers={"Authorization": f"Bearer {world['token']}"})
    assert resp.status_code == 200
    assert _stored(world["rec"])[0] == MARKDOWN


def test_blockquote_survives_to_the_html(world):
    _web(world["user"]).post("/save", json={"id": world["rec"], "summary": MARKDOWN})
    with app.app_context():
        html = db.session.get(Recording, world["rec"]).to_dict()["summary_html"]
    assert "<blockquote" in html and "&amp;gt;" not in html


@pytest.mark.parametrize("path", ["web", "api"])
def test_a_stored_tag_is_never_rendered(world, path):
    if path == "web":
        _web(world["user"]).post("/save", json={"id": world["rec"], "summary": HOSTILE, "notes": HOSTILE})
    else:
        headers = {"Authorization": f"Bearer {world['token']}"}
        app.test_client().put(f"/api/v1/recordings/{world['rec']}/summary", json={"summary": HOSTILE}, headers=headers)
        app.test_client().patch(f"/api/v1/recordings/{world['rec']}", json={"notes": HOSTILE}, headers=headers)
    with app.app_context():
        data = db.session.get(Recording, world["rec"]).to_dict()
    for field in ("summary_html", "notes_html"):
        assert data[field], field
        for bad in ("<details", "ontoggle", "<iframe", "srcdoc"):
            assert bad not in data[field], (field, bad)


def test_share_page_renders_only_cleaned_html(world):
    _web(world["user"]).post("/save", json={"id": world["rec"], "summary": HOSTILE, "notes": HOSTILE})
    with app.app_context():
        share = Share(recording_id=world["rec"], user_id=world["user"], share_summary=True, share_notes=True)
        db.session.add(share)
        db.session.commit()
        public_id = share.public_id
    page = app.test_client().get(f"/share/{public_id}").get_data(as_text=True)
    assert "Summary" in page
    # The raw text travels only JSON-escaped in data-recording, for the copy button
    markup = re.sub(r"data-recording='[^']*'", "", page)
    for bad in ("<details", "ontoggle", "<iframe", "srcdoc"):
        assert bad not in markup, bad
