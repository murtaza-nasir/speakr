#!/usr/bin/env python3
"""
Comprehensive test suite for Speaker API v1 endpoints.

Covers:
  - PUT  /recordings/<id>/speakers/assign   (17 tests)
  - POST /recordings/<id>/speakers/identify (10 tests)
  - PUT  /settings/auto-summarization        (5 tests, but one test is split: 4 unique)
  - Regression for GET /speakers and GET /recordings/<id>/speakers (2 tests)

Pattern follows tests/test_api_v1_upload.py — standalone, no pytest fixtures.
"""

import json
import secrets
import sys
import os
from unittest.mock import patch, MagicMock

# Add parent directory so we can import the app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, APIToken, Recording, Speaker
from src.utils.token_auth import hash_token

# ---------------------------------------------------------------------------
# Test data constants
# ---------------------------------------------------------------------------

SAMPLE_TRANSCRIPTION_JSON = json.dumps([
    {"speaker": "SPEAKER_00", "sentence": "Hi, I'm Alice."},
    {"speaker": "SPEAKER_01", "sentence": "Hello Alice, I'm Bob."},
    {"speaker": "SPEAKER_00", "sentence": "Nice to meet you, Bob."},
])

SAMPLE_TRANSCRIPTION_TEXT = (
    "[SPEAKER_00]: Hi, I'm Alice.\n"
    "[SPEAKER_01]: Hello Alice, I'm Bob.\n"
    "[SPEAKER_00]: Nice to meet you, Bob."
)

