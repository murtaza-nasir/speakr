"""A recording without an owner gets no Inquire chunks and no error (#683).

transcript_chunk.user_id is required, and the chunks of a recording whose
owner was removed (user_id NULL) failed with an IntegrityError: the index job
was retried and the startup backfill logged it on every start.
"""

from src.app import app
from src.database import db
from src.models import Recording, TranscriptChunk
from src.services.embeddings import process_recording_chunks
from src.services.job_queue import job_queue


def _ownerless_recording():
    rec = Recording(user_id=None, title="Ownerless", status="COMPLETED",
                    transcription="This transcript has more than enough words to make one chunk.")
    db.session.add(rec)
    db.session.commit()
    return rec.id


def test_process_recording_chunks_skips_an_ownerless_recording():
    with app.app_context():
        rid = _ownerless_recording()
        try:
            assert process_recording_chunks(rid) is True
            assert TranscriptChunk.query.filter_by(recording_id=rid).count() == 0
        finally:
            db.session.rollback()
            Recording.query.filter_by(id=rid).delete()
            db.session.commit()


def test_the_index_job_of_an_ownerless_recording_does_not_fail():
    with app.app_context():
        rid = _ownerless_recording()
        try:
            rec = db.session.get(Recording, rid)
            job_queue._run_index_chunks(None, rec, {})   # does not raise
        finally:
            db.session.rollback()
            Recording.query.filter_by(id=rid).delete()
            db.session.commit()
