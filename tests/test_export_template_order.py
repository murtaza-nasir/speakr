"""Export template order: tag, then folder, then the user's default (#412, Dc).

Every other per-recording setting ranks a tag above the recording's folder;
export templates ranked the folder first. They now follow the same order.

SHARED-DB: the user and templates created here are removed afterwards.
"""

import os
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.file_exporter import get_user_export_template
from src.models import ExportTemplate, User


@pytest.fixture
def templates():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"exo_{s}", email=f"exo_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        made = {}
        for key, default in (("tag", False), ("folder", False), ("user", True)):
            t = ExportTemplate(user_id=u.id, name=f"{key}-{s}", template=f"{key}", is_default=default)
            db.session.add(t)
            db.session.commit()
            made[key] = t.id
        made["user_id"] = u.id
    yield made
    with app.app_context():
        for key in ("tag", "folder", "user"):
            t = db.session.get(ExportTemplate, made[key])
            if t is not None:
                db.session.delete(t)
        db.session.delete(db.session.get(User, made["user_id"]))
        db.session.commit()


def _rec(tag_tid=None, folder_tid=None):
    tags = [SimpleNamespace(export_template_id=tag_tid)] if tag_tid else []
    folder = SimpleNamespace(export_template_id=folder_tid) if folder_tid else None
    return SimpleNamespace(tags=tags, folder=folder)


@pytest.mark.parametrize("tag,folder,expected", [
    (True, True, "tag"),
    (False, True, "folder"),
    (True, False, "tag"),
    (False, False, "user"),
])
def test_tag_then_folder_then_user_default(templates, tag, folder, expected):
    with app.app_context():
        user = db.session.get(User, templates["user_id"])
        rec = _rec(templates["tag"] if tag else None, templates["folder"] if folder else None)
        assert get_user_export_template(user, rec).id == templates[expected]
