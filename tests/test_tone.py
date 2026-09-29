"""Voice-tone record: validation, storage through the API, serialization, and migration.

Tone is supplied by an external scorer. These tests pin the properties the feature depends on:
a bad payload never damages a stored record, other users cannot write it, recordings without
tone are serialized exactly as before, lines find their window by time, and the schema
change is a nullable column that is safe to apply twice to an existing database.
"""

import copy
import os
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from src.app import app, db
from src.models import Recording, User
from src.services.tone import ToneError, validate_tone, window_for_time
from src.utils.database import add_column_if_not_exists

app.config["WTF_CSRF_ENABLED"] = False

GOOD = {
    "schema_version": 1,
    "scored_at": "2026-09-29T12:00:00Z",
    "audio_seconds": 60.0,
    "model": {"encoder": "example"},
    "windows": [
        {"start": 0.0, "end": 5.0, "speaker": "speaker_0", "scores": {"Arousal": 1.1, "Valence": 0.4},
         "standout": {"state": "Thankfulness_Gratitude", "label": "thankful", "emoji": "🙏", "score": 3.1, "z": 5.2, "strength": "strong"}},
        {"start": 6.0, "end": 12.0, "speaker": "speaker_1", "scores": {"Arousal": 0.9}, "standout": None},
    ],
    "call": {"windows": 2, "energy": 1.0},
}


def _user(prefix="tone"):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"{prefix}_{suffix}", email=f"{prefix}_{suffix}@local.test", password="x")
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user):
    rec = Recording(audio_path="/tmp/dummy.wav", original_filename="dummy.wav", title="tone test", status="COMPLETED", user_id=user.id)
    db.session.add(rec)
    db.session.commit()
    return rec


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ---------------------------------------------------------------- validation
def test_a_well_formed_record_is_accepted_unchanged():
    assert validate_tone(copy.deepcopy(GOOD)) == GOOD


@pytest.mark.parametrize("mutate,message", [
    (lambda p: [1, 2], "JSON object"),
    (lambda p: {**p, "surprise": 1}, "unknown field"),
    (lambda p: {**p, "schema_version": 2}, "schema_version"),
    (lambda p: {**p, "windows": "no"}, "windows must be a list"),
    (lambda p: {**p, "windows": [{"start": 5, "end": 5, "scores": {}}]}, "invalid time range"),
    (lambda p: {**p, "windows": [{"start": 6, "end": 9, "scores": {}}, {"start": 1, "end": 2, "scores": {}}]}, "time order"),
    (lambda p: {**p, "windows": [{"start": 0, "end": 2, "scores": {"Arousal": float("nan")}}]}, "finite"),
    (lambda p: {**p, "windows": [{"start": 0, "end": 2, "scores": {"Arousal": "high"}}]}, "finite"),
    (lambda p: {**p, "windows": [{"start": 0, "end": 2, "scores": {}, "standout": {"label": "x"}}]}, "label and an emoji"),
    (lambda p: {**p, "windows": [{"start": 0, "end": 2, "scores": {}, "standout": {"label": "x", "emoji": "y" * 20}}]}, "too long"),
    (lambda p: {**p, "call": "summary"}, "call must be an object"),
    (lambda p: {**p, "model": {"blob": "x" * 600000}}, "too large"),
])
def test_bad_payloads_are_rejected_with_a_reason(mutate, message):
    with pytest.raises(ToneError, match=message):
        validate_tone(mutate(copy.deepcopy(GOOD)))


# ---------------------------------------------------------------- lookup by time
def test_a_line_finds_its_window_by_time_even_after_it_is_split():
    whole = window_for_time(GOOD, 3.0)
    assert whole["speaker"] == "speaker_0"
    # a line split at 3 s: both halves still resolve to the same window (the per-line-copy shortcut would lose the second half)
    assert window_for_time(GOOD, 1.0) is whole and window_for_time(GOOD, 4.9) is whole
    assert window_for_time(GOOD, 5.5) is None, "the gap between windows has none"
    assert window_for_time(GOOD, 12.0) is None, "end is exclusive"
    assert window_for_time(None, 3.0) is None and window_for_time({}, 3.0) is None and window_for_time(GOOD, None) is None


# ---------------------------------------------------------------- API
# Each request runs in its own app context (the test client makes one), because Flask-Login
# caches "who is logged in" on the app context: wrapping several requests in one shared context
# would make a second user inherit the first user's identity and hide real permission behavior.

def _make(prefix="tone"):
    """A user and one of their recordings; returns (user_id, recording_id)."""
    with app.app_context():
        user = _user(prefix)
        return user.id, _recording(user).id


