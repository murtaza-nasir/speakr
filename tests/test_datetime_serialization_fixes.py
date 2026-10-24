#!/usr/bin/env python3
"""
Regression tests for the datetime bugs reported in #319 and #320.

Covers:
  - Speaker.to_dict() must serialize datetimes as ISO strings, not raw
    datetime objects (raw objects get jsonify'd as RFC-1123 strings, which
    the frontend's parseInstant mangles into "Invalid Date") (#319)
  - src.utils.dates.to_utc_naive normalizes datetimes to the naive-UTC
    storage convention shared by meeting_date and created_at (#320)
  - PATCH /api/v1/recordings/<id> with a zone-aware meeting_date stores a
    naive UTC value (#320)
  - Speaker snippet lookup finds recordings beyond the 10 most recent when
    the speaker's name matches (#319, "samples shown but no preview")

Pattern follows tests/test_api_v1_speakers.py — standalone, no pytest fixtures.
"""

import json
import secrets
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, APIToken, Recording, Speaker
from src.utils.dates import to_utc_naive
from src.utils.token_auth import hash_token


def _get_or_create_test_user(suffix=""):
    username = f"dtfix_test_user{suffix}"
    user = User.query.filter_by(username=username).first()
    created = False
    if not user:
        user = User(username=username, email=f"{username}@local.test")
        db.session.add(user)
        db.session.commit()
        created = True
    return user, created


def _create_api_token(user):
    plaintext = f"test-token-{secrets.token_urlsafe(16)}"
    token = APIToken(user_id=user.id, token_hash=hash_token(plaintext), name="dtfix-token")
    db.session.add(token)
    db.session.commit()
    return token, plaintext


def _cleanup(*objects):
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


# ---------------------------------------------------------------------------
# Speaker.to_dict serialization (#319)
# ---------------------------------------------------------------------------


def test_speaker_to_dict_serializes_datetimes_as_iso_strings():
    with app.app_context():
        user, cu = _get_or_create_test_user()
        speaker = Speaker(name="ISO Check", user_id=user.id,
                          created_at=datetime(2026, 2, 23, 5, 11, 59),
                          last_used=datetime(2026, 6, 10, 0, 19, 12))
        db.session.add(speaker)
        db.session.commit()
        try:
            data = speaker.to_dict()
            assert data["created_at"] == "2026-02-23T05:11:59"
            assert data["last_used"] == "2026-06-10T00:19:12"
            # Round-trippable by the frontend parseInstant convention
            datetime.fromisoformat(data["created_at"])
            datetime.fromisoformat(data["last_used"])
        finally:
            _cleanup(speaker)
            if cu:
                _cleanup(user)


def test_speaker_to_dict_handles_null_dates():
    with app.app_context():
        user, cu = _get_or_create_test_user()
        speaker = Speaker(name="Null Dates", user_id=user.id)
        db.session.add(speaker)
        db.session.commit()
        # Column defaults fill these on insert; null them out afterwards
        speaker.created_at = None
        speaker.last_used = None
        db.session.commit()
        try:
            data = speaker.to_dict()
            assert data["created_at"] is None
            assert data["last_used"] is None
        finally:
            _cleanup(speaker)
            if cu:
                _cleanup(user)