SAMPLE_EMBEDDINGS = json.dumps({
    "SPEAKER_00": [0.1] * 256,
    "SPEAKER_01": [0.2] * 256,
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_test_user(suffix=""):
    """Get or create a test user. Returns (user, created_bool)."""
    username = f"speaker_test_user{suffix}"
    user = User.query.filter_by(username=username).first()
    created = False
    if not user:
        user = User(
            username=username,
            email=f"{username}@local.test",
            name="Test User" if not suffix else None,
        )
        db.session.add(user)
        db.session.commit()
        created = True
    return user, created


def _create_api_token(user):
    """Create a fresh API token. Returns (token_record, plaintext)."""
    plaintext = f"test-token-{secrets.token_urlsafe(16)}"
    token = APIToken(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        name="test-api-token",
    )
    db.session.add(token)
    db.session.commit()
    return token, plaintext


def _create_test_recording(user, transcription=None, speaker_embeddings=None, status="COMPLETED"):
    """Create a Recording owned by *user*."""
    rec = Recording(
        user_id=user.id,
        title="Test Recording",
        status=status,
        transcription=transcription,
        speaker_embeddings=speaker_embeddings,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def _create_test_speaker(user, name="Alice"):
    """Create a Speaker owned by *user*."""
    speaker = Speaker(name=name, user_id=user.id)
    db.session.add(speaker)
    db.session.commit()
    return speaker


def _cleanup(*objects):
    """Delete DB objects in reverse order, committing once."""
    for obj in reversed(objects):
        try:
            db.session.delete(obj)
        except Exception:
            db.session.rollback()
            try:
                merged = db.session.merge(obj)
                db.session.delete(merged)
            except Exception:
                pass
    db.session.commit()


# =========================================================================
# Group 1: PUT /recordings/<id>/speakers/assign  (17 tests)
# =========================================================================


def test_assign_no_auth():
    """No token -> 302 redirect (Flask-Login)."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              json={"speaker_map": {}})
            assert resp.status_code in (302, 401), f"Expected 302/401, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec)
            if cu:
                _cleanup(user)


def test_assign_recording_not_found():
    """Nonexistent recording ID -> 404."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        client = app.test_client()
        try:
            resp = client.put("/api/v1/recordings/999999/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {}})
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"
            return True
        finally:
            _cleanup(token_rec)
            if cu:
                _cleanup(user)


def test_assign_wrong_user_recording():
    """Other user's recording -> 403."""
    with app.app_context():
        owner, co = _get_or_create_test_user("_owner")
        other, cu = _get_or_create_test_user("_other")
        token_rec, token = _create_api_token(other)
        rec = _create_test_recording(owner, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": "Alice"}})
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(other)
            if co:
                _cleanup(owner)


def test_assign_missing_speaker_map():
    """Body {} -> 400 'speaker_map is required'."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            body = resp.get_json()
            assert "speaker_map" in body.get("error", "").lower(), f"Unexpected error: {body}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_invalid_speaker_map_type():
    """speaker_map: 'string' -> 400."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": "not a dict"})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_string_value_json_transcript():
    """Happy path: string names update JSON segments + participants."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("success") is True
            # Verify participants
            participants = body["recording"]["participants"]
            assert "Alice" in participants and "Bob" in participants
            # Verify transcription was updated
            db.session.refresh(rec)
            segments = json.loads(rec.transcription)
            assert segments[0]["speaker"] == "Alice"
            assert segments[1]["speaker"] == "Bob"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_integer_value_speaker_id():
    """Happy path: Speaker model IDs resolved to names."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        speaker = _create_test_speaker(user, "Alice Speaker")
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": speaker.id}})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            db.session.refresh(rec)
            segments = json.loads(rec.transcription)
            assert segments[0]["speaker"] == "Alice Speaker"
            return True
        finally:
            _cleanup(rec, speaker, token_rec)
            if cu:
                _cleanup(user)


def test_assign_object_value_with_name():
    """Happy path: {name, isMe} object format."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {
                                  "SPEAKER_00": {"name": "Alice", "isMe": False},
                              }})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            db.session.refresh(rec)
            segments = json.loads(rec.transcription)
            assert segments[0]["speaker"] == "Alice"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_is_me_flag_with_user_name():
    """isMe: true resolves to user.name."""
    with app.app_context():
        user, cu = _get_or_create_test_user()  # user.name == "Test User"
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {
                                  "SPEAKER_00": {"name": "", "isMe": True},
                              }})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            db.session.refresh(rec)
            segments = json.loads(rec.transcription)
            assert segments[0]["speaker"] == "Test User", f"Got {segments[0]['speaker']}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_is_me_flag_without_user_name():
    """isMe: true falls back to 'Me' when user.name is None."""
    with app.app_context():
        user, cu = _get_or_create_test_user("_noname")
        # Ensure user.name is None
        user.name = None
        db.session.commit()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {
                                  "SPEAKER_00": {"name": "", "isMe": True},
                              }})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            db.session.refresh(rec)
            segments = json.loads(rec.transcription)
            assert segments[0]["speaker"] == "Me", f"Got {segments[0]['speaker']}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_plain_text_transcript():
    """Replaces [SPEAKER_XX] in plain text format."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_TEXT)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            db.session.refresh(rec)
            assert "[Alice]" in rec.transcription
            assert "[Bob]" in rec.transcription
            assert "[SPEAKER_00]" not in rec.transcription
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_speaker_xx_filtered_from_participants():
    """Unresolved SPEAKER_XX labels excluded from participants."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            # Only assign one speaker — SPEAKER_01 stays unresolved
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": "Alice"}})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            participants = body["recording"]["participants"]
            assert "SPEAKER_01" not in participants, f"SPEAKER_01 should be filtered: {participants}"
            assert "Alice" in participants
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_invalid_speaker_id_not_found():
    """Integer ID 999999 -> 404."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": 999999}})
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_wrong_user_speaker_id():
    """Speaker owned by other user -> 403."""
    with app.app_context():
        owner, co = _get_or_create_test_user("_spk_owner")
        other, cu = _get_or_create_test_user("_spk_other")
        token_rec, token = _create_api_token(other)
        rec = _create_test_recording(other, transcription=SAMPLE_TRANSCRIPTION_JSON)
        speaker = _create_test_speaker(owner, "Owner Speaker")
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": speaker.id}})
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, speaker, token_rec)
            if cu:
                _cleanup(other)
            if co:
                _cleanup(owner)


