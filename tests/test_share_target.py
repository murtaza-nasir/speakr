"""Regression tests for the PWA Web Share Target endpoint (issue #285).

The endpoint is what Android Chrome / iOS Safari 16.4+ POST to when the
user picks Speakr from the native share sheet. We verify:

- the manifest declares /share-target as the action
- a missing file redirects with share_target_error=missing_file
- a valid POST creates a Recording row owned by the current user
- the endpoint is CSRF-exempt (the share sheet has no CSRF token)
- the endpoint is login-required (unauthenticated visits bounce to login)
"""

import io
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording


def _setup_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_manifest_share_target_action():
    """The PWA manifest must point share_target.action at /share-target."""
    manifest_path = Path(__file__).parent.parent / "static" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["share_target"]["action"] == "/share-target"
    assert manifest["share_target"]["method"] == "POST"
    assert manifest["share_target"]["enctype"] == "multipart/form-data"
    files = manifest["share_target"]["params"]["files"]
    assert any(f["name"] == "shared_audio" for f in files)


def test_share_target_missing_file_redirects_with_error_flag():
    with app.app_context():
        user = _setup_user("share_missing")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/share-target", data={}, follow_redirects=False)
        assert resp.status_code == 302
        assert "share_target_error=missing_file" in resp.headers.get("Location", "")
        db.session.delete(user)
        db.session.commit()


def test_share_target_creates_recording_and_enqueues_job():
    with app.app_context():
        user = _setup_user("share_ok")
        client = app.test_client()
        _login(client, user)

        captured = {}

        def fake_enqueue(*args, **kwargs):
            captured.update(kwargs)
            return 999

        with patch("src.services.job_queue.job_queue.enqueue", side_effect=fake_enqueue), \
             patch("src.api.recordings.os.path.getsize", return_value=12345):
            resp = client.post(
                "/share-target",
                data={
                    "title": "Shared from Recorder app",
                    "text": "Meeting notes",
                    "shared_audio": (io.BytesIO(b"fake-audio-bytes"), "shared.webm"),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )
        assert resp.status_code == 302, resp.data
        location = resp.headers.get("Location", "")
        assert "share_target=ok" in location, location
        assert "recording_id=" in location, location

        recordings = Recording.query.filter_by(user_id=user.id).all()
        assert len(recordings) == 1
        rec = recordings[0]
        assert rec.title == "Shared from Recorder app"
        assert rec.notes and "Meeting notes" in rec.notes
        assert rec.status == "PENDING"
        assert captured.get("user_id") == user.id
        assert captured.get("recording_id") == rec.id
        assert captured.get("job_type") == "transcribe", (
            f"share-target must use the same job_type as upload_file (transcribe), "
            f"got {captured.get('job_type')!r}"
        )
        assert captured.get("is_new_upload") is True, "share-target uploads should be flagged as new uploads"
        # Clean up the saved file artefact if present
        if rec.audio_path and os.path.exists(rec.audio_path):
            try:
                os.remove(rec.audio_path)
            except OSError:
                pass
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()


def test_share_target_without_title_gets_ai_titleable_placeholder():
    """Regression: a file shared WITHOUT an explicit title must get the same
    placeholder ('Recording - <filename>') a normal upload gets, so the AI
    title task recognises it and generates a title. Previously the share
    target used the filename STEM, which the title task treated as a
    user-chosen title and skipped, leaving shared files untitled."""
    from src.utils.titles import is_placeholder_title

    with app.app_context():
        user = _setup_user("share_notitle")
        client = app.test_client()
        _login(client, user)

        with patch("src.services.job_queue.job_queue.enqueue", return_value=1), \
             patch("src.api.recordings.os.path.getsize", return_value=12345):
            resp = client.post(
                "/share-target",
                data={
                    # No "title" field — the common case from a share sheet.
                    "shared_audio": (io.BytesIO(b"fake-audio-bytes"), "voice_memo_001.webm"),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )
        assert resp.status_code == 302, resp.data

        rec = Recording.query.filter_by(user_id=user.id).one()
        # The title must be the recognised placeholder, NOT the filename stem.
        assert rec.title == "Recording - voice_memo_001.webm", rec.title
        assert is_placeholder_title(rec.title, rec.original_filename) is True

        if rec.audio_path and os.path.exists(rec.audio_path):
            try:
                os.remove(rec.audio_path)
            except OSError:
                pass
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()


def test_share_target_requires_login():
    """Unauthenticated POSTs hit @login_required and redirect to /login."""
    with app.app_context():
        client = app.test_client()
        resp = client.post(
            "/share-target",
            data={"shared_audio": (io.BytesIO(b"x"), "x.webm")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        loc = resp.headers.get("Location", "")
        assert "/login" in loc or "next=" in loc, loc


def test_share_target_is_csrf_exempt():
    """The share sheet has no CSRF token; the route must be exempted."""
    from src.app import csrf
    # Flask-WTF stores exempt names on the protect instance
    exempt_set = getattr(csrf, "_exempt_views", set())
    # Match by qualified function name
    assert any("share_target" in name for name in exempt_set), \
        f"share_target view should be CSRF-exempt; current exempts: {exempt_set}"


def teardown_module(module):
    with app.app_context():
        for u in User.query.filter(User.username.like("share_%")).all():
            db.session.delete(u)
        db.session.commit()


if __name__ == "__main__":
    test_manifest_share_target_action()
    test_share_target_missing_file_redirects_with_error_flag()
    test_share_target_creates_recording_and_enqueues_job()
    test_share_target_requires_login()
    test_share_target_is_csrf_exempt()
    print("All share-target tests passed.")
