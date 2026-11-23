"""Connections of a request and of the job queue (#667).

A status poll held one pooled connection for its request and took another
for each of the three queue lookups it made, because every lookup pushed an
application context of its own (Flask-SQLAlchemy keeps one session per
context). The pool itself was SQLAlchemy's default of 5 + 10, below the
request and job threads of the process that owns the queue. A request with an
API token also committed a write of last_used_at every time.
"""

import os
import secrets
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from src.app import app
from src.database import db
from src.models import APIToken, ProcessingJob, Recording, User
from src.services.job_queue import job_queue
from src.utils import db_pool
from src.utils.token_auth import LAST_USED_INTERVAL, _mark_used, hash_token


# --- pool size --------------------------------------------------------------

def test_default_pool_covers_request_job_and_service_threads():
    opts = db_pool.pool_options({}, 'sqlite:////data/instance/transcriptions.db')
    needed = db_pool.REQUEST_THREADS + 7 + db_pool.SERVICE_THREADS
    assert opts['pool_size'] + opts['max_overflow'] >= needed + db_pool.MARGIN
    assert opts['pool_timeout'] == 30


def test_more_job_workers_get_more_connections():
    small = db_pool.pool_options({}, 'postgresql://x/y')
    big = db_pool.pool_options({'JOB_QUEUE_WORKERS': '12'}, 'postgresql://x/y')
    assert big['max_overflow'] == small['max_overflow'] + 10


def test_pool_settings_from_the_environment():
    opts = db_pool.pool_options({'DB_POOL_SIZE': '4', 'DB_MAX_OVERFLOW': '0', 'DB_POOL_TIMEOUT': '5'},
                                'postgresql://x/y')
    assert opts == {'pool_size': 4, 'max_overflow': 0, 'pool_timeout': 5}


@pytest.mark.parametrize('value', ['many', '-1', '0'])
def test_an_invalid_pool_size_falls_back_to_the_default(value):
    opts = db_pool.pool_options({'DB_POOL_SIZE': value}, 'postgresql://x/y')
    assert opts['pool_size'] == db_pool.DEFAULT_POOL_SIZE


@pytest.mark.parametrize('uri', ['sqlite://', 'sqlite:///:memory:'])
def test_an_in_memory_sqlite_takes_no_pool_arguments(uri):
    assert db_pool.pool_options({}, uri) == {}


def test_the_app_engine_uses_the_configured_pool():
    expected = db_pool.pool_options(os.environ, app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        pool = db.engine.pool
        assert pool.size() == expected['pool_size']
        assert pool._max_overflow == expected['max_overflow']
        assert pool._timeout == expected['pool_timeout']


# --- queue lookups inside a request -----------------------------------------

@pytest.fixture
def recording_with_job():
    with app.app_context():
        stamp = f"{os.getpid()}_{datetime.utcnow().timestamp()}"
        user = User(username=f"pool_{os.getpid()}"[:20], email=f"pool_{stamp}@example.com")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="Pool", status="PENDING", audio_path="/tmp/pool.mp3")
        db.session.add(rec)
        db.session.commit()
        job_queue.init_app(app)
        job_queue.enqueue(user.id, rec.id, 'transcribe')
        ids = (user.id, rec.id)
    yield ids
    with app.app_context():
        ProcessingJob.query.filter_by(recording_id=ids[1]).delete()
        Recording.query.filter_by(id=ids[1]).delete()
        User.query.filter_by(id=ids[0]).delete()
        db.session.commit()


def test_queue_lookups_in_a_request_use_the_request_connection(recording_with_job):
    _, rid = recording_with_job
    checkouts = []

    with app.test_request_context('/'):
        engine = db.engine
        db.session.connection()   # the request holds its connection

        def counted(*args):
            checkouts.append(1)

        event.listen(engine, 'checkout', counted)
        try:
            assert job_queue.get_position_in_queue(rid) is not None
            assert job_queue.get_stage(rid) == 'transcription'
            assert job_queue.get_active_job(rid).recording_id == rid
            assert job_queue.get_job_for_recording(rid) is not None
            job_queue.get_queue_status()
        finally:
            event.remove(engine, 'checkout', counted)

    assert checkouts == []


def test_queue_lookups_outside_a_context_still_work(recording_with_job):
    _, rid = recording_with_job
    assert job_queue.get_stage(rid) == 'transcription'


# --- API token last use -------------------------------------------------------

@pytest.fixture
def token():
    with app.app_context():
        stamp = f"{os.getpid()}_{datetime.utcnow().timestamp()}"
        user = User(username=f"tok_{os.getpid()}"[:20], email=f"tok_{stamp}@example.com")
        db.session.add(user)
        db.session.commit()
        row = APIToken(user_id=user.id, token_hash=hash_token(secrets.token_urlsafe(16)), name='poll')
        db.session.add(row)
        db.session.commit()
        yield row
        db.session.rollback()
        APIToken.query.filter_by(id=row.id).delete()
        User.query.filter_by(id=user.id).delete()
        db.session.commit()


def _stored_last_used(token_id):
    with db.engine.connect() as conn:
        value = conn.execute(db.text("SELECT last_used_at FROM api_token WHERE id = :i"),
                             {"i": token_id}).scalar()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value


def test_last_used_is_written_at_most_once_a_minute(token):
    _mark_used(token)
    first = _stored_last_used(token.id)
    assert first is not None

    _mark_used(token)
    assert _stored_last_used(token.id) == first


def test_last_used_is_written_again_after_a_minute(token):
    _mark_used(token)
    old = datetime.utcnow() - LAST_USED_INTERVAL - timedelta(seconds=5)
    token.last_used_at = old
    db.session.commit()

    _mark_used(token)
    assert _stored_last_used(token.id) > old + LAST_USED_INTERVAL