def _client(user_id=None):
    client = app.test_client()
    if user_id is not None:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True
    return client


def _stored(recording_id):
    with app.app_context():
        return db.session.get(Recording, recording_id).tone


def test_put_stores_the_record_and_it_appears_in_the_recording():
    user_id, rec_id = _make()
    response = _client(user_id).put(f"/api/v1/recordings/{rec_id}/tone", json=GOOD)
    assert response.status_code == 200 and response.get_json() == {"success": True, "windows": 2}
    with app.app_context():
        assert db.session.get(Recording, rec_id).to_dict()["tone"] == GOOD


def test_a_bad_payload_leaves_the_stored_record_untouched():
    user_id, rec_id = _make()
    client = _client(user_id)
    assert client.put(f"/api/v1/recordings/{rec_id}/tone", json=GOOD).status_code == 200
    response = client.put(f"/api/v1/recordings/{rec_id}/tone", json={**GOOD, "windows": "broken"})
    assert response.status_code == 400 and "windows" in response.get_json()["error"]
    assert _stored(rec_id) == GOOD


def test_a_non_json_body_is_a_400_not_a_crash():
    user_id, rec_id = _make()
    response = _client(user_id).put(f"/api/v1/recordings/{rec_id}/tone", data="not json", content_type="text/plain")
    assert response.status_code == 400


def test_another_user_cannot_write_or_clear_someone_elses_tone():
    owner_id, rec_id = _make("owner")
    stranger_id, _ = _make("stranger")
    assert _client(owner_id).put(f"/api/v1/recordings/{rec_id}/tone", json=GOOD).status_code == 200
    stranger = _client(stranger_id)
    assert stranger.put(f"/api/v1/recordings/{rec_id}/tone", json={**GOOD, "audio_seconds": 1.0}).status_code == 403
    assert stranger.delete(f"/api/v1/recordings/{rec_id}/tone").status_code == 403
    assert _stored(rec_id) == GOOD, "the owner's record survived both attempts"


def test_an_anonymous_caller_is_refused_and_an_unknown_recording_is_404():
    user_id, rec_id = _make()
    assert _client().put(f"/api/v1/recordings/{rec_id}/tone", json=GOOD).status_code in (401, 302)
    assert _stored(rec_id) is None, "and nothing was stored"
    assert _client(user_id).put("/api/v1/recordings/99999999/tone", json=GOOD).status_code == 404


def test_delete_removes_the_record():
    user_id, rec_id = _make()
    client = _client(user_id)
    client.put(f"/api/v1/recordings/{rec_id}/tone", json=GOOD)
    assert client.delete(f"/api/v1/recordings/{rec_id}/tone").status_code == 200
    assert _stored(rec_id) is None


# ---------------------------------------------------------------- recordings without tone are unchanged
def test_a_recording_without_tone_serializes_with_no_tone_key_at_all():
    with app.app_context():
        rec = _recording(_user())
        assert "tone" not in rec.to_dict()


# ---------------------------------------------------------------- migration
def test_the_column_is_added_once_to_an_existing_database_and_existing_rows_are_untouched():
    """Applies the migration helper to a database that predates the column. The tempting shortcut
    (a hand-run ALTER TABLE) is not idempotent and would fail on the second call."""
    with tempfile.TemporaryDirectory() as folder:
        engine = create_engine(f"sqlite:///{os.path.join(folder, 'old.db')}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE recording (id INTEGER PRIMARY KEY, title TEXT, summary TEXT)"))
            conn.execute(text("INSERT INTO recording (id, title, summary) VALUES (1, 'kept', 'kept too')"))
        assert add_column_if_not_exists(engine, "recording", "tone", "JSON") is True
        assert add_column_if_not_exists(engine, "recording", "tone", "JSON") is False
        with engine.connect() as conn:
            assert [r[1] for r in conn.execute(text("PRAGMA table_info(recording)"))] == ["id", "title", "summary", "tone"]
            assert conn.execute(text("SELECT id, title, summary, tone FROM recording")).fetchall() == [(1, "kept", "kept too", None)]


def test_cleared_tone_is_sql_null_not_json_null():
    """A backfill finds recordings without tone with `tone IS NULL`; a stored JSON `null` would hide them."""
    with app.app_context():
        user = _user("nulltone")
        rec = _recording(user)
        rec.tone = copy.deepcopy(GOOD)
        db.session.commit()
        rec.tone = None
        db.session.commit()
        raw = db.session.execute(text("SELECT tone IS NULL FROM recording WHERE id = :i"), {"i": rec.id}).scalar()
        assert raw == 1
