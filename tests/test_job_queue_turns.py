"""Users take turns in the job queue, one job each, whatever order they uploaded in."""

import os
from datetime import datetime, timedelta

import pytest

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import job_queue


@pytest.fixture
def queue():
    job_queue.init_app(app)
    job_queue._turns = {'transcription': {}, 'summary': {}}
    with app.app_context():
        # Jobs left by other test files would take turns too.
        stale = ProcessingJob.query.filter(ProcessingJob.status == 'queued').all()
        for job in stale:
            job.status = 'completed'
        db.session.commit()
        made = {'users': [], 'recordings': []}
        yield made
        for rid in made['recordings']:
            for job in ProcessingJob.query.filter_by(recording_id=rid).all():
                db.session.delete(job)
        db.session.commit()
        for rid in made['recordings']:
            db.session.delete(db.session.get(Recording, rid))
        db.session.commit()
        for uid in made['users']:
            db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _user(made, name):
    suffix = f"{os.getpid()}_{datetime.utcnow().timestamp()}"
    user = User(username=f"turn_{name}_{os.getpid()}"[:20], email=f"turn_{name}_{suffix}@example.com")
    db.session.add(user)
    db.session.commit()
    made['users'].append(user.id)
    return user.id


def _upload(made, user_id, label, seconds):
    """Queue a transcription job created `seconds` after a fixed start."""
    rec = Recording(user_id=user_id, title=label, audio_path='/tmp/turn.mp3', status='PENDING')
    db.session.add(rec)
    db.session.commit()
    made['recordings'].append(rec.id)
    job_id = job_queue.enqueue(user_id, rec.id, 'transcribe')
    job = db.session.get(ProcessingJob, job_id)
    job.created_at = datetime(2026, 1, 1) + timedelta(seconds=seconds)
    db.session.commit()


def _claim_all():
    """Claim jobs one by one, finishing each before the next claim."""
    order = []
    while True:
        job = job_queue._claim_next_job(['transcribe'], 'transcription')
        if job is None:
            return order
        order.append(db.session.get(Recording, job.recording_id).title)
        job.status = 'completed'
        db.session.commit()


def test_three_users_with_three_files_each_take_turns(queue):
    a, b, c = (_user(queue, n) for n in 'abc')
    for i in range(3):
        _upload(queue, a, f'A{i + 1}', 0 + i * 0.01)
        _upload(queue, b, f'B{i + 1}', 1 + i * 0.01)
        _upload(queue, c, f'C{i + 1}', 2 + i * 0.01)

    assert _claim_all() == ['A1', 'B1', 'C1', 'A2', 'B2', 'C2', 'A3', 'B3', 'C3']


def test_a_later_upload_does_not_cost_a_user_a_turn(queue):
    # A's second file arrives after B's two and C's first.
    a, b, c = (_user(queue, n) for n in 'abc')
    _upload(queue, a, 'A1', 0)
    _upload(queue, b, 'B1', 1)
    _upload(queue, b, 'B2', 2)
    _upload(queue, c, 'C1', 3)
    _upload(queue, a, 'A2', 5)
    _upload(queue, c, 'C2', 10)

    assert _claim_all() == ['A1', 'B1', 'C1', 'A2', 'B2', 'C2']
