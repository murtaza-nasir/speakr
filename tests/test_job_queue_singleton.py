"""The job queue must run in exactly one process.

src/app.py executes run_startup_tasks() at module scope, so every process that
imports it -- the entrypoint's schema check, the admin-user script, and each
gunicorn worker -- used to start its own job-queue workers and run its own
orphan recovery.

recover_orphaned_jobs() resets every 'processing' job to 'queued' with no way
to tell a crash-orphaned job from one a live sibling is transcribing right now.
So a second process starting up un-claimed in-flight work, a worker re-claimed
it, and the same audio went to the ASR service twice at once -- N times for N
processes. That is why the deployment guidance was "never run more than one
gunicorn worker".

These tests pin the election that fixes it.
"""

import multiprocessing as mp
import os

import pytest
from sqlalchemy import create_engine

from src.app import app
from src.database import db
from src.models import ProcessingJob, Recording, User
from src.services.job_queue import job_queue
from src.utils.database import acquire_singleton_lock


# Force 'fork' explicitly: the child must inherit the module state it is
# testing, and the default start method varies by platform.
try:
    _MP = mp.get_context("fork")
except ValueError:  # pragma: no cover - non-POSIX
    pytest.skip("requires fork", allow_module_level=True)


@pytest.fixture
def sqlite_url(tmp_path):
    """A private database file, so these tests elect leaders in isolation."""
    return f"sqlite:///{tmp_path / 'elect.db'}"


def _contend(url, name, barrier, results):
    """Child process: try to win the election, report whether it did."""
    engine = create_engine(url)
    if barrier is not None:
        barrier.wait()
    results.put(acquire_singleton_lock(engine, name))


def _run_contenders(url, name, count):
    barrier = _MP.Barrier(count)
    results = _MP.Queue()
    procs = [
        _MP.Process(target=_contend, args=(url, name, barrier, results))
        for _ in range(count)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    return [results.get() for _ in range(count)]


# ---------------------------------------------------------------------------
# the election itself
# ---------------------------------------------------------------------------

def test_exactly_one_process_wins_the_election(sqlite_url):
    """The property the whole fix rests on."""
    url = sqlite_url
    won = _run_contenders(url, "speakr.test_owner", count=5)
    assert sum(won) == 1, f"expected exactly one owner, got {sum(won)} of 5"


def test_a_different_name_elects_a_separate_owner(sqlite_url):
    """Responsibilities are independent; one owner must not block another."""
    url = sqlite_url
    assert sum(_run_contenders(url, "speakr.test_a", count=3)) == 1
    assert sum(_run_contenders(url, "speakr.test_b", count=3)) == 1


def test_leadership_is_released_when_the_holder_dies(sqlite_url):
    """Leadership must survive a crash, not outlive it.

    The lock is never explicitly released, so the only thing that frees it is
    process death. If that did not work, one killed gunicorn worker would stop
    all background processing until the container was restarted.
    """
    url = sqlite_url
    name = "speakr.test_recovery"

    # A child takes the lock and exits, holding it for its whole life.
    assert _run_contenders(url, name, count=1) == [True]

    # With the holder gone, the next process must be able to win.
    assert _run_contenders(url, name, count=1) == [True]


def test_asking_twice_in_one_process_is_stable(sqlite_url):
    url = sqlite_url
    engine = create_engine(url)
    first = acquire_singleton_lock(engine, "speakr.test_idempotent")
    second = acquire_singleton_lock(engine, "speakr.test_idempotent")
    assert first is True
    assert second is first


# ---------------------------------------------------------------------------
# what the election gates
# ---------------------------------------------------------------------------

@pytest.fixture
def unowned_queue():
    """The queue as a non-owner process sees it."""
    previous = job_queue._is_owner
    job_queue._is_owner = False
    yield job_queue
    job_queue._is_owner = previous
    job_queue._running = False


def test_a_non_owner_starts_no_workers(unowned_queue):
    unowned_queue.start()
    assert unowned_queue._running is False
    assert unowned_queue._transcription_workers == []
    assert unowned_queue._summary_workers == []


def test_an_owner_starts_workers(unowned_queue):
    unowned_queue.mark_as_owner()
    unowned_queue.start()
    assert unowned_queue._running is True
    unowned_queue.stop()


def test_enqueue_does_not_make_a_non_owner_start_working(unowned_queue):
    """Close the back door in enqueue().

    enqueue() auto-starts workers when the queue is idle. Without the guard in
    start(), any web worker that accepted an upload would promote itself into a
    transcription process -- reintroducing the duplicate submissions from a
    different direction. The job must still be enqueued: a non-owner accepts
    uploads, it just does not transcribe them.
    """
    unowned_queue.init_app(app)

    with app.app_context():
        user = User(username=f"sing_{os.getpid()}"[:20],
                    email=f"sing_{os.getpid()}@example.com")
        if hasattr(user, "password"):
            user.password = "unused"
        db.session.add(user)
        db.session.commit()

        rec = Recording(user_id=user.id, title="singleton test",
                        audio_path="/tmp/singleton_test.mp3",
                        original_filename="singleton_test.mp3",
                        status="PENDING")
        db.session.add(rec)
        db.session.commit()

        try:
            job_id = unowned_queue.enqueue(user.id, rec.id, "transcribe")

            # The work was accepted...
            assert db.session.get(ProcessingJob, job_id) is not None
            # ...but this process did not appoint itself to do it.
            assert unowned_queue._running is False
            assert unowned_queue._transcription_workers == []
        finally:
            for job in ProcessingJob.query.filter_by(recording_id=rec.id).all():
                db.session.delete(job)
            db.session.commit()
            db.session.delete(rec)
            db.session.delete(user)
            db.session.commit()


# ---------------------------------------------------------------------------
# how the election fails
# ---------------------------------------------------------------------------

def test_an_election_error_leaves_this_process_in_charge(monkeypatch, sqlite_url):
    """Fail open: the worst case must be today's behaviour, never a dead queue.

    If the lock cannot be taken at all, refusing ownership would leave an app
    that looks healthy, accepts uploads and never transcribes them. Every
    process taking ownership instead degrades to the pre-election behaviour,
    which processes work, at the cost of the duplicates this fix removes.
    """
    import src.utils.database as database

    def broken_flock(*args, **kwargs):
        raise OSError("simulated: lock directory not writable")

    monkeypatch.setattr(database.fcntl, "flock", broken_flock)
    engine = create_engine(sqlite_url)
    assert acquire_singleton_lock(engine, "speakr.test_fail_open") is True


def test_the_election_never_opens_a_database_connection():
    """Leadership must not depend on a connection nobody watches.

    A session-level advisory lock vanishes when its connection does -- server
    restart, pooler, idle-timeout -- while the owner's local flag says it still
    owns the queue. The next process then wins a second election and the
    duplicate submissions are back. So the election must not use the database
    at all, whichever backend it is.
    """

    class _EngineThatRefusesConnections:
        name = "postgresql"
        url = "postgresql://probe@nowhere/elect"

        def connect(self):
            raise AssertionError("the election opened a database connection")

    assert acquire_singleton_lock(
        _EngineThatRefusesConnections(), "speakr.test_no_connection"
    ) is True
