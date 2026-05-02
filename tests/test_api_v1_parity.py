#!/usr/bin/env python3
"""
Smoke test for issue #274: /api/v1/recordings and /api/v1/recordings/{id}
should expose audio_duration, durations, folder, events, and deletion_exempt.

Run with: docker exec speakr-dev python /app/tests/test_api_v1_parity.py
"""

import secrets
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app import app, db
from src.models import User, Recording, APIToken
from src.models.organization import Folder
from src.utils.token_auth import hash_token


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


def setup_user_and_recording():
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"v1parity_{suffix}",
        email=f"v1parity_{suffix}@test.local",
        password="x",
    )
    db.session.add(user)
    db.session.flush()
    folder = Folder(name=f"folder_{suffix}", user_id=user.id)
    db.session.add(folder)
    db.session.flush()

    rec = Recording(
        audio_path="/tmp/dummy.wav",
        original_filename="dummy.wav",
        title="parity test",
        status="COMPLETED",
        user_id=user.id,
        folder_id=folder.id,
        deletion_exempt=True,
        transcription_duration_seconds=42,
        summarization_duration_seconds=7,
    )
    db.session.add(rec)
    db.session.flush()

    plaintext = f"v1parity-{secrets.token_urlsafe(16)}"
    token = APIToken(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        name="v1parity-token",
    )
    db.session.add(token)
    db.session.commit()
    return user, rec, folder, plaintext


REQUIRED_DETAIL_FIELDS = {
    "audio_duration",
    "transcription_duration_seconds",
    "summarization_duration_seconds",
    "folder_id",
    "folder",
    "deletion_exempt",
    "events",
}

REQUIRED_LIST_FIELDS = {
    "audio_duration",
    "transcription_duration_seconds",
    "summarization_duration_seconds",
    "folder_id",
    "folder",
    "deletion_exempt",
    "completed_at",
    "processing_time_seconds",
}


def test_detail_has_all_new_fields():
    with app.app_context():
        user, rec, folder, token = setup_user_and_recording()
        client = app.test_client()
        resp = client.get(
            f"/api/v1/recordings/{rec.id}",
            headers={"X-API-Token": token},
        )
        assert resp.status_code == 200, f"unexpected status {resp.status_code}: {resp.data!r}"
        body = resp.get_json()
        missing = REQUIRED_DETAIL_FIELDS - set(body.keys())
        assert not missing, f"detail endpoint missing fields: {missing}"
        # audio file is /tmp/dummy.wav and doesn't exist → audio_duration is None.
        assert body["audio_duration"] is None
        assert body["transcription_duration_seconds"] == 42
        assert body["summarization_duration_seconds"] == 7
        assert body["folder_id"] == folder.id
        assert body["folder"] == {"id": folder.id, "name": folder.name}
        assert body["deletion_exempt"] is True
        assert body["events"] == []


def test_list_has_all_new_fields():
    with app.app_context():
        user, rec, folder, token = setup_user_and_recording()
        client = app.test_client()
        resp = client.get(
            "/api/v1/recordings",
            headers={"X-API-Token": token},
        )
        assert resp.status_code == 200, f"unexpected status {resp.status_code}: {resp.data!r}"
        body = resp.get_json()
        assert body.get("recordings"), f"no recordings in response: {body!r}"
        # Find our recording in the list (other tests may have added rows).
        ours = next((r for r in body["recordings"] if r["id"] == rec.id), None)
        assert ours, f"new recording {rec.id} missing from list"
        missing = REQUIRED_LIST_FIELDS - set(ours.keys())
        assert not missing, f"list item missing fields: {missing}"


def main():
    print("=== Issue #274: API v1 parity ===\n")
    run("detail endpoint exposes new fields", test_detail_has_all_new_fields)
    run("list endpoint exposes new fields", test_list_has_all_new_fields)
    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
