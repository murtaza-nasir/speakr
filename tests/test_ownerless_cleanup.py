"""Daily cleanup of recordings without an owner (#572).

Before #572 an admin's user delete left the user's recordings in the
database with user_id NULL. The cleanup marks such a row on one run and
deletes it on a later one, never touches an owned row, skips anything with
work in flight and never deletes a file it found only on disk.
"""

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, RecordingSession, User
from src.models.processing_job import ProcessingJob
from src.models.upload_join import UploadJoinPart
from src.services import ownerless_cleanup
from src.services.ownerless_cleanup import OWNERLESS_GRACE, sweep_ownerless_recordings

T0 = datetime(2026, 11, 12, 2, 0, 0)


def _storage():
    storage = MagicMock()
    storage.exists.return_value = True
    storage.settings = SimpleNamespace(local_root=None, key_prefix='recordings')
    return storage


def _user():
    suffix = uuid.uuid4().hex[:8]
    u = User(username=f"own_{suffix}", email=f"own_{suffix}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _rec(user=None, **kw):
    rec = Recording(user_id=user.id if user else None, title="t", status="COMPLETED",
                    audio_path=f"local://recordings/2026/11/{uuid.uuid4().hex}.webm",
                    original_filename="x.webm", transcription="hello", **kw)
    db.session.add(rec)
    db.session.commit()
    return rec.id


def _get(rid):
    db.session.expire_all()
    return db.session.get(Recording, rid)


@pytest.fixture
def ctx():
    with app.app_context():
        # Ownerless rows other tests left behind would change the counts.
        storage = _storage()
        from src.services.recording_deletion import delete_recording_completely
        for (rid,) in db.session.query(Recording.id).filter(Recording.user_id.is_(None)).all():
            rec = db.session.get(Recording, rid)
            ProcessingJob.query.filter_by(recording_id=rid).delete()
            delete_recording_completely(rec, storage=storage)
        yield
        db.session.rollback()


def test_owned_recordings_are_never_touched(ctx):
    owner = _user()
    rid = _rec(owner)
    storage = _storage()
    for hours in (0, 21, 48):
        sweep_ownerless_recordings(now=T0 + timedelta(hours=hours), storage=storage)
    rec = _get(rid)
    assert rec is not None and rec.user_id == owner.id
    assert rec.ownerless_since is None
    storage.delete.assert_not_called()


def test_first_run_marks_only(ctx):
    rid = _rec()
    storage = _storage()
    stats = sweep_ownerless_recordings(now=T0, storage=storage)
    rec = _get(rid)
    assert rec is not None
    assert rec.ownerless_since == T0
    assert stats['candidates'] == 1 and stats['marked'] == 1 and stats['deleted'] == 0
    storage.delete.assert_not_called()


def test_second_run_inside_window_does_nothing(ctx):
    rid = _rec()
    storage = _storage()
    sweep_ownerless_recordings(now=T0, storage=storage)
    assert OWNERLESS_GRACE >= timedelta(hours=19)
    sweep_ownerless_recordings(now=T0 + timedelta(hours=1), storage=storage)
    stats = sweep_ownerless_recordings(now=T0 + OWNERLESS_GRACE - timedelta(minutes=1), storage=storage)
    rec = _get(rid)
    assert rec is not None and rec.ownerless_since == T0  # the mark is not moved forward
    assert stats['marked'] == 0 and stats['deleted'] == 0 and stats['waiting'] == 1
    storage.delete.assert_not_called()


def test_run_after_window_deletes_completely(ctx):
    rid = _rec()
    path = _get(rid).audio_path
    storage = _storage()
    sweep_ownerless_recordings(now=T0, storage=storage)
    stats = sweep_ownerless_recordings(now=T0 + timedelta(hours=24), storage=storage)
    assert _get(rid) is None
    assert stats['deleted'] == 1
    storage.delete.assert_called_once_with(path, missing_ok=True)


def test_in_flight_job_is_skipped_and_loses_its_mark(ctx):
    rid = _rec()
    storage = _storage()
    sweep_ownerless_recordings(now=T0, storage=storage)
    worker = _user()
    db.session.add(ProcessingJob(user_id=worker.id, recording_id=rid, job_type="summarize", status="processing"))
    db.session.commit()

    stats = sweep_ownerless_recordings(now=T0 + timedelta(hours=24), storage=storage)
    rec = _get(rid)
    assert rec is not None and rec.ownerless_since is None
    assert stats['skipped_in_flight'] == 1 and stats['deleted'] == 0
    storage.delete.assert_not_called()

    # Once the job is done the wait starts again from the next run.
    ProcessingJob.query.filter_by(recording_id=rid).update({'status': 'completed'})
    db.session.commit()
    sweep_ownerless_recordings(now=T0 + timedelta(hours=48), storage=storage)
    assert _get(rid).ownerless_since == T0 + timedelta(hours=48)
    sweep_ownerless_recordings(now=T0 + timedelta(hours=60), storage=storage)
    assert _get(rid) is not None
    sweep_ownerless_recordings(now=T0 + timedelta(hours=72), storage=storage)
    assert _get(rid) is None


def test_queued_job_active_session_and_unmerged_join_are_skipped(ctx):
    queued = _rec()
    in_session = _rec()
    joining = _rec()
    worker = _user()
    db.session.add(ProcessingJob(user_id=worker.id, recording_id=queued, job_type="transcribe", status="queued"))
    db.session.add(RecordingSession(user_id=worker.id, status="finalizing", finalized_recording_id=in_session))
    db.session.add(UploadJoinPart(user_id=worker.id, group_id=uuid.uuid4().hex, part_index=0, part_count=2,
                                  audio_path="local://recordings/2026/11/join-1/part.m4a", recording_id=joining))
    db.session.commit()
    storage = _storage()
    for hours in (0, 24, 48):
        stats = sweep_ownerless_recordings(now=T0 + timedelta(hours=hours), storage=storage)
        assert stats['skipped_in_flight'] == 3
    for rid in (queued, in_session, joining):
        assert _get(rid) is not None and _get(rid).ownerless_since is None
    storage.delete.assert_not_called()


def test_regained_owner_clears_mark(ctx):
    rid = _rec()
    storage = _storage()
    sweep_ownerless_recordings(now=T0, storage=storage)
    owner = _user()
    Recording.query.filter_by(id=rid).update({'user_id': owner.id})
    db.session.commit()

    stats = sweep_ownerless_recordings(now=T0 + timedelta(hours=24), storage=storage)
    rec = _get(rid)
    assert rec is not None and rec.user_id == owner.id and rec.ownerless_since is None
    assert stats['unmarked'] == 1 and stats['deleted'] == 0
    storage.delete.assert_not_called()


def test_idempotent(ctx):
    rid = _rec()
    owner = _user()
    owned = _rec(owner)
    storage = _storage()
    first = sweep_ownerless_recordings(now=T0, storage=storage)
    again = sweep_ownerless_recordings(now=T0, storage=storage)
    assert first['marked'] == 1 and again['marked'] == 0
    assert _get(rid).ownerless_since == T0

    later = T0 + timedelta(hours=24)
    deleted = sweep_ownerless_recordings(now=later, storage=storage)
    after = sweep_ownerless_recordings(now=later, storage=storage)
    assert deleted['deleted'] == 1 and after['deleted'] == 0 and after['candidates'] == 0
    assert _get(rid) is None and _get(owned) is not None
    assert storage.delete.call_count == 1


def test_media_that_cannot_be_deleted_keeps_the_row(ctx):
    rid = _rec()
    storage = _storage()
    sweep_ownerless_recordings(now=T0, storage=storage)
    storage.delete.side_effect = OSError("disk")
    stats = sweep_ownerless_recordings(now=T0 + timedelta(hours=24), storage=storage)
    assert stats['errors'] == 1 and stats['deleted'] == 0
    assert _get(rid) is not None


def test_unreferenced_files_are_counted_never_deleted(ctx):
    with tempfile.TemporaryDirectory() as root:
        rec_dir = os.path.join(root, 'recordings', '2026', '11')
        os.makedirs(rec_dir)
        os.makedirs(os.path.join(root, '_sessions', 'abc'))
        kept = os.path.join(rec_dir, 'kept.webm')
        stray = os.path.join(rec_dir, 'stray.webm')
        legacy_stray = os.path.join(root, 'old_upload.mp3')
        session_chunk = os.path.join(root, '_sessions', 'abc', 'chunk1.webm')
        for p in (kept, stray, legacy_stray, session_chunk):
            open(p, 'wb').close()
        owner = _user()
        _rec(owner)  # a row whose file does not exist: not counted
        Recording.query.filter(Recording.user_id == owner.id).update(
            {'audio_path': 'local://recordings/2026/11/kept.webm'})
        db.session.commit()

        storage = MagicMock()
        storage.settings = SimpleNamespace(local_root=root, key_prefix='recordings')
        storage.resolve_local_filesystem_path.side_effect = (
            lambda loc: os.path.join(root, loc.split('://', 1)[1]) if loc.startswith('local://') else
            (_ for _ in ()).throw(ValueError('not local')))

        stats = sweep_ownerless_recordings(now=T0, storage=storage)
        assert stats['unreferenced_files'] == 2  # stray.webm and old_upload.mp3
        for p in (kept, stray, legacy_stray, session_chunk):
            assert os.path.exists(p)
        storage.delete.assert_not_called()


def test_daily_run_cleans_up_when_auto_deletion_is_disabled(monkeypatch):
    from src.config import startup
    calls = []
    monkeypatch.setattr(startup, 'scheduled_deletion_due', lambda: False)
    monkeypatch.setattr(ownerless_cleanup, 'run_ownerless_cleanup', lambda: calls.append('cleanup'))
    startup.run_daily_maintenance(app)
    assert calls == ['cleanup']


def test_daily_run_cleans_up_when_auto_deletion_fails(monkeypatch):
    from src.config import startup
    from src.services import retention
    calls = []

    def boom():
        raise RuntimeError("retention broke")
    monkeypatch.setattr(startup, 'scheduled_deletion_due', lambda: True)
    monkeypatch.setattr(retention, 'process_auto_deletion', boom)
    monkeypatch.setattr(ownerless_cleanup, 'run_ownerless_cleanup', lambda: calls.append('cleanup'))
    startup.run_daily_maintenance(app)
    assert calls == ['cleanup']