def test_assign_invalid_value_type():
    """Array value -> 400 'Invalid value type'."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                              headers={"X-API-Token": token},
                              json={"speaker_map": {"SPEAKER_00": [1, 2, 3]}})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            body = resp.get_json()
            assert "invalid value type" in body.get("error", "").lower(), f"Unexpected: {body}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_regenerate_summary():
    """regenerate_summary: true -> job_queue.enqueue called, summary_queued: true."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            mock_jq = MagicMock()
            mock_jq.enqueue = MagicMock(return_value="job-123")
            with patch("src.services.job_queue.job_queue", mock_jq):
                resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                                  headers={"X-API-Token": token},
                                  json={
                                      "speaker_map": {"SPEAKER_00": "Alice"},
                                      "regenerate_summary": True,
                                  })
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("summary_queued") is True
            mock_jq.enqueue.assert_called_once()
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_assign_embeddings_updated():
    """With speaker_embeddings -> update_speaker_embedding called, counts returned."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(
            user,
            transcription=SAMPLE_TRANSCRIPTION_JSON,
            speaker_embeddings=SAMPLE_EMBEDDINGS,
        )
        speaker = _create_test_speaker(user, "Alice")
        client = app.test_client()
        try:
            mock_update = MagicMock()
            mock_snippets = MagicMock(return_value=2)
            with patch("src.services.speaker_embedding_matcher.update_speaker_embedding", mock_update), \
                 patch("src.services.speaker_snippets.create_speaker_snippets", mock_snippets):
                resp = client.put(f"/api/v1/recordings/{rec.id}/speakers/assign",
                                  headers={"X-API-Token": token},
                                  json={"speaker_map": {"SPEAKER_00": "Alice"}})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("embeddings_updated") >= 1, f"embeddings_updated: {body}"
            mock_update.assert_called()
            return True
        finally:
            _cleanup(rec, speaker, token_rec)
            if cu:
                _cleanup(user)


# =========================================================================
# Group 2: POST /recordings/<id>/speakers/identify  (10 tests)
# =========================================================================


def test_identify_no_auth():
    """No token -> 302."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify")
            assert resp.status_code in (302, 401), f"Expected 302/401, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec)
            if cu:
                _cleanup(user)


def test_identify_recording_not_found():
    """Nonexistent ID -> 404."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        client = app.test_client()
        try:
            resp = client.post("/api/v1/recordings/999999/speakers/identify",
                               headers={"X-API-Token": token})
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"
            return True
        finally:
            _cleanup(token_rec)
            if cu:
                _cleanup(user)


def test_identify_wrong_user_recording():
    """Other user's recording -> 403."""
    with app.app_context():
        owner, co = _get_or_create_test_user("_id_owner")
        other, cu = _get_or_create_test_user("_id_other")
        token_rec, token = _create_api_token(other)
        rec = _create_test_recording(owner, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                               headers={"X-API-Token": token})
            assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(other)
            if co:
                _cleanup(owner)


