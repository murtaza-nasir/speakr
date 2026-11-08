"""Without an embedding backend, the Inquire chunks are stored without vectors (#563).

The keyword search Inquire uses then reads those chunks, and the index job
completes instead of failing and being retried. With a backend that returns
fewer vectors than chunks, the build still fails and the old chunks are kept.
"""
import json
import uuid
from unittest.mock import patch

import pytest

import src.services.embeddings as emb
from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, TranscriptChunk, User
from src.services.job_queue import job_queue

TRANSCRIPT = json.dumps([
    {"speaker": "Ada", "sentence": "The quarterly budget review is on Friday.", "start_time": 0.0, "end_time": 4.0},
    {"speaker": "Bob", "sentence": "I will bring the vendor figures.", "start_time": 4.5, "end_time": 7.0},
])


@pytest.fixture
def recording():
    job_queue.init_app(app)
    with app.app_context():
        stale = ProcessingJob.query.filter(ProcessingJob.status == 'queued').all()
        for job in stale:
            job.status = 'completed'
        db.session.commit()
        s = uuid.uuid4().hex[:8]
        user = User(username=f"noemb_{s}", email=f"noemb_{s}@local.test")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title='Budget', audio_path='local://noemb.mp3',
                        transcription=TRANSCRIPT, status='COMPLETED')
        db.session.add(rec)
        db.session.commit()
        yield rec
        TranscriptChunk.query.filter_by(recording_id=rec.id).delete()
        ProcessingJob.query.filter_by(recording_id=rec.id).delete()
        db.session.delete(db.session.get(Recording, rec.id))
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


def test_chunks_are_stored_without_embeddings_when_no_backend_exists(recording):
    # With no backend, generate_embeddings returns [] (it logs that it skips).
    with patch.object(emb, 'EMBEDDINGS_AVAILABLE', False), \
         patch.object(emb, 'generate_embeddings', return_value=[]) as generate:
        assert emb.process_recording_chunks(recording.id) is True
    generate.assert_not_called()
    chunks = TranscriptChunk.query.filter_by(recording_id=recording.id).all()
    assert chunks
    assert all(chunk.embedding is None for chunk in chunks)
    assert any('quarterly budget' in chunk.content for chunk in chunks)


def test_keyword_search_finds_the_chunks_without_embeddings(recording):
    with patch.object(emb, 'EMBEDDINGS_AVAILABLE', False), \
         patch.object(emb, 'generate_embeddings', return_value=[]):
        emb.process_recording_chunks(recording.id)
        results = emb.semantic_search_chunks(recording.user_id, 'vendor figures')
    assert results
    assert any('vendor figures' in (r[0].content if isinstance(r, tuple) else r.content) for r in results)


def test_the_index_job_completes_without_retries_when_no_backend_exists(recording):
    with patch.object(emb, 'EMBEDDINGS_AVAILABLE', False), \
         patch.object(emb, 'generate_embeddings', return_value=[]):
        job_id = job_queue.enqueue(recording.user_id, recording.id, 'index_chunks')
        job = job_queue._claim_next_job(['index_chunks'], 'index')
        assert job is not None and job.id == job_id
        job_queue._process_job(job)
    job = db.session.get(ProcessingJob, job_id)
    assert job.status == 'completed'
    assert job.retry_count == 0


def test_a_backend_returning_too_few_vectors_still_fails_and_keeps_old_chunks(recording):
    with patch.object(emb, 'EMBEDDINGS_AVAILABLE', False), \
         patch.object(emb, 'generate_embeddings', return_value=[]):
        emb.process_recording_chunks(recording.id)
    before = [c.content for c in TranscriptChunk.query.filter_by(recording_id=recording.id).order_by(TranscriptChunk.chunk_index)]
    with patch.object(emb, 'EMBEDDINGS_AVAILABLE', True), \
         patch.object(emb, 'generate_embeddings', return_value=[]):
        assert emb.process_recording_chunks(recording.id) is False
    after = [c.content for c in TranscriptChunk.query.filter_by(recording_id=recording.id).order_by(TranscriptChunk.chunk_index)]
    assert after == before
