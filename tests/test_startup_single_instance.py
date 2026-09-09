"""Background work must start in exactly one process (#384).

src/app.py runs run_startup_tasks() at module scope, so every process that
imports it runs this: the entrypoint's schema check, docker_create_admin.py,
and each gunicorn worker. The image ships three workers, so anything that
starts a thread or sweeps a table here ran five or so times per container.

The tests that were already in the tree when #384 was reported
(tests/test_job_queue_race_condition.py) missed it, and the way they missed it
is worth stating, because it shapes what is written below. They exercise job
CLAIMING with a ThreadPoolExecutor and never call start(). Claiming was never
broken. The bug was that several PROCESSES ran the queue at all. A test using
threads in one process cannot see that, no matter how thoroughly it tests the
thing it does test.

So: real processes, assertions on the duplication itself rather than on the
lock's return value, and one structural test that fails for a background task
nobody has written yet.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from unittest.mock import patch

import pytest

from src.app import app
from src.config import startup


def _reset_process_local_guards():
    """Clear the `global _x_started` flags several starters keep.

    Importing src.app has already set them in this process, so without this a
    starter that BYPASSED the election would return early on its own guard and
    look innocent. That is exactly the masking that let #384 survive: a guard
    that is useless across processes still hides the bypass from an
    in-process test.
    """
    from src.services import webhook_dispatch
    import src.file_monitor as file_monitor
    webhook_dispatch._dispatcher_thread_started = False
    startup._cleanup_thread_started = False
    file_monitor.file_monitor = None


@pytest.fixture
def unguarded():
    """Run with the process-local guards down, and put them back after."""
    from src.services import webhook_dispatch
    import src.file_monitor as file_monitor
    saved = (webhook_dispatch._dispatcher_thread_started,
             startup._cleanup_thread_started,
             file_monitor.file_monitor)
    _reset_process_local_guards()
    yield
    (webhook_dispatch._dispatcher_thread_started,
     startup._cleanup_thread_started,
     file_monitor.file_monitor) = saved


# ---------------------------------------------------------------------------
# the structural test: does anything bypass the gate?
# ---------------------------------------------------------------------------

def test_run_startup_tasks_starts_no_background_work_outside_the_election(unguarded):
    """The test that should fail for a background task nobody has written yet.

    Neutralise start_single_instance, then run the whole of
    run_startup_tasks and watch for threads. Anything that appears started
    itself directly, which means it will run once per gunicorn worker.
    """
    started_threads = []
    real_start = threading.Thread.start

    def record_and_skip(self):
        started_threads.append(self.name)
        # Do not actually run it; we only care that it was asked to start.

    with patch.object(startup, 'start_single_instance', return_value=False), \
            patch.object(threading.Thread, 'start', record_and_skip):
        startup.run_startup_tasks(app)

    assert started_threads == [], (
        "run_startup_tasks started these threads without going through "
        f"start_single_instance(), so each gunicorn worker will start its own: {started_threads}"
    )
    threading.Thread.start = real_start


def test_every_background_starter_is_registered_for_an_election(unguarded):
    """Each gated call must use a name from SINGLE_INSTANCE_LOCKS.

    Catches a new task gated with an ad-hoc string, which would elect
    correctly but silently drop out of this module's inventory.
    """
    used = []

    def capture(app_, lock_name, start, description):
        used.append(lock_name)
        return False

    with patch.object(startup, 'start_single_instance', capture):
        startup.run_startup_tasks(app)

    assert used, "run_startup_tasks elected nothing at all"
    unknown = [n for n in used if n not in startup.SINGLE_INSTANCE_LOCKS]
    assert unknown == [], f"lock names missing from SINGLE_INSTANCE_LOCKS: {unknown}"
    # Every registered lock should actually be used; a stale name means a task
    # was removed and the inventory not updated.
    unused = [n for n in startup.SINGLE_INSTANCE_LOCKS if n not in used]
    assert unused == [], f"declared but never elected on: {unused}"


def test_the_job_queue_is_bound_in_every_process_even_when_it_loses(unguarded):
    """Losing the election must not stop a process accepting uploads.

    A web worker that cannot enqueue is worse than a duplicated transcription.
    """
    with patch.object(startup, 'start_single_instance', return_value=False), \
            patch.object(threading.Thread, 'start', lambda self: None):
        startup.run_startup_tasks(app)

    from src.services.job_queue import job_queue
    assert job_queue._app is not None, "queue was not bound in a non-owner process"


# ---------------------------------------------------------------------------
# the cross-process test: what actually happens with several workers
# ---------------------------------------------------------------------------
#
# Real subprocesses, not fork. Two reasons. A gunicorn worker without
# --preload imports the app fresh, so a fresh interpreter is the honest
# simulation. And forking a process that already has background threads
# holding locks can deadlock the child, which is exactly what happened when
# this was first written with multiprocessing: the file passed alone and hung
# the whole suite.
#
# Lock names are namespaced per run because this test process, and the dev
# server if one is running, already hold the production names; children would
# otherwise just measure the parent winning.

# Booting the app prints a page of startup logging to stdout, so the result
# is handed over in a file rather than parsed out of that noise.
def _run_workers(count, timeout=120):
    """Boot `count` app processes at once, collect what each one started.

    They are held alive together on purpose. Run one after another, each
    would win every election and the test would pass while proving nothing.
    """
    run_id = f'speakr_test_{os.getpid()}_{uuid.uuid4().hex[:8]}'
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    procs, out_paths = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            out = os.path.join(tmp, f'worker-{i}.json')
            out_paths.append(out)
            procs.append(subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), run_id, out],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                cwd=repo_root))
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if all(os.path.exists(p) and os.path.getsize(p) > 0 for p in out_paths):
                    break
                dead = [p for p in procs if p.poll() is not None]
                if dead:
                    raise AssertionError(
                        f'a worker exited before reporting: {dead[0].stderr.read()[-2000:]}')
                time.sleep(0.2)
            else:
                raise AssertionError(f'workers did not all report within {timeout}s')
            return [json.loads(open(p).read()) for p in out_paths]
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    p.kill()


def test_three_workers_booting_together_start_each_task_exactly_once():
    """The shipped configuration: the Dockerfile CMD runs --workers 3."""
    per_process = _run_workers(3)

    started = [name for r in per_process for name in r['started']]
    assert started, f"no process started anything; election failed closed: {per_process}"

    duplicated = sorted({n for n in started if started.count(n) > 1})
    assert not duplicated, (
        f"these ran in more than one of 3 processes: {duplicated}. "
        f"per-process breakdown: {per_process}"
    )

    assert len(set(started)) == len(startup.SINGLE_INSTANCE_LOCKS), (
        f"expected all {len(startup.SINGLE_INSTANCE_LOCKS)} tasks to run once across "
        f"3 processes, got {sorted(set(started))}"
    )


def test_eight_workers_do_not_start_eight_dispatchers():
    """More workers must not mean more duplication. #384 scaled with the
    worker count: 3 workers produced 2 ASR submissions of one file, 8 produced 7.
    """
    per_process = _run_workers(8)
    started = [name for r in per_process for name in r['started']]
    for task in ('webhook dispatcher', 'job queue', 'auto-deletion scheduler',
                 'automated file processing', 'recording-session cleanup'):
        assert started.count(task) == 1, (
            f"{task!r} started {started.count(task)} times across 8 processes: {per_process}")


def test_every_worker_can_still_enqueue_even_though_only_one_owns_the_queue():
    """Seven of eight processes lose, and all eight must still take uploads."""
    per_process = _run_workers(8)
    assert all(r['queue_bound'] for r in per_process), per_process
    owners = [r for r in per_process if 'job queue' in r['started']]
    assert len(owners) == 1, per_process


# ---------------------------------------------------------------------------
# failure behaviour
# ---------------------------------------------------------------------------

def test_a_broken_election_still_starts_the_work():
    """Fail open. An election that cannot run must not silently disable every
    background task, which would leave an app that accepts uploads and
    transcribes nothing."""
    ran = []
    with app.app_context(), \
            patch('src.utils.database.acquire_singleton_lock', side_effect=OSError('no locks')):
        result = startup.start_single_instance(
            app, 'speakr.test_broken', lambda: ran.append('started'), 'test task')

    assert ran == ['started']
    assert result is True


def test_a_task_that_throws_does_not_stop_the_others():
    """One failing starter must not take out the rest of startup."""
    with app.app_context():
        result = startup.start_single_instance(
            app, 'speakr.test_throws', lambda: (_ for _ in ()).throw(RuntimeError('boom')),
            'exploding task')
    assert result is False


def test_losing_is_not_reported_as_a_failure():
    """A loser returns False, same as a crashed starter, so the two must be
    distinguishable by their logging rather than only by the return value."""
    with app.app_context(), \
            patch('src.utils.database.acquire_singleton_lock', return_value=False), \
            patch.object(app.logger, 'debug') as debug, \
            patch.object(app.logger, 'warning') as warn:
        result = startup.start_single_instance(
            app, 'speakr.test_loser', lambda: None, 'test task')
    assert result is False
    assert debug.called, "a lost election should log at debug, it is the normal case"
    assert not warn.called, "a lost election is not a warning"


# ---------------------------------------------------------------------------
# worker entry point (not collected by pytest; see _run_workers above)
# ---------------------------------------------------------------------------

def _main():
    """Boot like a gunicorn worker and report what this process started."""
    run_id, out_path = sys.argv[1], sys.argv[2]
    _reset_process_local_guards()

    started = []
    real = startup.start_single_instance

    def gated(app_, lock_name, start, description):
        return real(app_, f'{run_id}.{lock_name}',
                    lambda: started.append(description), description)

    from src.services.job_queue import job_queue
    with patch.object(startup, 'start_single_instance', gated), \
            patch.object(threading.Thread, 'start', lambda self: None):
        startup.run_startup_tasks(app)

    with open(out_path, 'w') as f:
        json.dump({'started': sorted(started),
                   'queue_bound': job_queue._app is not None}, f)
    # Hold the locks so siblings genuinely contend rather than inheriting a
    # free field from a process that already exited.
    time.sleep(60)


if __name__ == '__main__':
    _main()
