import os, sys, uuid

import pytest

from src.app import app, db
from src.models import User, Recording, ProcessingJob
from src.api import auth as auth_mod
from src.services import transcription_translation as tt

GOOD_PW = "Sup3rSecret!"


@pytest.fixture(autouse=True)
def _disable_csrf():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = prev


@pytest.fixture
def cleanup_users():
    created = {"usernames": set(), "emails": set()}
    yield created
    q = User.query.filter(
        db.or_(
            User.username.in_(created["usernames"] or [""]),
            User.email.in_(created["emails"] or [""]),
        )
    )
    for u in q.all():
        db.session.delete(u)
    db.session.commit()


def _mk_user(cleanup):
    username = "u_" + uuid.uuid4().hex[:14]
    email = uuid.uuid4().hex[:10] + "@example.com"
    hashed = auth_mod.bcrypt.generate_password_hash(GOOD_PW).decode('utf-8')
    u = User(username=username, email=email, password=hashed)
    db.session.add(u)
    db.session.commit()
    cleanup["usernames"].add(username)
    cleanup["emails"].add(email)
    return u


def test_job_failure_leaves_transcript_and_status(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model exploded")
    monkeypatch.setattr(tt, '_call_llm', boom)

    cleanup = {"usernames": set(), "emails": set()}
    with app.app_context():
        user = _mk_user(cleanup)
        rec = Recording(user_id=user.id, transcription="Hello world this is a test transcript", status='COMPLETED')
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
        uid = user.id
        orig_status = rec.status
        orig_transcript = rec.transcription

        from src.services.job_queue import job_queue
        job_id = job_queue.enqueue(user_id=uid, recording_id=rid, job_type='translate_transcription',
                                   params={'user_id': uid, 'target_language': 'Spanish'})
        job = db.session.get(ProcessingJob, job_id)
        for _ in range(3):
            db.session.refresh(job)
            if job.status not in ('queued', 'processing'):
                break
            job_queue._process_job(job)
            db.session.commit()
            db.session.refresh(job)

        rec2 = db.session.get(Recording, rid)
        job2 = db.session.get(ProcessingJob, job_id)
        assert rec2.status == orig_status
        assert rec2.transcription == orig_transcript
        assert rec2.transcription_translation is None
        assert job2.status == 'failed'
        assert 'exploded' in (job2.error_message or '')

        db.session.delete(job2)
        db.session.delete(rec2)
        db.session.delete(user)
        db.session.commit()


def test_same_language_job_copies_without_calling_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('the LLM must not be called for a same-language copy')

    monkeypatch.setattr(tt, '_call_llm', boom)

    cleanup = {"usernames": set(), "emails": set()}
    with app.app_context():
        user = _mk_user(cleanup)
        original = "Hola mundo, esta transcripcion ya esta en espanol."
        rec = Recording(user_id=user.id, transcription=original, status='COMPLETED',
                        transcription_language='es',
                        transcription_requested_language='es')
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
        uid = user.id

        from src.services.job_queue import job_queue
        job_id = job_queue.enqueue(user_id=uid, recording_id=rid, job_type='translate_transcription',
                                   params={'user_id': uid, 'target_language': 'Spanish'})
        job = db.session.get(ProcessingJob, job_id)
        for _ in range(3):
            db.session.refresh(job)
            if job.status not in ('queued', 'processing'):
                break
            job_queue._process_job(job)
            db.session.commit()
            db.session.refresh(job)

        rec2 = db.session.get(Recording, rid)
        job2 = db.session.get(ProcessingJob, job_id)
        assert job2.status == 'completed'
        assert rec2.transcription == original
        assert rec2.transcription_translation == original
        assert rec2.transcription_translation_language == 'Spanish'
        assert rec2.translation_duration_seconds is None

        db.session.delete(job2)
        db.session.delete(rec2)
        db.session.delete(user)
        db.session.commit()


def test_auto_detect_job_still_calls_llm(monkeypatch):
    calls = []

    def fake_call(messages, user_id):
        calls.append(messages)
        content = messages[-1]['content']
        import json as _j
        start = content.find('{')
        end = content.rfind('}')
        payload = _j.loads(content[start:end + 1])
        return _j.dumps({k: (v + ' [es]' if isinstance(v, str) else v)
                         for k, v in payload.items()}, ensure_ascii=False)

    monkeypatch.setattr(tt, '_call_llm', fake_call)

    cleanup = {"usernames": set(), "emails": set()}
    with app.app_context():
        user = _mk_user(cleanup)
        original = "Hello world, this transcript was auto-detected."
        rec = Recording(user_id=user.id, transcription=original, status='COMPLETED',
                        transcription_language='en',
                        transcription_requested_language=None)
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
        uid = user.id

        from src.services.job_queue import job_queue
        job_id = job_queue.enqueue(user_id=uid, recording_id=rid, job_type='translate_transcription',
                                   params={'user_id': uid, 'target_language': 'Spanish'})
        job = db.session.get(ProcessingJob, job_id)
        for _ in range(3):
            db.session.refresh(job)
            if job.status not in ('queued', 'processing'):
                break
            job_queue._process_job(job)
            db.session.commit()
            db.session.refresh(job)

        rec2 = db.session.get(Recording, rid)
        job2 = db.session.get(ProcessingJob, job_id)
        assert calls, 'auto-detected transcriptions must still be translated'
        assert job2.status == 'completed'
        assert rec2.transcription == original
        assert rec2.transcription_translation != original
        assert rec2.transcription_translation.endswith('[es]')
        assert rec2.translation_duration_seconds is not None

        db.session.delete(job2)
        db.session.delete(rec2)
        db.session.delete(user)
        db.session.commit()



def test_concurrent_enqueue_keeps_one_active_job():
    """Simultaneous translate requests must share one job (ticket 07 dedup).

    Two tabs, two workers or two app processes can all pass the "is there an
    active job?" check before any of them inserts. The partial unique index on
    (recording_id, job_type) makes that check-then-insert atomic, and the
    in-process enqueue lock keeps the local race out of the database.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from src.services.job_queue import job_queue

    job_queue.init_app(app)

    with app.app_context():
        user = _mk_user({"usernames": set(), "emails": set()})
        rec = Recording(user_id=user.id, transcription="Hello world.", status='COMPLETED')
        db.session.add(rec)
        db.session.commit()
        rid, uid = rec.id, user.id

    barrier = threading.Barrier(4)

    def fire():
        barrier.wait()
        return job_queue.enqueue(
            user_id=uid,
            recording_id=rid,
            job_type='translate_transcription',
            params={'user_id': uid, 'target_language': 'Spanish'},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = [future.result() for future in [pool.submit(fire) for _ in range(4)]]

    assert len(set(ids)) == 1, f"concurrent enqueues returned several job ids: {ids}"
    with app.app_context():
        jobs = ProcessingJob.query.filter_by(
            recording_id=rid, job_type='translate_transcription').all()
        assert len(jobs) == 1, [j.id for j in jobs]
        assert jobs[0].id == ids[0]
        assert jobs[0].status in ('queued', 'processing')

        db.session.delete(jobs[0])
        db.session.delete(db.session.get(Recording, rid))
        db.session.delete(db.session.get(User, uid))
        db.session.commit()
