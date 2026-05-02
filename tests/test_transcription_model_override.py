#!/usr/bin/env python3
"""
Tests for issue #266: per-upload, per-tag, per-folder transcription model
selection.

Validates:
1. TRANSCRIPTION_MODEL_OPTIONS is parsed from env vars correctly.
2. Reprocess endpoint resolves the precedence chain
   (user input > tag default > folder default), validates against the
   allowlist, and writes the model into job_params.
3. The TranscriptionRequest dataclass carries `model` through to the
   connector via _effective_model().
4. Per-request model override falls back gracefully when the override is
   blank or unsupported.

Run with: docker exec speakr-dev python /app/tests/test_transcription_model_override.py
"""

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# CSRF tokens are scoped to interactive sessions. Disable for the test client.
from src.app import app, db
app.config["WTF_CSRF_ENABLED"] = False

from src.models import User, Recording
from src.models.organization import Tag, Folder, RecordingTag
from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.openai_whisper import (
    OpenAIWhisperConnector,
)


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


# -----------------------------------------------------------------------------
# Section 1: env-var → TRANSCRIPTION_MODEL_OPTIONS
# -----------------------------------------------------------------------------

def test_models_csv_parses_with_labels():
    """TRANSCRIPTION_MODELS_AVAILABLE + TRANSCRIPTION_MODEL_LABELS pair correctly."""
    # Reload the config module with env vars set.
    import importlib
    os.environ["TRANSCRIPTION_MODELS_AVAILABLE"] = "whisper-1, gpt-4o-transcribe ,vibevoice"
    os.environ["TRANSCRIPTION_MODEL_LABELS"] = "Whisper,GPT-4o,VibeVoice"
    try:
        import src.config.app_config as cfg
        importlib.reload(cfg)
        assert cfg.TRANSCRIPTION_MODELS_AVAILABLE == [
            "whisper-1", "gpt-4o-transcribe", "vibevoice"
        ], cfg.TRANSCRIPTION_MODELS_AVAILABLE
        assert cfg.TRANSCRIPTION_MODEL_OPTIONS == [
            {"value": "whisper-1", "label": "Whisper"},
            {"value": "gpt-4o-transcribe", "label": "GPT-4o"},
            {"value": "vibevoice", "label": "VibeVoice"},
        ], cfg.TRANSCRIPTION_MODEL_OPTIONS
    finally:
        os.environ.pop("TRANSCRIPTION_MODELS_AVAILABLE", None)
        os.environ.pop("TRANSCRIPTION_MODEL_LABELS", None)
        importlib.reload(cfg)


def test_models_csv_unset_means_empty():
    """When unset, TRANSCRIPTION_MODEL_OPTIONS is an empty list (dropdown hidden)."""
    import importlib
    os.environ.pop("TRANSCRIPTION_MODELS_AVAILABLE", None)
    os.environ.pop("TRANSCRIPTION_MODEL_LABELS", None)
    import src.config.app_config as cfg
    importlib.reload(cfg)
    assert cfg.TRANSCRIPTION_MODEL_OPTIONS == [], cfg.TRANSCRIPTION_MODEL_OPTIONS


# -----------------------------------------------------------------------------
# Section 2: reprocess endpoint precedence chain
# -----------------------------------------------------------------------------

def setup_recording(*, user_input=None, tag_model=None, folder_model=None,
                    available_models=None):
    """Build a Recording + Tag + Folder with the requested defaults.

    Returns (user, recording, plain-text-token-for-test-client) where the
    'token' is unused. Caller logs in via test_client session.
    """
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"models_{suffix}",
        email=f"models_{suffix}@test.local",
        password="x",
    )
    db.session.add(user)
    db.session.flush()

    folder = None
    if folder_model:
        folder = Folder(
            name=f"f_{suffix}",
            user_id=user.id,
            default_transcription_model=folder_model,
        )
        db.session.add(folder)
        db.session.flush()

    rec = Recording(
        audio_path="/tmp/dummy.wav",
        original_filename="dummy.wav",
        title="model test",
        status="COMPLETED",
        user_id=user.id,
        folder_id=folder.id if folder else None,
    )
    db.session.add(rec)
    db.session.flush()

    if tag_model:
        tag = Tag(
            name=f"t_{suffix}",
            user_id=user.id,
            default_transcription_model=tag_model,
        )
        db.session.add(tag)
        db.session.flush()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=1))
        db.session.flush()

    db.session.commit()
    return user, rec


def call_reprocess(client, recording_id, body, available_models=None):
    """POST to the reprocess endpoint and capture job_params."""
    from src.services import job_queue as jq_module

    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured.update(kwargs)
        return 999

    original_enqueue = jq_module.job_queue.enqueue
    jq_module.job_queue.enqueue = fake_enqueue
    try:
        # Patch the allowlist used by the endpoint.
        with patch("src.api.recordings.os.path.exists", return_value=True), \
             patch("src.config.app_config.TRANSCRIPTION_MODELS_AVAILABLE",
                   list(available_models) if available_models is not None else []):
            resp = client.post(
                f"/recording/{recording_id}/reprocess_transcription",
                data=json.dumps(body),
                content_type="application/json",
            )
    finally:
        jq_module.job_queue.enqueue = original_enqueue
    return resp, captured.get("params", {})


