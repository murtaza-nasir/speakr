"""Where a title came from decides whether processing may replace it (#412).

- 'user': typed in an upload form, an edit or the API; never replaced.
- 'auto': produced by the title step; replaced when the recording is
  processed again, for example on a transcription reprocess (decision Da).
- NULL: recordings from before title_source existed keep the earlier rule
  (a placeholder gets a title, anything else is kept), so an upgrade changes
  no existing title.
A merge without a typed title is titled like an upload (decision Dg); without
an LLM it falls back to the "<first title> (merged)" name merges had before.

SHARED-DB: everything created here is removed afterwards.
"""

import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from src.services import recording_merge
from src.tasks import processing
from src.utils.titles import title_is_user_chosen, upload_title_source

app.config["WTF_CSRF_ENABLED"] = False
TRANSCRIPT = "Speaker 1: We agreed to move the launch to next quarter and review the budget on Monday."


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def made():
    ids = []
    yield ids
    with app.app_context():
        db.session.rollback()
        for model, oid in reversed(ids):
            obj = db.session.get(model, oid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _user(made):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"tsrc_{s}", email=f"tsrc_{s}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    made.append((User, u.id))
    return u


def _rec(made, user_id, **kw):
    kw.setdefault("original_filename", "call.wav")
    kw.setdefault("transcription", TRANSCRIPT)
    kw.setdefault("status", "PROCESSING")
    r = Recording(user_id=user_id, **kw)
    db.session.add(r)
    db.session.commit()
    made.append((Recording, r.id))
    return r


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("title,source,expected", [
    ("Board meeting", "user", True),
    ("Launch moved", "auto", False),
    ("Recording - call.wav", None, False),     # placeholder, any era
    ("Board meeting", None, True),             # legacy title: kept
    (None, None, False),
])
def test_title_is_user_chosen(title, source, expected):
    rec = SimpleNamespace(title=title, title_source=source, original_filename="call.wav")
    assert title_is_user_chosen(rec) is expected


@pytest.mark.parametrize("value,expected", [("Board meeting", "user"), ("  ", None), ("", None), (None, None)])
def test_upload_title_source(value, expected):
    assert upload_title_source(value) == expected


# ------------------------------------------- reprocess re-titles generated titles

def _run_title_step(rid, ai="Launch moved"):
    with patch.object(processing, "_generate_ai_title", return_value=ai), \
         patch.object(processing, "client", object()):
        processing.generate_title_task(app.app_context(), rid, will_auto_summarize=True)


@pytest.mark.parametrize("source,old,expected", [
    ("auto", "Old AI title", "Launch moved"),
    ("user", "Board meeting", "Board meeting"),
    (None, "Board meeting", "Board meeting"),
    (None, "Recording - call.wav", "Launch moved"),
])
def test_processing_again_replaces_only_generated_titles(made, source, old, expected):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id, title=old, title_source=source)
        _run_title_step(r.id)
        db.session.expire_all()
        out = db.session.get(Recording, r.id)
        assert out.title == expected
        if expected == "Launch moved":
            assert out.title_source == "auto"


def test_an_edited_title_is_kept_on_reprocess(made):
    with app.app_context():
        u = _user(made)
        r = _rec(made, u.id, title="Launch moved", title_source="auto", status="COMPLETED")
        c = _Client(app, app.response_class, use_cookies=True)
        with c.session_transaction() as sess:
            sess["_user_id"] = str(u.id)
        with patch("src.api.recordings.export_recording"):
            resp = c.post("/save", json={"id": r.id, "title": "Q3 planning"})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        db.session.expire_all()
        assert db.session.get(Recording, r.id).title_source == "user"
        _run_title_step(r.id)
        db.session.expire_all()
        assert db.session.get(Recording, r.id).title == "Q3 planning"


# ------------------------------------------------------------- merge (Dg)

def _merge(made, user, sources, title=None):
    with patch.object(recording_merge, "_validate_sources", return_value=sources):
        merged = recording_merge.create_merge_recording(user, [s.id for s in sources], title=title)
    made.append((Recording, merged.id))
    return merged


def test_a_merge_without_a_title_is_titled_like_an_upload(made):
    with app.app_context():
        u = _user(made)
        a = _rec(made, u.id, title="Standup", status="COMPLETED")
        b = _rec(made, u.id, title="Standup part 2", status="COMPLETED")
        m = _merge(made, u, [a, b])
        assert m.original_filename == "Standup (merged).m4a"
        assert m.title_source is None and not title_is_user_chosen(m)
        m.transcription = TRANSCRIPT
        db.session.commit()
        _run_title_step(m.id)
        db.session.expire_all()
        assert db.session.get(Recording, m.id).title == "Launch moved"


def test_a_merge_without_a_title_or_llm_keeps_the_old_name(made):
    with app.app_context():
        u = _user(made)
        a = _rec(made, u.id, title="Standup", status="COMPLETED")
        b = _rec(made, u.id, title="Standup part 2", status="COMPLETED")
        m = _merge(made, u, [a, b])
        with patch.object(processing, "client", None):
            processing.generate_title_task(app.app_context(), m.id, will_auto_summarize=True)
        db.session.expire_all()
        assert db.session.get(Recording, m.id).title == "Standup (merged)"


def test_a_typed_merge_title_is_kept(made):
    with app.app_context():
        u = _user(made)
        a = _rec(made, u.id, title="Standup", status="COMPLETED")
        b = _rec(made, u.id, title="Standup part 2", status="COMPLETED")
        m = _merge(made, u, [a, b], title="Weekly sync")
        assert (m.title, m.title_source) == ("Weekly sync", "user")
        m.transcription = TRANSCRIPT
        db.session.commit()
        _run_title_step(m.id)
        db.session.expire_all()
        assert db.session.get(Recording, m.id).title == "Weekly sync"
