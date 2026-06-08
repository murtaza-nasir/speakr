#!/usr/bin/env python3
"""
Test script for job queue race condition fix.

This script verifies that the atomic job claiming mechanism prevents
multiple workers from claiming the same job simultaneously.

The fix lives in FairJobQueue._claim_next_job() in src/services/job_queue.py,
which uses an atomic UPDATE ... WHERE status='queued' so only one worker can
flip a job from 'queued' to 'processing'. These tests exercise that REAL
method (via the module-level `job_queue` singleton) rather than re-implementing
the claim SQL inline, so a regression in the source claim logic would fail here.
"""

import os
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_user(db, User):
    """Get the first existing user or create a throwaway test user."""
    user = User.query.first()
    if user:
        return user, False
    user = User(username='test_race_condition_user', email='test_race@example.com')
    # Different User models expose different password attributes; set whatever
    # exists so the row is valid without depending on a specific hashing path.
    if hasattr(user, 'password_hash'):
        user.password_hash = 'unused'
    db.session.add(user)
    db.session.commit()
    return user, True


def test_atomic_job_claiming():
    """
    Test that only one worker can claim a single queued job even with
    concurrent attempts against the REAL _claim_next_job method.
    """
    print("\n=== Testing Atomic Job Claiming (real _claim_next_job) ===\n")

    from src.app import app
    from src.database import db
    from src.models import ProcessingJob, User, Recording
    from src.services.job_queue import job_queue

    # Bind the singleton queue to the test app so its internal _app_context()
    # opens the test DB. We never call start(), so no worker threads spawn.
    job_queue.init_app(app)

    created_user = False
    with app.app_context():
        test_user, created_user = _make_user(db, User)

        recording = Recording(
            user_id=test_user.id,
            title='Test Race Condition Recording',
            audio_path='/tmp/test_audio.mp3',
            status='QUEUED',
        )
        db.session.add(recording)
        db.session.commit()

        job = ProcessingJob(
            recording_id=recording.id,
            user_id=test_user.id,
            job_type='transcribe',
            status='queued',
        )
        db.session.add(job)
        db.session.commit()

        job_id = job.id
        recording_id = recording.id
        print(f"Created test job {job_id} with status 'queued'")

    successful_claims = []
    claim_lock = threading.Lock()
    num_workers = 10
    barrier = threading.Barrier(num_workers)

    def worker(worker_id):
        # Each worker calls the REAL claim method. Exactly one should win.
        barrier.wait()
        claimed = job_queue._claim_next_job(['transcribe'], 'transcription')
        if claimed is not None and claimed.id == job_id:
            with claim_lock:
                successful_claims.append(worker_id)

    print(f"\nSpawning {num_workers} workers to claim job {job_id} simultaneously...")
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker, i) for i in range(num_workers)]
        for f in as_completed(futures):
            f.result()

    try:
        with app.app_context():
            db.session.expire_all()
            final_job = db.session.get(ProcessingJob, job_id)
            final_status = final_job.status
            print(f"\n=== Results ===")
            print(f"Total workers: {num_workers}")
            print(f"Successful claims: {len(successful_claims)} -> {successful_claims}")
            print(f"Final job status: {final_status}")

            assert len(successful_claims) == 1, \
                f"Expected exactly 1 successful claim, got {len(successful_claims)}"
            assert final_status == 'processing', \
                f"Expected status 'processing', got {final_status}"

        print("\n[PASS] Only one worker successfully claimed the job!")
    finally:
        with app.app_context():
            j = db.session.get(ProcessingJob, job_id)
            if j:
                db.session.delete(j)
            r = db.session.get(Recording, recording_id)
            if r:
                db.session.delete(r)
            db.session.commit()


def test_multiple_jobs_fair_distribution():
    """
    Test that the REAL _claim_next_job hands out N distinct jobs exactly once
    each (no double-claims) when called repeatedly.
    """
    print("\n=== Testing Multiple Jobs Distribution (real _claim_next_job) ===\n")

    from src.app import app
    from src.database import db
    from src.models import ProcessingJob, User, Recording
    from src.services.job_queue import job_queue

    job_queue.init_app(app)

    num_jobs = 5
    job_ids = []
    recording_ids = []

    with app.app_context():
        test_user, _ = _make_user(db, User)

        for i in range(num_jobs):
            recording = Recording(
                user_id=test_user.id,
                title=f'Test Distribution Recording {i}',
                audio_path=f'/tmp/test_audio_{i}.mp3',
                status='QUEUED',
            )
            db.session.add(recording)
            db.session.commit()
            recording_ids.append(recording.id)

            jb = ProcessingJob(
                recording_id=recording.id,
                user_id=test_user.id,
                job_type='transcribe',
                status='queued',
            )
            db.session.add(jb)
            db.session.commit()
            job_ids.append(jb.id)

        print(f"Created {num_jobs} test jobs: {job_ids}")

    claimed_jobs = []
    try:
        # Claim repeatedly via the real method until it returns None.
        # Extra attempts beyond num_jobs must yield no further claims.
        for i in range(num_jobs + 2):
            claimed = job_queue._claim_next_job(['transcribe'], 'transcription')
            if claimed is not None and claimed.id in job_ids:
                claimed_jobs.append(claimed.id)
                print(f"  Attempt {i} claimed job {claimed.id}")
            else:
                # Could be None, or (in a shared dev DB) a pre-existing job from
                # another user; ignore anything not in our created set.
                print(f"  Attempt {i} claimed no job of ours")

        print(f"\nClaimed jobs: {claimed_jobs}")
        print(f"Unique jobs claimed: {len(set(claimed_jobs))}")

        assert len(claimed_jobs) == len(set(claimed_jobs)), "Duplicate job claims detected!"
        assert len(claimed_jobs) == num_jobs, \
            f"Expected {num_jobs} claims, got {len(claimed_jobs)}"

        print("\n[PASS] All jobs claimed exactly once!")
    finally:
        # Cleanup must run even if the assertions above fail, so synthetic
        # 'Test Distribution Recording N' rows don't leak into the dev DB.
        with app.app_context():
            for jid in job_ids:
                j = db.session.get(ProcessingJob, jid)
                if j:
                    db.session.delete(j)
            for rid in recording_ids:
                r = db.session.get(Recording, rid)
                if r:
                    db.session.delete(r)
            db.session.commit()


if __name__ == '__main__':
    print("=" * 60)
    print("Job Queue Race Condition Tests")
    print("=" * 60)

    try:
        test_atomic_job_claiming()
        test_multiple_jobs_fair_distribution()

        print("\n" + "=" * 60)
        print("All tests passed!")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n[FAIL] Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
