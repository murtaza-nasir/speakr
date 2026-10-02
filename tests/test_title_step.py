"""The title step is shared by processing and the regenerate-title endpoint (#412).

Before #412 the regenerate button saved the raw AI title and ignored the
naming template, and a folder's naming template was saved but never used.
These tests pin the shared order (tag, then folder, then the owner's default),
the template-only case without an LLM call, and that the background task and
the button produce the same title for the same recording.

SHARED-DB: everything created here is removed afterwards.
"""

import os
import secrets
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Folder, NamingTemplate, Recording, RecordingTag, Tag, User
from src.services.titling import compute_title, resolve_naming_template
from src.tasks import processing

app.config["WTF_CSRF_ENABLED"] = False
TRANSCRIPT = "Speaker 1: We agreed to move the launch to next quarter and review the budget on Monday."


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    """A user with a recording, a tag, a folder and three naming templates."""
    with app.app_context():
        name = f"titlestep_{secrets.token_hex(4)}"
        user = User(username=name, email=f"{name}@local.test")
        db.session.add(user)
        db.session.commit()
        t_tag = NamingTemplate(user_id=user.id, name="tag-t", template="TAG {{ai_title}}")
        t_folder = NamingTemplate(user_id=user.id, name="folder-t", template="FOLDER {{ai_title}}")
        t_user = NamingTemplate(user_id=user.id, name="user-t", template="USER {{ai_title}}")
        db.session.add_all([t_tag, t_folder, t_user])
        db.session.commit()
        tag = Tag(name=f"{name}-tag", user_id=user.id)
        folder = Folder(name=f"{name}-folder", user_id=user.id)
        db.session.add_all([tag, folder])
        db.session.commit()
        rec = Recording(user_id=user.id, title=f"Recording - {name}.wav", status="COMPLETED",
                        original_filename=f"{name}.wav", transcription=TRANSCRIPT,
                        meeting_date=datetime(2026, 10, 1, 6, 13))
        db.session.add(rec)
        db.session.commit()
        ids = dict(user=user.id, rec=rec.id, tag=tag.id, folder=folder.id,
                   t_tag=t_tag.id, t_folder=t_folder.id, t_user=t_user.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        RecordingTag.query.filter_by(recording_id=ids["rec"]).delete()
        for model, key in ((Recording, "rec"), (Tag, "tag"), (Folder, "folder")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()
        u = db.session.get(User, ids["user"])
        if u is not None:
            u.default_naming_template_id = None
            db.session.commit()
        for key in ("t_tag", "t_folder", "t_user"):
            obj = db.session.get(NamingTemplate, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.delete(db.session.get(User, ids["user"]))
        db.session.commit()


def _setup(ids, tag=False, folder=False, user=False):
    rec = db.session.get(Recording, ids["rec"])
    if tag:
        db.session.get(Tag, ids["tag"]).naming_template_id = ids["t_tag"]
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=ids["tag"], order=0))
    if folder:
        db.session.get(Folder, ids["folder"]).naming_template_id = ids["t_folder"]
        rec.folder_id = ids["folder"]
    if user:
        db.session.get(User, ids["user"]).default_naming_template_id = ids["t_user"]
    db.session.commit()
    return db.session.get(Recording, ids["rec"])


# ------------------------------------------------------------- resolution order

@pytest.mark.parametrize("levels,expected", [
    (dict(tag=True, folder=True, user=True), "tag"),
    (dict(folder=True, user=True), "folder"),
    (dict(user=True), "user"),
    (dict(), None),
])
def test_naming_template_order_is_tag_then_folder_then_user(world, levels, expected):
    with app.app_context():
        rec = _setup(world, **levels)
        _, source = resolve_naming_template(rec)
        assert source == expected


def test_folder_template_is_used_for_the_title(world):
    with app.app_context():
        rec = _setup(world, folder=True)
        with patch.object(processing, "_generate_ai_title", return_value="Launch moved"), \
             patch.object(processing, "client", object()):
            assert compute_title(rec) == "FOLDER Launch moved"


def test_template_without_ai_title_makes_no_llm_call(world):
    with app.app_context():
        rec = _setup(world)
        t = db.session.get(NamingTemplate, world["t_user"])
        t.template = "{{date}} call"
        db.session.get(User, world["user"]).default_naming_template_id = t.id
        db.session.commit()
        rec = db.session.get(Recording, world["rec"])
        with patch.object(processing, "_generate_ai_title", side_effect=AssertionError("LLM called")), \
             patch.object(processing, "client", None):
            assert compute_title(rec) == "2026-10-01 call"


def test_no_template_and_no_llm_falls_back_to_the_filename(world):
    with app.app_context():
        rec = _setup(world)
        with patch.object(processing, "client", None):
            assert compute_title(rec) == os.path.splitext(rec.original_filename)[0]


# ------------------------------------------------------ regenerate == pipeline

def _regenerate(ids):
    with app.app_context():
        app.test_client_class = _Client
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(ids["user"])
        with patch.object(processing, "_generate_ai_title", return_value="Launch moved"), \
             patch("src.api.recordings.client", object()), \
             patch.object(processing, "client", object()), \
             patch("src.api.recordings.export_recording") as exp:
            resp = c.post(f"/recording/{ids['rec']}/regenerate_title")
        return resp, exp


def test_regenerate_applies_the_tag_template(world):
    with app.app_context():
        _setup(world, tag=True, user=True)
    resp, exp = _regenerate(world)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["title"] == "TAG Launch moved"
    exp.assert_called_once_with(world["rec"])


def test_regenerate_with_a_template_only_title_needs_no_llm(world):
    with app.app_context():
        _setup(world, user=True)
        db.session.get(NamingTemplate, world["t_user"]).template = "{{date}} call"
        db.session.commit()
        app.test_client_class = _Client
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(world["user"])
        with patch("src.api.recordings.client", None), patch.object(processing, "client", None), \
             patch("src.api.recordings.export_recording"):
            resp = c.post(f"/recording/{world['rec']}/regenerate_title")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["title"] == "2026-10-01 call"


def test_background_task_and_regenerate_give_the_same_title(world):
    with app.app_context():
        _setup(world, folder=True, user=True)
        with patch.object(processing, "_generate_ai_title", return_value="Launch moved"), \
             patch.object(processing, "client", object()):
            processing.generate_title_task(app.app_context(), world["rec"], will_auto_summarize=True)
        task_title = db.session.get(Recording, world["rec"]).title
    resp, _ = _regenerate(world)
    assert task_title == "FOLDER Launch moved"
    assert resp.get_json()["title"] == task_title
