"""One-time rewrite of transcripts stored with \\uXXXX escapes (#622).

SHARED-DB: the users and recordings made here are removed afterwards, and
the setting of a completed run is removed before and after each test.
"""

import json
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, SystemSetting, User
from src.services import transcript_unicode_rewrite as rewrite
from src.services.transcript_unicode_rewrite import (
    DONE_KEY, rewritten_text, run_transcript_unicode_rewrite)

SEGMENTS = [{"speaker": "SPEAKER_00", "sentence": "Esta es una grabación en español", "start_time": 0.0},
            {"speaker": "Zoë", "sentence": "大家好,这是一段演示录音。", "start_time": 1.5}]
ESCAPED = json.dumps(SEGMENTS)
RAW = json.dumps(SEGMENTS, ensure_ascii=False)
PLAIN = "Plain text with a literal \\u00f1 that is not JSON"
MALFORMED = '[{"sentence": "espa\\u00f1ol"'
OLD = datetime(2024, 1, 2, 3, 4, 5)


def _clear_done():
    SystemSetting.query.filter_by(key=DONE_KEY).delete()
    db.session.commit()


@pytest.fixture
def owner():
    with app.app_context():
        _clear_done()
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"tur_{suffix}", email=f"tur_{suffix}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        yield user
        db.session.rollback()
        Recording.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()
        _clear_done()


def _recording(user, transcription):
    rec = Recording(user_id=user.id, title="t", status="COMPLETED", transcription=transcription)
    db.session.add(rec)
    db.session.commit()
    # A fixed past updated_at, set beside the ORM so that the test can tell
    # whether the rewrite moved it.
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE recording SET updated_at = :t WHERE id = :id"),
                     {"t": OLD, "id": rec.id})
    return rec.id


def _row(rec_id):
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT transcription, updated_at FROM recording WHERE id = :id"),
                            {"id": rec_id}).one()


def _as_datetime(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def test_escaped_rows_are_rewritten_and_the_rest_are_left_alone(owner):
    with app.app_context():
        escaped = _recording(owner, ESCAPED)
        raw = _recording(owner, RAW)
        plain = _recording(owner, PLAIN)
        malformed = _recording(owner, MALFORMED)
        empty = _recording(owner, None)

        counts = run_transcript_unicode_rewrite(batch_size=2)

        assert counts["rewritten"] >= 1
        assert _row(escaped)[0] == RAW
        assert json.loads(_row(escaped)[0]) == SEGMENTS
        assert _row(raw)[0] == RAW
        assert _row(plain)[0] == PLAIN
        assert _row(malformed)[0] == MALFORMED
        assert _row(empty)[0] is None
        assert SystemSetting.query.filter_by(key=DONE_KEY).first().value == "true"

        # The raw form is what a plain LIKE now finds.
        found = Recording.query.filter(Recording.user_id == owner.id,
                                       Recording.transcription.like("%español%")).all()
        assert {r.id for r in found} == {escaped, raw}


def test_the_rewrite_moves_no_timestamp(owner):
    """updated_at is moved by the before_flush listener on ORM changes; the
    rewrite is a core UPDATE, so recordings do not look edited."""
    with app.app_context():
        rec_id = _recording(owner, ESCAPED)
        run_transcript_unicode_rewrite()
        stored, updated_at = _row(rec_id)
        assert stored == RAW
        assert _as_datetime(updated_at) == OLD


def test_a_second_run_does_nothing(owner):
    with app.app_context():
        run_transcript_unicode_rewrite()
        later = _recording(owner, ESCAPED)
        assert run_transcript_unicode_rewrite() is None
        assert _row(later)[0] == ESCAPED


def test_an_interrupted_run_resumes_at_the_next_start(owner):
    with app.app_context():
        first = _recording(owner, ESCAPED)
        second = _recording(owner, json.dumps({"segments": SEGMENTS}))
        real = rewrite.rewritten_text
        calls = []

        def fail_on_second(stored):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("stopped")
            return real(stored)

        # One row per batch, so the first row is committed before the stop.
        with patch.object(rewrite, "rewritten_text", fail_on_second), pytest.raises(RuntimeError):
            run_transcript_unicode_rewrite(batch_size=1)
        db.session.rollback()
        assert SystemSetting.query.filter_by(key=DONE_KEY).first() is None

        run_transcript_unicode_rewrite()
        assert _row(first)[0] == RAW
        assert json.loads(_row(second)[0]) == {"segments": SEGMENTS}
        assert "\\u" not in _row(second)[0]
        assert SystemSetting.query.filter_by(key=DONE_KEY).first() is not None


def test_rows_that_must_be_kept():
    # Not an array or object, a lone surrogate (not storable as UTF-8), no
    # escape, an escape of a backslash only.
    assert rewritten_text(json.dumps("espa\u00f1ol")) is None
    assert rewritten_text('[{"sentence": "\\ud800"}]') is None
    assert rewritten_text(RAW) is None
    assert rewritten_text(json.dumps([{"sentence": "C:\\users"}])) is None
    assert rewritten_text(MALFORMED) is None
    assert rewritten_text(PLAIN) is None
    assert rewritten_text(ESCAPED) == RAW


def test_the_log_holds_counts_only(owner):
    with app.app_context():
        owner_title = "SENTINEL title"
        rec_id = _recording(owner, ESCAPED)
        db.session.get(Recording, rec_id).title = owner_title
        db.session.commit()
        with patch.object(rewrite.logger, "info") as info:
            run_transcript_unicode_rewrite()
        logged = " ".join(str(a) for call in info.call_args_list for a in call.args)
        assert info.called
        for secret in (owner_title, "español", "grabaci", "大家"):
            assert secret not in logged


def test_the_startup_thread_is_not_started_when_disabled():
    from src.config import startup
    started = []
    with patch.dict(os.environ, {"DISABLE_TRANSCRIPT_UNICODE_REWRITE": "true"}), \
            patch("threading.Thread.start", lambda self: started.append(self.name)):
        startup.initialize_transcript_unicode_rewrite(app)
    assert started == []

    with patch.dict(os.environ, {"DISABLE_TRANSCRIPT_UNICODE_REWRITE": "false"}), \
            patch("threading.Thread.start", lambda self: started.append(self.name)):
        startup.initialize_transcript_unicode_rewrite(app)
    assert started == ["TranscriptUnicodeRewrite"]