def login_client(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_user_input_wins_over_defaults():
    with app.app_context():
        user, rec = setup_recording(tag_model="vibevoice", folder_model="whisper-1")
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id,
                                       {"transcription_model": "gpt-4o-transcribe"},
                                       available_models=["gpt-4o-transcribe", "vibevoice", "whisper-1"])
        assert resp.status_code in (200, 202), f"unexpected status {resp.status_code}: {resp.data!r}"
        assert params.get("transcription_model") == "gpt-4o-transcribe", f"got {params!r}"


def test_tag_default_applied_when_no_user_input():
    with app.app_context():
        user, rec = setup_recording(tag_model="vibevoice", folder_model="whisper-1")
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {},
                                       available_models=["whisper-1", "vibevoice"])
        assert resp.status_code in (200, 202), f"unexpected status {resp.status_code}: {resp.data!r}"
        # Tag wins over folder.
        assert params.get("transcription_model") == "vibevoice", f"got {params!r}"


def test_folder_default_applied_when_no_tag():
    with app.app_context():
        user, rec = setup_recording(folder_model="whisper-1")
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {},
                                       available_models=["whisper-1", "vibevoice"])
        assert resp.status_code in (200, 202)
        assert params.get("transcription_model") == "whisper-1", f"got {params!r}"


def test_user_supplied_model_outside_allowlist_dropped():
    """An explicit override that isn't in the allowlist gets stripped."""
    with app.app_context():
        user, rec = setup_recording(tag_model="vibevoice")
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id,
                                       {"transcription_model": "secret-internal-model"},
                                       available_models=["whisper-1", "vibevoice"])
        assert resp.status_code in (200, 202)
        # Falls back to the tag default since user value was rejected.
        # Note: the endpoint validates AFTER applying the precedence chain. The
        # precedence chain runs first, so user input wins; then validation drops
        # the bad value to None. Tag default is not re-applied — so we expect
        # transcription_model to be None here.
        assert params.get("transcription_model") is None, f"got {params!r}"


def test_no_defaults_means_no_model_in_job_params():
    with app.app_context():
        user, rec = setup_recording()
        client = app.test_client()
        login_client(client, user)
        resp, params = call_reprocess(client, rec.id, {})
        assert resp.status_code in (200, 202)
        assert params.get("transcription_model") is None, f"got {params!r}"


# -----------------------------------------------------------------------------
# Section 3: TranscriptionRequest carries `model` to the connector
# -----------------------------------------------------------------------------

def test_transcription_request_has_model_field():
    req = TranscriptionRequest(
        audio_file=None, filename="x.wav", model="custom-model",
    )
    assert req.model == "custom-model"


def test_effective_model_uses_override():
    connector = OpenAIWhisperConnector({"api_key": "x"})
    req = TranscriptionRequest(
        audio_file=None, filename="x.wav", model="whisper-1",
    )
    assert connector._effective_model(req) == "whisper-1"


def test_effective_model_falls_back_to_default():
    connector = OpenAIWhisperConnector({"api_key": "x", "model": "default-model"})
    req = TranscriptionRequest(audio_file=None, filename="x.wav")
    assert connector._effective_model(req) == "default-model"


def test_effective_model_blank_override_falls_back():
    connector = OpenAIWhisperConnector({"api_key": "x", "model": "default-model"})
    req = TranscriptionRequest(audio_file=None, filename="x.wav", model="   ")
    assert connector._effective_model(req) == "default-model"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def cleanup_test_users():
    """Drop every user created by this file so leaked rows do not appear in
    the admin user-management screens or aggregate stats."""
    with app.app_context():
        for u in User.query.filter(User.username.like('models_%')).all():
            db.session.delete(u)
        db.session.commit()


def main():
    print("=== Issue #266: per-upload/tag/folder transcription model ===\n")
    try:
        print("--- env parsing ---")
        run("CSV + labels parses correctly", test_models_csv_parses_with_labels)
        run("unset env var → empty options", test_models_csv_unset_means_empty)
        print("\n--- precedence chain ---")
        run("user input wins over tag/folder", test_user_input_wins_over_defaults)
        run("tag default applies when no user input", test_tag_default_applied_when_no_user_input)
        run("folder default applies when no tag", test_folder_default_applied_when_no_tag)
        run("model outside allowlist dropped", test_user_supplied_model_outside_allowlist_dropped)
        run("nothing configured → None", test_no_defaults_means_no_model_in_job_params)
        print("\n--- TranscriptionRequest plumbing ---")
        run("model field exists", test_transcription_request_has_model_field)
        run("override propagates", test_effective_model_uses_override)
        run("falls back when no override", test_effective_model_falls_back_to_default)
        run("blank override falls back", test_effective_model_blank_override_falls_back)
    finally:
        cleanup_test_users()

    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
