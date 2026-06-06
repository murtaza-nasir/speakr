"""Regression tests for Recording.error_message persistence.

Three sites in src/tasks/processing.py used to write to a non-existent
``recording.error_msg`` attribute (model defines ``error_message``).
SQLAlchemy declarative doesn't reject unmapped attribute writes, so the
value was set on the Python instance and silently never persisted; the
UI showed FAILED with no detail across every code path that surfaced an
ffmpeg or budget error.

These tests pin the column name across the three call sites by
exercising the actual write-and-commit pattern through the model.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording


def _make_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password='placeholder-bcrypt-hash',
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_recording(user_id):
    recording = Recording(
        user_id=user_id,
        title='err-persist',
        audio_path='/tmp/nonexistent.mp3',
        status='PENDING',
    )
    db.session.add(recording)
    db.session.commit()
    return recording


def test_recording_error_message_persists_when_set():
    """Smoke test for the column itself. If the rename was incomplete
    or someone re-introduces the error_msg typo, the round-trip after
    commit will read back None instead of the assigned text."""
    with app.app_context():
        user = _make_user('err_smoke')
        recording = _make_recording(user.id)
        rec_id = recording.id
        user_id = user.id
        recording.status = 'FAILED'
        recording.error_message = 'budget exceeded for the month'
        db.session.commit()

        # Drop the cached instance and read back via fresh query so we
        # observe what actually landed in the row, not the instance state.
        db.session.expire_all()
        refetched = Recording.query.filter_by(id=rec_id).first()
        assert refetched.status == 'FAILED'
        assert refetched.error_message == 'budget exceeded for the month'

        db.session.delete(refetched)
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()


def test_processing_module_writes_to_error_message_not_error_msg():
    """Source-level guard against re-introducing the typo. Pins that
    no `recording.error_msg =` assignment lives in processing.py."""
    src_path = os.path.join(
        os.path.dirname(__file__), '..', 'src', 'tasks', 'processing.py'
    )
    with open(src_path, 'r', encoding='utf-8') as f:
        source = f.read()
    assert 'recording.error_msg' not in source, (
        'processing.py still has a recording.error_msg write — that '
        'attribute is not on the Recording model, so the assignment '
        'never persists. Use recording.error_message instead.'
    )
    # And ensure at least one error_message write still exists (the
    # rename actually happened).
    assert 'recording.error_message' in source, (
        'processing.py has no recording.error_message writes — the rename '
        'may have over-replaced or removed the assignments entirely.'
    )
