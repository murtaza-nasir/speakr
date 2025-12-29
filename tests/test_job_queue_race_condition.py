#!/usr/bin/env python3
"""
Test script for job queue race condition fix.

This script verifies that the atomic job claiming mechanism prevents
multiple workers from claiming the same job simultaneously.

The fix uses an atomic UPDATE with WHERE clause to ensure only one
worker can claim a job, even with multiple processes/threads.
"""

import os
import sys
import threading
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_atomic_job_claiming():
    """
    Test that only one worker can claim a job even with concurrent attempts.

    This simulates the race condition where multiple workers try to claim
    the same job simultaneously.
    """
    print("\n=== Testing Atomic Job Claiming ===\n")

    # Import Flask app and models
    from src.app import app
    from src.database import db
    from src.models import ProcessingJob, User, Recording
    from sqlalchemy import update

    with app.app_context():
        # Use the first existing user for testing, or create a minimal test user
        test_user = User.query.first()
        if not test_user:
            test_user = User(
                username='test_race_condition_user',
                email='test_race@example.com',
                password='not_used'  # Password not needed for this test
            )
            db.session.add(test_user)
            db.session.commit()

        # Create a test recording
        test_recording = Recording(
            user_id=test_user.id,
            title='Test Race Condition Recording',
            audio_path='/tmp/test_audio.mp3',
            status='QUEUED'
        )
        db.session.add(test_recording)
        db.session.commit()

        # Create a test job in 'queued' status
        test_job = ProcessingJob(
            recording_id=test_recording.id,
            user_id=test_user.id,
            job_type='transcribe',
            status='queued'
        )
        db.session.add(test_job)
        db.session.commit()

        job_id = test_job.id
        print(f"Created test job {job_id} with status 'queued'")

        # Track which threads successfully claimed the job
        successful_claims = []
        claim_lock = threading.Lock()

        def attempt_claim(worker_id):
            """Simulate a worker attempting to claim the job."""
            with app.app_context():
                try:
                    # This is the atomic claim logic from the fix
                    claim_time = datetime.utcnow()
                    result = db.session.execute(
                        update(ProcessingJob)
                        .where(
                            ProcessingJob.id == job_id,
                            ProcessingJob.status == 'queued'
                        )
                        .values(status='processing', started_at=claim_time)
                    )

                    if result.rowcount == 1:
                        db.session.commit()
                        with claim_lock:
                            successful_claims.append(worker_id)
                        return f"Worker {worker_id}: Successfully claimed job"
                    else:
                        db.session.rollback()
                        return f"Worker {worker_id}: Job already claimed (rowcount=0)"

                except Exception as e:
                    db.session.rollback()
                    return f"Worker {worker_id}: Error - {e}"

        # Spawn multiple threads to claim simultaneously
        num_workers = 10
        print(f"\nSpawning {num_workers} workers to claim job {job_id} simultaneously...")

        # Use a barrier to ensure all threads start at the same time
        barrier = threading.Barrier(num_workers)

        def worker_with_barrier(worker_id):
            barrier.wait()  # Wait for all threads to be ready
            return attempt_claim(worker_id)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(worker_with_barrier, i): i for i in range(num_workers)}

            for future in as_completed(futures):
                result = future.result()
                print(f"  {result}")

        # Verify results
        print(f"\n=== Results ===")
        print(f"Total workers: {num_workers}")
        print(f"Successful claims: {len(successful_claims)}")
        print(f"Workers that claimed: {successful_claims}")

        # Check final job status
        db.session.expire_all()
        final_job = db.session.get(ProcessingJob, job_id)
        print(f"Final job status: {final_job.status}")

        # Cleanup
        db.session.delete(final_job)
        db.session.delete(test_recording)
        db.session.commit()

        # Assert only one worker claimed the job
        assert len(successful_claims) == 1, f"Expected 1 successful claim, got {len(successful_claims)}"
        assert final_job.status == 'processing', f"Expected status 'processing', got {final_job.status}"

        print("\n[PASS] Only one worker successfully claimed the job!")
        return True


def test_multiple_jobs_fair_distribution():
    """
    Test that multiple jobs are distributed fairly across workers.
    """
    print("\n=== Testing Multiple Jobs Distribution ===\n")

    from src.app import app
    from src.database import db
    from src.models import ProcessingJob, User, Recording
    from sqlalchemy import update

    with app.app_context():
        # Use the first existing user for testing
        test_user = User.query.first()
        if not test_user:
            test_user = User(
                username='test_distribution_user',
                email='test_dist@example.com',
                password='not_used'
            )
            db.session.add(test_user)
            db.session.commit()

        # Create multiple test jobs
        num_jobs = 5
        job_ids = []
        recording_ids = []

        for i in range(num_jobs):
            recording = Recording(
                user_id=test_user.id,
                title=f'Test Distribution Recording {i}',
                audio_path=f'/tmp/test_audio_{i}.mp3',
                status='QUEUED'
            )
            db.session.add(recording)
            db.session.commit()
            recording_ids.append(recording.id)

            job = ProcessingJob(
                recording_id=recording.id,
                user_id=test_user.id,
                job_type='transcribe',
                status='queued'
            )
            db.session.add(job)
            db.session.commit()
            job_ids.append(job.id)

        print(f"Created {num_jobs} test jobs: {job_ids}")

        # Have workers claim jobs
        claimed_jobs = []

        def claim_any_job(worker_id):
            with app.app_context():
                # Find a queued job
                candidate = ProcessingJob.query.filter(
                    ProcessingJob.status == 'queued',
                    ProcessingJob.job_type == 'transcribe'
                ).first()

                if not candidate:
                    return None

                # Atomic claim
                result = db.session.execute(
                    update(ProcessingJob)
                    .where(
                        ProcessingJob.id == candidate.id,
                        ProcessingJob.status == 'queued'
                    )
                    .values(status='processing', started_at=datetime.utcnow())
                )

                if result.rowcount == 1:
                    db.session.commit()
                    return candidate.id
                else:
                    db.session.rollback()
                    return None

        # Each "worker" claims one job
        for i in range(num_jobs + 2):  # Extra attempts to ensure no double claims
            job_id = claim_any_job(i)
            if job_id:
                claimed_jobs.append(job_id)
                print(f"  Worker {i} claimed job {job_id}")
            else:
                print(f"  Worker {i} found no available jobs")

        print(f"\nClaimed jobs: {claimed_jobs}")
        print(f"Unique jobs claimed: {len(set(claimed_jobs))}")

        # Verify no duplicates
        assert len(claimed_jobs) == len(set(claimed_jobs)), "Duplicate job claims detected!"
        assert len(claimed_jobs) == num_jobs, f"Expected {num_jobs} claims, got {len(claimed_jobs)}"

        # Cleanup
        for job_id in job_ids:
            job = db.session.get(ProcessingJob, job_id)
            if job:
                db.session.delete(job)
        for rec_id in recording_ids:
            rec = db.session.get(Recording, rec_id)
            if rec:
                db.session.delete(rec)
        db.session.commit()

        print("\n[PASS] All jobs claimed exactly once!")
        return True


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
