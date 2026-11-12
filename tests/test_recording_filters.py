"""Direct tests for the recording-status predicates.

The three "needs" filters used to be SQL built inside the recordings
blueprint, reachable only through ``GET /api/recordings`` with a logged-in
user. They now live in ``src.services.recording_filters`` and are exercised
here as the module's public interface: build rows, run
``select(Recording.id).where(<predicate>)``, compare the id set. A failure
therefore points at the predicate, not at the route.

The list-route tests in ``tests/test_cov_recordings_read.py`` remain the
regression net for the HTTP surface; this file does not repeat them.

Real SQLite, never a mock: the LIKE-escape behaviour and ``trim()``
semantics are exactly what these predicates encode, and a mock would assert
them away.

SHARED-DB: every row is created under a fresh user and every query is scoped
to that user, because the test database is shared across modules.
"""

import json
import os
import sys
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import select, update

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from src.services.recording_filters import (
    needs_speakers_condition,
    needs_summary_condition,
    needs_transcription_condition,
    transcription_error_condition,
)
from src.utils.error_formatting import TRANSCRIPTION_ERROR_PREFIXES, is_transcription_error


@contextmanager
def _db():
    with app.app_context():
        yield


def _user():
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"filters_{suffix}"[:20],
        email=f"filters_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.flush()
    return user


def _recording(user, *, transcription="valid transcript", summary="finished summary",
               status="COMPLETED", title="rec"):
    recording = Recording(
        user_id=user.id,
        title=title,
        status=status,
        transcription=transcription,
        summary=summary,
        audio_path="local://filters/audio.mp3",
        original_filename="audio.mp3",
    )
    db.session.add(recording)
    db.session.flush()
    return recording


def _set_status(recording_id, status):
    """Set the status through an UPDATE so a NULL survives the column default."""
    db.session.execute(
        update(Recording).where(Recording.id == recording_id).values(status=status)
    )
    db.session.flush()


def _selected(condition, user):
    stmt = select(Recording.id).where(condition, Recording.user_id == user.id)
    return {row[0] for row in db.session.execute(stmt)}


def _speaker_transcript(*labels):
    return json.dumps(
        [{"speaker": label, "sentence": f"Line from {label}."} for label in labels]
    )


def test_needs_transcription_includes_blank_and_error_transcripts():
    with _db():
        user = _user()
        missing = _recording(user, transcription=None, summary=None)
        empty = _recording(user, transcription="", summary=None)
        failed = [
            _recording(user, transcription=f"{prefix} boom", summary=None, title=f"failed-{i}")
            for i, prefix in enumerate(TRANSCRIPTION_ERROR_PREFIXES)
        ]
        ready = _recording(user, transcription="valid transcript")

        selected = _selected(needs_transcription_condition(), user)
        assert selected == {missing.id, empty.id, *(rec.id for rec in failed)}
        assert ready.id not in selected


@pytest.mark.parametrize("status", ["QUEUED", "PENDING", "PROCESSING", "SUMMARIZING"])
def test_needs_transcription_excludes_every_active_status(status):
    with _db():
        user = _user()
        active = _recording(user, transcription=None, summary=None, status=status)

        selected = _selected(needs_transcription_condition(), user)
        assert selected == set()
        assert active.id not in selected


def test_needs_summary_includes_missing_and_marker_summaries():
    with _db():
        user = _user()
        missing = _recording(user, summary=None)
        empty = _recording(user, summary="")
        markers = [
            _recording(user, summary=f"{marker} quota exceeded", title=f"marker-{i}")
            for i, marker in enumerate(
                ("[Summary generation failed:", "[Summary skipped:", "[Summary not generated]")
            )
        ]
        finished = _recording(user, summary="finished summary")

        selected = _selected(needs_summary_condition(), user)
        assert selected == {missing.id, empty.id, *(rec.id for rec in markers)}
        assert finished.id not in selected


def test_needs_summary_excludes_rows_without_a_valid_transcript():
    with _db():
        user = _user()
        no_transcript = _recording(user, transcription=None, summary=None)
        empty_transcript = _recording(user, transcription="   ", summary=None)
        failed_transcript = _recording(
            user, transcription="Transcription failed: provider unavailable", summary=None
        )
        ready = _recording(user, transcription="valid transcript", summary=None)

        selected = _selected(needs_summary_condition(), user)
        assert selected == {ready.id}
        for recording in (no_transcript, empty_transcript, failed_transcript):
            assert recording.id not in selected


def test_needs_speakers_requires_the_label_token_and_an_idle_status():
    with _db():
        user = _user()
        unresolved = _recording(user, transcription=_speaker_transcript("SPEAKER_00"))
        named = _recording(
            user,
            transcription=_speaker_transcript("Alice", "Bob"),
            summary="finished summary",
        )
        no_transcript = _recording(user, transcription=None)
        active = _recording(
            user,
            transcription=_speaker_transcript("SPEAKER_00"),
            status="PROCESSING",
            title="active",
        )

        selected = _selected(needs_speakers_condition(), user)
        assert selected == {unresolved.id}
        for recording in (named, no_transcript, active):
            assert recording.id not in selected


def test_whitespace_only_transcription_is_missing():
    with _db():
        user = _user()
        whitespace = _recording(user, transcription="   ", summary=None)

        assert whitespace.id in _selected(needs_transcription_condition(), user)
        assert whitespace.id not in _selected(needs_summary_condition(), user)


def test_null_status_counts_as_idle():
    with _db():
        user = _user()
        needs_transcription = _recording(user, transcription=None, summary=None)
        needs_summary = _recording(user, transcription="valid transcript", summary=None)
        needs_speakers = _recording(user, transcription=_speaker_transcript("SPEAKER_01"))
        for recording in (needs_transcription, needs_summary, needs_speakers):
            _set_status(recording.id, None)

        assert needs_transcription.id in _selected(needs_transcription_condition(), user)
        assert needs_summary.id in _selected(needs_summary_condition(), user)
        assert needs_speakers.id in _selected(needs_speakers_condition(), user)


def test_prefix_matching_is_literal():
    with _db():
        user = _user()
        # A LIKE would read the '_' in ERROR_JSON: as a wildcard and match
        # this text; the escaped pattern must not.
        wrong_separator = _recording(user, transcription='ERRORXJSON:{"t": "x"}')
        # ']' is not the ':' the skipped marker requires.
        wrong_marker = _recording(
            user, transcription="valid transcript", summary="[Summary skipped] quota"
        )

        assert _selected(transcription_error_condition(), user) == set()
        assert wrong_separator.id not in _selected(needs_transcription_condition(), user)
        assert wrong_marker.id not in _selected(needs_summary_condition(), user)


def test_sql_predicate_and_string_check_agree():
    texts = [
        None,
        "",
        "   ",
        "valid transcript",
        "ERRORXJSON:{\"t\": \"x\"}",
        *[f"{prefix} boom" for prefix in TRANSCRIPTION_ERROR_PREFIXES],
    ]
    with _db():
        user = _user()
        expected = {}
        for index, text in enumerate(texts):
            recording = _recording(user, transcription=text, title=f"agreement-{index}")
            expected[recording.id] = is_transcription_error(text)

        selected = _selected(transcription_error_condition(), user)
        assert selected == {recording_id for recording_id, is_error in expected.items() if is_error}
