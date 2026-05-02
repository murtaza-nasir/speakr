#!/usr/bin/env python3
"""
Regression test for issue #265: Reprocess transcription does not apply
tag/folder/user default hotwords or initial_prompt.

Asserts that the reprocess endpoint, given an empty request body, walks the
same precedence chain that upload_file() uses:
    user input > tag defaults > folder defaults > user defaults

The test patches job_queue.enqueue() to capture the params dict that would be
sent to the worker.

Run with: docker exec speakr-dev python /app/tests/test_reprocess_hotwords.py
"""

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app import app, db
from src.models import User, Recording
from src.models.organization import Tag, Folder, RecordingTag

# CSRF tokens are scoped to interactive sessions. Disable for the test client.
app.config["WTF_CSRF_ENABLED"] = False


PASSED = 0
FAILED = 0


def run(name, func):
    global PASSED, FAILED
    try:
        func()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name}: EXCEPTION - {e}")
        FAILED += 1


def setup_recording(*, owner_hotwords=None, owner_prompt=None,
                    tag_hotwords=None, tag_prompt=None,
                    folder_hotwords=None, folder_prompt=None):
    """Create a user, recording, optional tag, optional folder, and any default values."""
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"reprocess_test_{suffix}",
        email=f"reprocess_test_{suffix}@test.local",
        password="x",  # column is just a hash string; auth isn't exercised in this test
    )
    user.transcription_hotwords = owner_hotwords
    user.transcription_initial_prompt = owner_prompt
    db.session.add(user)
    db.session.flush()

    folder = None
    if folder_hotwords or folder_prompt:
        folder = Folder(
            name=f"f_{user.id}",
            user_id=user.id,
            default_hotwords=folder_hotwords,
            default_initial_prompt=folder_prompt,
        )
        db.session.add(folder)
        db.session.flush()

    rec = Recording(
        audio_path="/tmp/dummy.wav",
        original_filename="dummy.wav",
        title="test",
        status="COMPLETED",
        user_id=user.id,
        folder_id=folder.id if folder else None,
    )
    db.session.add(rec)
    db.session.flush()

    if tag_hotwords or tag_prompt:
        tag = Tag(
            name=f"t_{user.id}",
            user_id=user.id,
            default_hotwords=tag_hotwords,
            default_initial_prompt=tag_prompt,
        )
        db.session.add(tag)
        db.session.flush()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=1))
        db.session.flush()

    db.session.commit()
    return user, rec


def call_reprocess(client, recording_id, body):
    """POST to the reprocess endpoint and capture the job_params."""
    from src.services import job_queue as jq_module

    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured.update(kwargs)
        return 999

    original = jq_module.job_queue.enqueue
    jq_module.job_queue.enqueue = fake_enqueue
    try:
        with patch("src.api.recordings.os.path.exists", return_value=True):
            resp = client.post(
                f"/recording/{recording_id}/reprocess_transcription",
                data=json.dumps(body),
                content_type="application/json",
            )
    finally:
        jq_module.job_queue.enqueue = original
    return resp, captured.get("params", {})


def login_client(client, user):
    """Log in via the test client by writing the session cookie directly."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_user_input_wins():
    """Explicit hotwords/initial_prompt in the request body take priority over all defaults."""
    with app.app_context():
        user, rec = setup_recording(
            owner_hotwords="user_hot", owner_prompt="user_prompt",
            tag_hotwords="tag_hot", tag_prompt="tag_prompt",
        )
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {
            "hotwords": "explicit_hot",
            "initial_prompt": "explicit_prompt",
        })
        assert resp.status_code in (200, 202), f"unexpected status {resp.status_code}: {resp.data!r}"
        assert params.get("hotwords") == "explicit_hot", f"got {params!r}"
        assert params.get("initial_prompt") == "explicit_prompt", f"got {params!r}"


def test_tag_defaults_apply_when_no_user_input():
    """Tag default_hotwords / default_initial_prompt apply when request body is empty."""
    with app.app_context():
        user, rec = setup_recording(
            tag_hotwords="tag_hot", tag_prompt="tag_prompt",
            folder_hotwords="folder_hot", folder_prompt="folder_prompt",
            owner_hotwords="user_hot", owner_prompt="user_prompt",
        )
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {})
        assert resp.status_code in (200, 202), f"unexpected status {resp.status_code}: {resp.data!r}"
        # Tag wins over folder and over user.  # noqa
        assert params.get("hotwords") == "tag_hot", f"got {params!r}"
        assert params.get("initial_prompt") == "tag_prompt", f"got {params!r}"


def test_folder_defaults_apply_when_no_tag():
    """Folder defaults apply when there's no tag override and no user input."""
    with app.app_context():
        user, rec = setup_recording(
            folder_hotwords="folder_hot", folder_prompt="folder_prompt",
            owner_hotwords="user_hot", owner_prompt="user_prompt",
        )
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {})
        assert resp.status_code in (200, 202)
        # Folder wins over user, no tag in play.
        assert params.get("hotwords") == "folder_hot", f"got {params!r}"
        assert params.get("initial_prompt") == "folder_prompt", f"got {params!r}"


def test_user_defaults_apply_as_last_resort():
    """User account defaults apply when nothing else is set."""
    with app.app_context():
        user, rec = setup_recording(
            owner_hotwords="user_hot", owner_prompt="user_prompt",
        )
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {})
        assert resp.status_code in (200, 202)
        assert params.get("hotwords") == "user_hot", f"got {params!r}"
        assert params.get("initial_prompt") == "user_prompt", f"got {params!r}"


def test_empty_when_nothing_configured():
    """No user input + no tag/folder/user defaults → params have None for both."""
    with app.app_context():
        user, rec = setup_recording()
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {})
        assert resp.status_code in (200, 202)
        assert params.get("hotwords") is None, f"got {params!r}"
        assert params.get("initial_prompt") is None, f"got {params!r}"


def cleanup_test_users():
    """Remove every user created by this test file.

    Tests run against the live dev database, so without an explicit teardown
    the synthesised users would accumulate forever and pollute the admin
    user-management screens.
    """
    with app.app_context():
        for u in User.query.filter(User.username.like('reprocess_test_%')).all():
            db.session.delete(u)
        db.session.commit()


def main():
    print("=== Issue #265: reprocess hotwords/initial_prompt precedence ===\n")
    try:
        run("user input wins over all defaults", test_user_input_wins)
        run("tag defaults apply when no user input", test_tag_defaults_apply_when_no_user_input)
        run("folder defaults apply when no tag", test_folder_defaults_apply_when_no_tag)
        run("user account defaults apply as last resort", test_user_defaults_apply_as_last_resort)
        run("nothing configured → both None", test_empty_when_nothing_configured)
    finally:
        cleanup_test_users()

    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