def test_speakers_endpoint_returns_iso_dates():
    """GET /speakers (the page's own endpoint) must not emit RFC-1123 dates."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        speaker = Speaker(name="Endpoint ISO", user_id=user.id,
                          created_at=datetime(2026, 1, 2, 3, 4, 5),
                          last_used=datetime(2026, 1, 2, 3, 4, 5))
        db.session.add(speaker)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        try:
            resp = client.get("/speakers")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            speakers = resp.get_json()
            ours = [s for s in speakers if s.get("name") == "Endpoint ISO"]
            assert ours, "created speaker missing from /speakers response"
            for field in ("created_at", "last_used"):
                value = ours[0][field]
                assert "GMT" not in value, f"{field} serialized as RFC-1123: {value}"
                datetime.fromisoformat(value)
        finally:
            _cleanup(speaker)
            if cu:
                _cleanup(user)


# ---------------------------------------------------------------------------
# src.utils.dates helpers (#320)
# ---------------------------------------------------------------------------


def test_to_utc_naive_none_and_naive_passthrough():
    assert to_utc_naive(None) is None
    naive = datetime(2026, 7, 2, 11, 0, 0)
    assert to_utc_naive(naive) is naive


def test_to_utc_naive_converts_aware_to_utc_and_strips_tz():
    # UTC input: tzinfo stripped, clock unchanged
    aware_utc = datetime(2026, 7, 2, 16, 0, 0, tzinfo=timezone.utc)
    assert to_utc_naive(aware_utc) == datetime(2026, 7, 2, 16, 0, 0)
    # Offset input: converted to UTC
    aware_offset = datetime(2026, 7, 2, 18, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    result = to_utc_naive(aware_offset)
    assert result.tzinfo is None
    assert result == datetime(2026, 7, 2, 16, 0, 0)


# ---------------------------------------------------------------------------
# PATCH meeting_date stores naive UTC (#320)
# ---------------------------------------------------------------------------


def test_patch_meeting_date_offset_converted_to_utc():
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = Recording(user_id=user.id, title="DT Patch", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        client = app.test_client()
        try:
            resp = client.patch(f"/api/v1/recordings/{rec.id}",
                                headers={"X-API-Token": token},
                                json={"meeting_date": "2026-07-02T18:00:00+02:00"})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.data}"
            db.session.refresh(rec)
            assert rec.meeting_date is not None
            assert rec.meeting_date.tzinfo is None, "meeting_date stored zone-aware"
            assert rec.meeting_date == datetime(2026, 7, 2, 16, 0, 0)
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


def test_patch_meeting_date_naive_stored_as_utc_literal():
    """A naive UTC string (what the picker sends via toISOString) is stored as-is."""
    with app.app_context():
        user, cu = _get_or_create_test_user()
        token_rec, token = _create_api_token(user)
        rec = Recording(user_id=user.id, title="DT Naive", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        client = app.test_client()
        try:
            resp = client.patch(f"/api/v1/recordings/{rec.id}",
                                headers={"X-API-Token": token},
                                json={"meeting_date": "2026-07-02T11:30:00"})
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.data}"
            db.session.refresh(rec)
            assert rec.meeting_date == datetime(2026, 7, 2, 11, 30, 0)
        finally:
            _cleanup(rec, token_rec)
            if cu:
                _cleanup(user)


# ---------------------------------------------------------------------------
# Snippet lookup beyond the 10 most recent recordings (#319)
# ---------------------------------------------------------------------------


def test_snippets_found_when_speaker_only_in_older_recordings():
    from src.services.speaker_snippets import get_speaker_snippets

    with app.app_context():
        user, cu = _get_or_create_test_user("_snip")
        speaker = Speaker(name="Old Timer", user_id=user.id)
        db.session.add(speaker)
        db.session.commit()

        created = []
        base = datetime(2026, 1, 1, 12, 0, 0)
        # Oldest recording is the ONLY one featuring the speaker
        match_transcript = json.dumps([
            {"speaker": "Old Timer", "sentence": "This is a long enough sentence to snippet.",
             "start_time": 5.0, "end_time": 12.0},
        ])
        rec_match = Recording(user_id=user.id, title="Old Match", status="COMPLETED",
                              transcription=match_transcript, created_at=base)
        db.session.add(rec_match)
        created.append(rec_match)

        filler_transcript = json.dumps([
            {"speaker": "Somebody Else", "sentence": "Nothing relevant in this one at all.",
             "start_time": 1.0, "end_time": 6.0},
        ])
        for i in range(12):
            rec = Recording(user_id=user.id, title=f"Filler {i}", status="COMPLETED",
                            transcription=filler_transcript,
                            created_at=base + timedelta(days=i + 1))
            db.session.add(rec)
            created.append(rec)
        db.session.commit()

        try:
            snippets = get_speaker_snippets(speaker.id, limit=3)
            assert snippets, "expected a snippet from the older matching recording"
            assert snippets[0]["recording_id"] == rec_match.id
            assert snippets[0]["start_time"] == 5.0
        finally:
            _cleanup(*created, speaker)
            if cu:
                _cleanup(user)


def test_snippet_lookup_matches_underscore_in_name_literally():
    # #485: '_' in the speaker name is not a wildcard, so recordings of a
    # similarly named speaker cannot fill the ten candidate slots
    from src.services.speaker_snippets import get_speaker_snippets

    with app.app_context():
        user, cu = _get_or_create_test_user("_lit")
        speaker = Speaker(name="Old_Timer", user_id=user.id)
        db.session.add(speaker)
        db.session.commit()

        base = datetime(2026, 1, 1, 12, 0, 0)
        rec_match = Recording(user_id=user.id, title="Old Match", status="COMPLETED", created_at=base,
                              transcription=json.dumps([
                                  {"speaker": "Old_Timer", "sentence": "This is a long enough sentence.",
                                   "start_time": 5.0, "end_time": 12.0}]))
        created = [rec_match]
        lookalike = json.dumps([{"speaker": "OldXTimer", "sentence": "A different speaker entirely.",
                                 "start_time": 1.0, "end_time": 6.0}])
        for i in range(12):
            created.append(Recording(user_id=user.id, title=f"Lookalike {i}", status="COMPLETED",
                                     transcription=lookalike, created_at=base + timedelta(days=i + 1)))
        db.session.add_all(created)
        db.session.commit()

        try:
            snippets = get_speaker_snippets(speaker.id, limit=3)
            assert [s["recording_id"] for s in snippets] == [rec_match.id]
        finally:
            _cleanup(*created, speaker)
            if cu:
                _cleanup(user)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
