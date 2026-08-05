"""Tests for configurable auto-export filename templates (#348).

Covers:
  - Default template (NULL) yields the legacy "recording_{id}" name
  - Custom templates render {{id}}/{{title}}/{{filename}}/date variables
  - Filesystem-unsafe characters are stripped, whitespace collapsed
  - Case-insensitive collisions with another recording's stored name append _{id}
  - Re-export writes to the stored (authoritative) filename
  - mark_export_as_deleted renames the stored name (incl. after row deletion)
  - POST /api/export/apply-filename-template renames on disk + updates DB
  - Unauthenticated requests to the rename endpoint are rejected
  - POST /account validation for the template setting

File exports go to a per-test temporary directory (AUTO_EXPORT_DIR patched).

SHARED-DB: every assertion is scoped to the user/recordings the test created.
"""

import os
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording
from src import file_exporter
from src.file_exporter import (
    render_export_filename,
    sanitize_export_filename,
    export_recording,
    mark_export_as_deleted,
    apply_filename_template_for_user,
)

app.config["WTF_CSRF_ENABLED"] = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _mk_user(prefix="xf", template=None):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
        export_filename_template=template,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _mk_recording(user, **kwargs):
    rec = Recording(
        user_id=user.id,
        title=kwargs.pop("title", f"rec_{uuid.uuid4().hex[:8]}"),
        status=kwargs.pop("status", "COMPLETED"),
        audio_path=kwargs.pop("audio_path", "local://recordings/x.mp3"),
        original_filename=kwargs.pop("original_filename", "x.mp3"),
        transcription=kwargs.pop("transcription", "some transcript text"),
        **kwargs,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


@contextmanager
def _export_env():
    """Enable auto-export against a throwaway directory."""
    tmpdir = tempfile.mkdtemp(prefix="speakr_export_test_")
    try:
        with patch.object(file_exporter, "ENABLE_AUTO_EXPORT", True), \
             patch.object(file_exporter, "AUTO_EXPORT_DIR", tmpdir), \
             patch.object(file_exporter, "AUTO_EXPORT_TRANSCRIPTION", True), \
             patch.object(file_exporter, "AUTO_EXPORT_SUMMARY", True):
            yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _user_dir(tmpdir, user):
    from werkzeug.utils import secure_filename
    return os.path.join(tmpdir, secure_filename(user.username))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_default_template_yields_legacy_name():
    with app.app_context():
        user = _mk_user(template=None)
        rec = _mk_recording(user, title="Anything At All")
        assert render_export_filename(rec, user) == f"recording_{rec.id}"


def test_custom_template_renders_variables():
    with app.app_context():
        user = _mk_user(template="{{date}} {{title}} ({{filename}}) [{{id}}]")
        rec = _mk_recording(
            user,
            title="Weekly Sync",
            original_filename="audio file.m4a",
            meeting_date=datetime(2025, 3, 14, 9, 30),
        )
        assert render_export_filename(rec, user) == (
            f"2025-03-14 Weekly Sync (audio file) [{rec.id}]"
        )


def test_date_variables_fall_back_to_created_at():
    with app.app_context():
        user = _mk_user(template="{{year}}-{{month}}-{{day}} {{time}}")
        rec = _mk_recording(user, meeting_date=None,
                            created_at=datetime(2024, 12, 31, 23, 5))
        assert render_export_filename(rec, user) == "2024-12-31 23-05"


def test_unsafe_characters_are_stripped():
    with app.app_context():
        user = _mk_user(template="{{title}}")
        rec = _mk_recording(user, title='A/B\\C:D*E?F"G<H>I|J')
        assert render_export_filename(rec, user) == "ABCDEFGHIJ"


def test_whitespace_collapsed_and_edges_trimmed():
    assert sanitize_export_filename("  Hello   World.. ") == "Hello World"
    assert sanitize_export_filename("...") == ""
    # Unicode letters must survive (no secure_filename mangling)
    assert sanitize_export_filename("Réunion générale — Überblick") == "Réunion générale — Überblick"


def test_empty_render_falls_back_to_legacy_name():
    with app.app_context():
        user = _mk_user(template="{{title}}")
        rec = _mk_recording(user, title="???")  # sanitizes to empty
        assert render_export_filename(rec, user) == f"recording_{rec.id}"


def test_length_capped():
    with app.app_context():
        user = _mk_user(template="{{title}}")
        rec = _mk_recording(user, title="x" * 400)
        assert len(render_export_filename(rec, user)) <= 150


def test_collision_appends_id():
    with app.app_context():
        user = _mk_user(template="{{title}}")
        rec1 = _mk_recording(user, title="Meeting")
        rec2 = _mk_recording(user, title="meeting")  # case-insensitive clash
        rec1.export_filename = render_export_filename(rec1, user)
        db.session.commit()
        assert rec1.export_filename == "Meeting"
        assert render_export_filename(rec2, user) == f"meeting_{rec2.id}"


def test_no_collision_across_users():
    with app.app_context():
        user_a = _mk_user(template="{{title}}")
        user_b = _mk_user(template="{{title}}")
        rec_a = _mk_recording(user_a, title="Standup")
        rec_a.export_filename = "Standup"
        db.session.commit()
        rec_b = _mk_recording(user_b, title="Standup")
        assert render_export_filename(rec_b, user_b) == "Standup"


# --------------------------------------------------------------------------- #
# Export flow
# --------------------------------------------------------------------------- #

def test_export_stores_filename_and_reexport_uses_it():
    with app.app_context(), _export_env() as tmpdir:
        user = _mk_user(template="{{title}}")
        rec = _mk_recording(user, title="Project Kickoff")

        path1 = export_recording(rec.id)
        assert path1 is not None
        assert os.path.basename(path1) == "Project Kickoff.md"
        assert os.path.exists(path1)

        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        assert rec.export_filename == "Project Kickoff"

        # Changing the template does NOT move already-exported files:
        # the stored name stays authoritative on re-export.
        user = db.session.get(User, user.id)
        user.export_filename_template = "{{id}}_{{title}}"
        db.session.commit()

        path2 = export_recording(rec.id)
        assert path2 == path1
        exported = os.listdir(_user_dir(tmpdir, user))
        assert exported == ["Project Kickoff.md"]


def test_export_default_template_writes_legacy_path():
    with app.app_context(), _export_env() as tmpdir:
        user = _mk_user(template=None)
        rec = _mk_recording(user)
        path = export_recording(rec.id)
        assert os.path.basename(path) == f"recording_{rec.id}.md"
        db.session.expire_all()
        assert db.session.get(Recording, rec.id).export_filename == f"recording_{rec.id}"


# --------------------------------------------------------------------------- #
# Deletion marking
# --------------------------------------------------------------------------- #

def test_mark_export_as_deleted_uses_stored_name():
    with app.app_context(), _export_env() as tmpdir:
        user = _mk_user(template="{{title}}")
        rec = _mk_recording(user, title="Board Meeting")
        rec.export_filename = "Board Meeting"
        db.session.commit()

        udir = _user_dir(tmpdir, user)
        os.makedirs(udir, exist_ok=True)
        with open(os.path.join(udir, "Board Meeting.md"), "w") as f:
            f.write("content")

        rec_id = rec.id
        # Production order: row is deleted BEFORE mark_export_as_deleted runs.
        db.session.delete(rec)
        db.session.commit()

        new_path = mark_export_as_deleted(rec_id)
        assert new_path is not None
        assert os.path.basename(new_path) == "[deleted]_Board Meeting.md"
        assert os.path.exists(new_path)
        assert not os.path.exists(os.path.join(udir, "Board Meeting.md"))


def test_mark_export_as_deleted_legacy_fallback():
    with app.app_context(), _export_env() as tmpdir:
        user = _mk_user()
        rec = _mk_recording(user)  # no export_filename stored
        rec_id = rec.id

        udir = _user_dir(tmpdir, user)
        os.makedirs(udir, exist_ok=True)
        with open(os.path.join(udir, f"recording_{rec_id}.md"), "w") as f:
            f.write("content")

        db.session.delete(rec)
        db.session.commit()

        new_path = mark_export_as_deleted(rec_id)
        assert new_path is not None
        assert os.path.basename(new_path) == f"[deleted]_recording_{rec_id}.md"


# --------------------------------------------------------------------------- #
# Rename migration endpoint
# --------------------------------------------------------------------------- #

def test_apply_filename_template_endpoint_renames_disk_and_db():
    with app.app_context(), _export_env() as tmpdir:
        user = _mk_user(template=None)
        rec1 = _mk_recording(user, title="Alpha Review")
        rec2 = _mk_recording(user, title="Beta Review")
        rec3 = _mk_recording(user, title="Never Exported")

        # Simulate legacy exports on disk (NULL export_filename in DB).
        udir = _user_dir(tmpdir, user)
        os.makedirs(udir, exist_ok=True)
        for rec in (rec1, rec2):
            with open(os.path.join(udir, f"recording_{rec.id}.md"), "w") as f:
                f.write("content")
        # A deleted-marked export keeps its prefix through the rename.
        rec4 = _mk_recording(user, title="Old One")
        rec4.export_filename = f"recording_{rec4.id}"
        db.session.commit()
        with open(os.path.join(udir, f"[deleted]_recording_{rec4.id}.md"), "w") as f:
            f.write("content")

        user.export_filename_template = "{{title}}"
        db.session.commit()

        client = app.test_client()
        _login(client, user)
        resp = client.post("/api/export/apply-filename-template")
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["renamed"] == 3
        assert result["errors"] == 0
        assert result["skipped"] >= 1  # rec3 had no file on disk

        files = sorted(os.listdir(udir))
        assert "Alpha Review.md" in files
        assert "Beta Review.md" in files
        assert "[deleted]_Old One.md" in files
        assert f"recording_{rec1.id}.md" not in files

        db.session.expire_all()
        assert db.session.get(Recording, rec1.id).export_filename == "Alpha Review"
        assert db.session.get(Recording, rec2.id).export_filename == "Beta Review"
        assert db.session.get(Recording, rec4.id).export_filename == "Old One"


def test_apply_filename_template_skips_missing_files():
    with app.app_context(), _export_env():
        user = _mk_user(template="{{title}}")
        rec = _mk_recording(user, title="Ghost")
        rec.export_filename = "Some Old Name"  # file never written / missing
        db.session.commit()

        result = apply_filename_template_for_user(user.id)
        assert result["errors"] == 0
        assert result["renamed"] == 0
        assert result["skipped"] >= 1
        db.session.expire_all()
        # Stored name untouched because there was nothing to rename.
        assert db.session.get(Recording, rec.id).export_filename == "Some Old Name"


def test_apply_filename_template_endpoint_disabled_returns_400():
    with app.app_context():
        user = _mk_user()
        client = app.test_client()
        _login(client, user)
        with patch.object(file_exporter, "ENABLE_AUTO_EXPORT", False):
            resp = client.post("/api/export/apply-filename-template")
        assert resp.status_code == 400


def test_apply_filename_template_requires_login():
    client = app.test_client()
    with patch.object(file_exporter, "ENABLE_AUTO_EXPORT", True):
        resp = client.post("/api/export/apply-filename-template")
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------- #
# Settings form (POST /account)
# --------------------------------------------------------------------------- #

def test_account_post_saves_template():
    with app.app_context():
        user = _mk_user()
        client = app.test_client()
        _login(client, user)
        resp = client.post(
            "/account",
            data={"export_filename_template": "{{date}} {{title}}"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(User, user.id).export_filename_template == "{{date}} {{title}}"


def test_account_post_empty_template_saves_null():
    with app.app_context():
        user = _mk_user(template="{{title}}")
        client = app.test_client()
        _login(client, user)
        resp = client.post(
            "/account",
            data={"export_filename_template": "   "},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(User, user.id).export_filename_template is None


def test_account_post_rejects_path_separators():
    with app.app_context():
        user = _mk_user(template="{{title}}")
        client = app.test_client()
        _login(client, user)
        for bad in ("../{{title}}", "a/b", "a\\b"):
            resp = client.post(
                "/account",
                data={"export_filename_template": bad},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert resp.status_code == 400
        db.session.expire_all()
        # Original template untouched.
        assert db.session.get(User, user.id).export_filename_template == "{{title}}"


if __name__ == "__main__":
    # Legacy standalone invocation support (mirrors other test files).
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
