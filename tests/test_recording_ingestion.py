"""Duplicate detection steps of upload ingestion (src/services/recording_ingestion.py).

lookup_duplicate hashes the staged upload and finds an earlier recording of
it; handle_duplicate_replay answers a retried upload with that recording.

SHARED-DB: the user, recordings and jobs are removed afterwards.
"""

import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User
from src.services import recording_ingestion
from src.services.recording_ingestion import handle_duplicate_replay, lookup_duplicate

PAYLOAD = b"duplicate-detection-bytes"
PAYLOAD_HASH = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def owner():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"ing1_{s}", email=f"ing1_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        ProcessingJob.query.filter_by(user_id=uid).delete()
        Recording.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


@pytest.fixture
def staged(tmp_path):
    path = tmp_path / "staged_upload.wav"
    path.write_bytes(PAYLOAD)
    return str(path)


def _recording(uid, **fields):
    values = dict(user_id=uid, title="Earlier upload", status="COMPLETED",
                  original_filename="a.wav", file_hash=PAYLOAD_HASH, processing_source="upload")
    values.update(fields)
    rec = Recording(**values)
    db.session.add(rec)
    db.session.commit()
    return rec


def _lookup(uid, staged, form=None, reuse_duplicate=False, **kwargs):
    args = dict(processing_source="upload", original_filename="a.wav")
    args.update(kwargs)
    return lookup_duplicate(owner=db.session.get(User, uid), filepath=staged, form=form or {},
                            reuse_duplicate=reuse_duplicate, **args)


# --- lookup_duplicate ---------------------------------------------------------

def test_lookup_leaves_everything_none_when_hashing_fails(owner, staged):
    with app.app_context():
        _recording(owner)
        with patch("src.utils.file_hash.compute_file_sha256", side_effect=OSError("unreadable")), \
                patch.object(app.logger, "warning") as warning:
            result = _lookup(owner, staged)
        assert result == (None, None, None)
        warning.assert_called_once_with("Could not compute file hash or check duplicates: unreadable")


def test_lookup_keeps_the_hash_when_the_query_fails(owner, staged):
    with app.app_context():
        with patch.object(Recording, "query") as query, patch.object(app.logger, "warning") as warning:
            query.filter_by.side_effect = RuntimeError("db gone")
            result = _lookup(owner, staged)
        assert result == (PAYLOAD_HASH, None, None)
        warning.assert_called_once_with("Could not compute file hash or check duplicates: db gone")


def test_lookup_without_a_match(owner, staged):
    with app.app_context():
        _recording(owner, file_hash="0" * 64)
        result = _lookup(owner, staged)
        assert result == (PAYLOAD_HASH, None, None)


def test_lookup_matches_on_hash_and_warns(owner, staged):
    with app.app_context():
        rec = _recording(owner, created_at=datetime(2026, 1, 2, 3, 4, 5))
        result = _lookup(owner, staged, processing_source="api_v1", original_filename="other.wav")
        assert result.file_hash == PAYLOAD_HASH
        assert result.existing.id == rec.id
        assert result.duplicate_warning == {
            "existing_recording_id": rec.id,
            "existing_title": "Earlier upload",
            "existing_created_at": "2026-01-02T03:04:05",
        }


def test_lookup_ignores_another_users_recording(owner, staged):
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        other = User(username=f"ing1o_{s}", email=f"ing1o_{s}@local.test", password="x")
        db.session.add(other)
        db.session.commit()
        try:
            _recording(other.id)
            assert _lookup(owner, staged) == (PAYLOAD_HASH, None, None)
        finally:
            Recording.query.filter_by(user_id=other.id).delete()
            db.session.delete(other)
            db.session.commit()