def test_identify_no_transcription():
    """No transcription -> 400."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=None)
        client = app.test_client()
        try:
            resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                               headers={"X-API-Token": token})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_identify_non_json_transcription():
    """Plain text -> 400."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_TEXT)
        client = app.test_client()
        try:
            resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                               headers={"X-API-Token": token})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_identify_json_but_not_list():
    """Dict JSON -> 400."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=json.dumps({"key": "value"}))
        client = app.test_client()
        try:
            resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                               headers={"X-API-Token": token})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_identify_happy_path():
    """Mock LLM returns names -> 200 with speaker_map."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            # Build a mock LLM completion response
            mock_completion = MagicMock()
            mock_completion.choices = [MagicMock()]
            mock_completion.choices[0].message.content = json.dumps({
                "SPEAKER_00": "Alice",
                "SPEAKER_01": "Bob",
            })

            with patch("src.services.llm.call_llm_completion", return_value=mock_completion), \
                 patch("src.models.system.SystemSetting.get_setting", return_value=30000):
                resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                                   headers={"X-API-Token": token})

            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("success") is True
            sm = body.get("speaker_map", {})
            assert sm.get("SPEAKER_00") == "Alice"
            assert sm.get("SPEAKER_01") == "Bob"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_identify_post_processing_unknown_values():
    """'Unknown'/'N/A' cleared to ''."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            mock_completion = MagicMock()
            mock_completion.choices = [MagicMock()]
            mock_completion.choices[0].message.content = json.dumps({
                "SPEAKER_00": "Unknown",
                "SPEAKER_01": "N/A",
            })

            with patch("src.services.llm.call_llm_completion", return_value=mock_completion), \
                 patch("src.models.system.SystemSetting.get_setting", return_value=30000):
                resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                                   headers={"X-API-Token": token})

            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            sm = body.get("speaker_map", {})
            assert sm.get("SPEAKER_00") == "", f"Expected empty, got {sm.get('SPEAKER_00')}"
            assert sm.get("SPEAKER_01") == "", f"Expected empty, got {sm.get('SPEAKER_01')}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_identify_no_speakers_in_transcript():
    """Segments without speaker field -> 400."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        no_speakers = json.dumps([{"sentence": "Hello"}, {"sentence": "World"}])
        rec = _create_test_recording(user, transcription=no_speakers)
        client = app.test_client()
        try:
            resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                               headers={"X-API-Token": token})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_identify_llm_error():
    """LLM raises exception -> 500."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            with patch("src.services.llm.call_llm_completion", side_effect=RuntimeError("LLM down")), \
                 patch("src.models.system.SystemSetting.get_setting", return_value=30000):
                resp = client.post(f"/api/v1/recordings/{rec.id}/speakers/identify",
                                   headers={"X-API-Token": token})
            assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


# =========================================================================
# Group 3: PUT /settings/auto-summarization  (5 tests)
# =========================================================================


def test_auto_summarization_no_auth():
    """No token -> 302."""
    with app.app_context():
        client = app.test_client()
        resp = client.put("/api/v1/settings/auto-summarization",
                          json={"enabled": True})
        assert resp.status_code in (302, 401), f"Expected 302/401, got {resp.status_code}"
        return True


def test_auto_summarization_missing_enabled():
    """Body {} -> 400."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        client = app.test_client()
        try:
            resp = client.put("/api/v1/settings/auto-summarization",
                              headers={"X-API-Token": token},
                              json={})
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            body = resp.get_json()
            assert "enabled" in body.get("error", "").lower(), f"Unexpected: {body}"
            return True
        finally:
            _cleanup(token_rec)
            if cu:
                _cleanup(user)


def test_auto_summarization_invalid_json():
    """Non-JSON body -> 400 (or 415 if Flask rejects content-type)."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        client = app.test_client()
        try:
            resp = client.put("/api/v1/settings/auto-summarization",
                              headers={"X-API-Token": token,
                                       "Content-Type": "application/json"},
                              data="not valid json")
            assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
            return True
        finally:
            _cleanup(token_rec)
            if cu:
                _cleanup(user)


def test_auto_summarization_enable():
    """enabled: true -> updates user, returns true."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        user.auto_summarization = False
        db.session.commit()
        token_rec, token = _create_api_token(user)
        client = app.test_client()
        try:
            resp = client.put("/api/v1/settings/auto-summarization",
                              headers={"X-API-Token": token},
                              json={"enabled": True})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("auto_summarization") is True
            db.session.refresh(user)
            assert user.auto_summarization is True
            return True
        finally:
            _cleanup(token_rec)
            if cu:
                _cleanup(user)