def test_reuse_duplicate_matches_source_filename_notes_and_meeting_date(owner, staged):
    with app.app_context():
        rec = _recording(owner, processing_source="api_v1", notes="n1",
                         meeting_date=datetime(2026, 3, 4, 5, 6, 7),
                         created_at=datetime.utcnow() - timedelta(days=30))
        form = {"notes": "n1", "meeting_date": "2026-03-04T05:06:07Z"}
        found = _lookup(owner, staged, form, reuse_duplicate=True, processing_source="api_v1")
        assert found.existing.id == rec.id
        assert found.file_hash == PAYLOAD_HASH
        # A replay gets no duplicate warning.
        assert found.duplicate_warning is None

        misses = [
            ({**form, "meeting_date": "2026-03-04T05:06:08Z"}, {}),
            ({**form, "notes": "n2"}, {}),
            (form, {"original_filename": "b.wav"}),
            (form, {"processing_source": "upload"}),
        ]
        for miss_form, overrides in misses:
            args = {"processing_source": "api_v1", **overrides}
            assert _lookup(owner, staged, miss_form, reuse_duplicate=True, **args) == (PAYLOAD_HASH, None, None)


def test_reuse_duplicate_without_a_meeting_date_matches_only_the_last_day(owner, staged):
    with app.app_context():
        old = _recording(owner, created_at=datetime.utcnow() - timedelta(days=2))
        assert _lookup(owner, staged, {}, reuse_duplicate=True) == (PAYLOAD_HASH, None, None)
        # An unparsable meeting date counts as none.
        assert _lookup(owner, staged, {"meeting_date": "not a date"},
                       reuse_duplicate=True) == (PAYLOAD_HASH, None, None)
        old.created_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        assert _lookup(owner, staged, {"meeting_date": "not a date"}, reuse_duplicate=True).existing.id == old.id


# --- handle_duplicate_replay ----------------------------------------------------

def _replay(uid, rec, staged, success_status=200):
    return handle_duplicate_replay(rec, owner=db.session.get(User, uid), filepath=staged,
                                   file_hash=PAYLOAD_HASH, success_status=success_status)


def test_replay_answers_with_the_existing_recording(owner, staged):
    with app.app_context():
        rec = _recording(owner, status="COMPLETED")
        with patch.object(recording_ingestion.job_queue, "enqueue") as enqueue, \
                patch.object(app.logger, "info") as info:
            response, status = _replay(owner, rec, staged, success_status=201)
        assert status == 201
        body = response.get_json()
        assert body["id"] == rec.id and body["idempotent_replay"] is True
        assert not os.path.exists(staged)
        enqueue.assert_not_called()
        info.assert_any_call(f"Idempotent upload replay for user {owner}: "
                             f"hash={PAYLOAD_HASH[:12]}... matches recording {rec.id}")


@pytest.mark.parametrize("status", ["PENDING", "QUEUED"])
def test_replay_queues_a_lost_transcription(owner, staged, status):
    with app.app_context():
        rec = _recording(owner, status=status)
        with patch.object(recording_ingestion, "resolve_transcription_params",
                          return_value={"language": "en"}) as resolve, \
                patch.object(recording_ingestion.job_queue, "enqueue") as enqueue:
            response, code = _replay(owner, rec, staged)
        assert code == 200 and response.get_json()["idempotent_replay"] is True
        resolve.assert_called_once_with(rec)
        enqueue.assert_called_once_with(user_id=owner, recording_id=rec.id, job_type="transcribe",
                                        params={"language": "en"}, is_new_upload=True)


@pytest.mark.parametrize("job_type", ["transcribe", "convert"])
def test_replay_leaves_a_queued_job_alone(owner, staged, job_type):
    with app.app_context():
        rec = _recording(owner, status="QUEUED")
        db.session.add(ProcessingJob(user_id=owner, recording_id=rec.id, job_type=job_type))
        db.session.commit()
        with patch.object(recording_ingestion.job_queue, "enqueue") as enqueue:
            response, code = _replay(owner, rec, staged)
        assert code == 200 and response.get_json()["id"] == rec.id
        enqueue.assert_not_called()


def test_replay_removes_staging_before_enqueueing(owner, staged):
    with app.app_context():
        rec = _recording(owner, status="PENDING")
        seen = []
        with patch.object(recording_ingestion.job_queue, "enqueue",
                          side_effect=lambda **kw: seen.append(os.path.exists(staged))):
            _replay(owner, rec, staged)
        assert seen == [False]