def test_auto_summarization_disable():
    """enabled: false -> updates user, returns false."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        user.auto_summarization = True
        db.session.commit()
        token_rec, token = _create_api_token(user)
        client = app.test_client()
        try:
            resp = client.put("/api/v1/settings/auto-summarization",
                              headers={"X-API-Token": token},
                              json={"enabled": False})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            assert body.get("auto_summarization") is False
            db.session.refresh(user)
            assert user.auto_summarization is False
            return True
        finally:
            _cleanup(token_rec)
            if cu:
                _cleanup(user)


# =========================================================================
# Group 4: Regression tests  (2 tests)
# =========================================================================


def test_regression_get_speakers_list():
    """GET /speakers still returns user's speakers."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        speaker = _create_test_speaker(user, "Regression Speaker")
        client = app.test_client()
        try:
            resp = client.get("/api/v1/speakers",
                              headers={"X-API-Token": token})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            names = [s["name"] for s in body.get("speakers", [])]
            assert "Regression Speaker" in names, f"Speaker not found: {names}"
            return True
        finally:
            _cleanup(speaker, token_rec)
            if cu:
                _cleanup(user)


def test_regression_get_recording_speakers():
    """GET /recordings/<id>/speakers still returns transcript speakers."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = _create_test_recording(user, transcription=SAMPLE_TRANSCRIPTION_JSON)
        client = app.test_client()
        try:
            with patch("src.services.speaker_embedding_matcher.find_matching_speakers", return_value={}):
                resp = client.get(f"/api/v1/recordings/{rec.id}/speakers",
                                  headers={"X-API-Token": token})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.get_json()
            labels = [s["label"] for s in body.get("speakers", [])]
            assert "SPEAKER_00" in labels and "SPEAKER_01" in labels, f"Labels: {labels}"
            return True
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


# =========================================================================
# Runner
# =========================================================================

ALL_TESTS = [
    # Group 1: assign
    test_assign_no_auth,
    test_assign_recording_not_found,
    test_assign_wrong_user_recording,
    test_assign_missing_speaker_map,
    test_assign_invalid_speaker_map_type,
    test_assign_string_value_json_transcript,
    test_assign_integer_value_speaker_id,
    test_assign_object_value_with_name,
    test_assign_is_me_flag_with_user_name,
    test_assign_is_me_flag_without_user_name,
    test_assign_plain_text_transcript,
    test_assign_speaker_xx_filtered_from_participants,
    test_assign_invalid_speaker_id_not_found,
    test_assign_wrong_user_speaker_id,
    test_assign_invalid_value_type,
    test_assign_regenerate_summary,
    test_assign_embeddings_updated,
    # Group 2: identify
    test_identify_no_auth,
    test_identify_recording_not_found,
    test_identify_wrong_user_recording,
    test_identify_no_transcription,
    test_identify_non_json_transcription,
    test_identify_json_but_not_list,
    test_identify_happy_path,
    test_identify_post_processing_unknown_values,
    test_identify_no_speakers_in_transcript,
    test_identify_llm_error,
    # Group 3: auto-summarization
    test_auto_summarization_no_auth,
    test_auto_summarization_missing_enabled,
    test_auto_summarization_invalid_json,
    test_auto_summarization_enable,
    test_auto_summarization_disable,
    # Group 4: regression
    test_regression_get_speakers_list,
    test_regression_get_recording_speakers,
]


def main():
    print(f"Running {len(ALL_TESTS)} Speaker API tests...\n")
    passed = 0
    failed = 0
    errors = []

    for test_fn in ALL_TESTS:
        name = test_fn.__name__
        try:
            result = test_fn()
            if result:
                print(f"  PASS  {name}")
                passed += 1
            else:
                print(f"  FAIL  {name} (returned False)")
                failed += 1
                errors.append(name)
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            failed += 1
            errors.append(name)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(ALL_TESTS)}")
    if errors:
        print("Failed tests:")
        for e in errors:
            print(f"  - {e}")
    print('=' * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
