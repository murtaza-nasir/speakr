"""Translation chunk progress, the same-language short-circuit and the
already-running answer of the translate endpoint."""

import json
import uuid

import pytest
from sqlalchemy import text

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
def ctx():
    with app.app_context():
        created = []
        yield created
        db.session.rollback()
        for uid in created:
            ProcessingJob.query.filter_by(user_id=uid).delete()
            Recording.query.filter_by(user_id=uid).delete()
            user = db.session.get(User, uid)
            if user:
                db.session.delete(user)
        db.session.commit()


def _mk_user(created, **extra):
    hashed = auth_mod.bcrypt.generate_password_hash(GOOD_PW).decode('utf-8')
    u = User(username="u_" + uuid.uuid4().hex[:14], email=uuid.uuid4().hex[:10] + "@example.com",
             password=hashed, **extra)
    db.session.add(u)
    db.session.commit()
    created.append(u.id)
    return u


def _segments(n):
    return json.dumps([
        {"speaker": "A", "sentence": f"Sentence number {i}.", "start_time": i, "end_time": i + 1}
        for i in range(n)
    ])


def _echo_llm(calls, drop_first=False):
    """A fake model that appends [it] to each value; optionally drops a key once."""
    state = {'dropped': False}

    def fake(messages, user_id, prefix_opt):
        content = messages[-1]['content']
        payload = json.loads(content[content.find('{'):content.rfind('}') + 1])
        calls.append(len(payload))
        out = {k: v + ' [it]' for k, v in payload.items()}
        if drop_first and not state['dropped'] and len(out) > 1:
            state['dropped'] = True
            out.pop(next(iter(out)))
        return json.dumps(out, ensure_ascii=False)

    return fake


def _login(client, user):
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True


def _job_progress(job_id):
    # Read on a separate connection, as the queue poll would.
    with db.engine.connect() as conn:
        return tuple(conn.execute(
            text('SELECT progress_done, progress_total FROM processing_job WHERE id = :id'),
            {'id': job_id}).one())


# --- service -----------------------------------------------------------

def test_progress_reported_once_per_chunk_and_reaches_total(monkeypatch):
    calls = []
    monkeypatch.setattr(tt, '_call_llm', _echo_llm(calls))
    seen = []
    tt.translate_transcription_text(_segments(60), target_language='Italian',
                                    progress=lambda d, t: seen.append((d, t)))
    # 60 segments at 25 per chunk is 3 chunks.
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert len(calls) == 3


def test_split_chunk_still_counts_as_one_part(monkeypatch):
    calls = []
    monkeypatch.setattr(tt, '_call_llm', _echo_llm(calls, drop_first=True))
    seen = []
    out = tt.translate_transcription_text(_segments(60), target_language='Italian',
                                          progress=lambda d, t: seen.append((d, t)))
    # The first chunk came back short, was discarded and sent again as two halves.
    assert len(calls) == 5
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert all(s['sentence'].endswith('[it]') for s in json.loads(out))


def test_plain_text_progress(monkeypatch):
    calls = []
    monkeypatch.setattr(tt, '_call_llm', _echo_llm(calls))
    monkeypatch.setattr(tt, 'TRANSLATION_CHUNK_CHARS', 40)
    seen = []
    tt.translate_transcription_text('First paragraph here.\n\nSecond paragraph here.\n\nThird one.',
                                    target_language='Italian', progress=lambda d, t: seen.append((d, t)))
    assert seen[0][0] == 0 and seen[-1][0] == seen[-1][1] == seen[0][1] > 1
    assert [d for d, _ in seen] == list(range(seen[0][1] + 1))


def test_failing_progress_callback_does_not_fail_translation(monkeypatch):
    monkeypatch.setattr(tt, '_call_llm', _echo_llm([]))

    def boom(done, total):
        raise RuntimeError('no')

    out = tt.translate_transcription_text(_segments(3), target_language='Italian', progress=boom)
    assert json.loads(out)[0]['sentence'].endswith('[it]')


# --- job -----------------------------------------------------------------

def test_job_writes_progress_per_chunk(ctx, monkeypatch):
    from src.services.job_queue import job_queue

    user = _mk_user(ctx)
    rec = Recording(user_id=user.id, transcription=_segments(60), status='COMPLETED')
    db.session.add(rec)
    db.session.commit()
    job_id = job_queue.enqueue(user_id=user.id, recording_id=rec.id, job_type='translate_transcription',
                               params={'user_id': user.id, 'target_language': 'Italian'})
    job = db.session.get(ProcessingJob, job_id)
    job.status = 'processing'  # as a worker's claim leaves it
    db.session.commit()

    during = []
    echo = _echo_llm([])

    def fake(messages, user_id, prefix_opt):
        during.append(_job_progress(job_id))
        return echo(messages, user_id, prefix_opt)

    monkeypatch.setattr(tt, '_call_llm', fake)
    job_queue._process_job(job)

    # Before chunk n the job shows n-1 of 3 done.
    assert during == [(0, 3), (1, 3), (2, 3)]
    assert _job_progress(job_id) == (3, 3)
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job_id)
    assert job.status == 'completed'
    assert job.to_dict()['progress_done'] == 3

    client = app.test_client()
    _login(client, user)
    jobs = client.get('/api/recordings/job-queue-status').get_json()['jobs']
    mine = [j for j in jobs if j['id'] == job_id][0]
    assert (mine['progress_done'], mine['progress_total']) == (3, 3)


def test_retry_starts_progress_again(ctx, monkeypatch):
    from src.services.job_queue import job_queue

    user = _mk_user(ctx)
    rec = Recording(user_id=user.id, transcription=_segments(60), status='COMPLETED')
    db.session.add(rec)
    db.session.commit()
    job_id = job_queue.enqueue(user_id=user.id, recording_id=rec.id, job_type='translate_transcription',
                               params={'user_id': user.id, 'target_language': 'Italian'})
    job = db.session.get(ProcessingJob, job_id)
    job.status = 'processing'
    db.session.commit()

    echo = _echo_llm([])
    count = {'n': 0}

    def flaky(messages, user_id, prefix_opt):
        count['n'] += 1
        if count['n'] == 2:
            raise RuntimeError('model timed out')
        return echo(messages, user_id, prefix_opt)

    monkeypatch.setattr(tt, '_call_llm', flaky)
    job_queue._process_job(job)
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job_id)
    assert job.status == 'queued'  # re-queued for retry
    assert (job.progress_done, job.progress_total) == (1, 3)

    # The claim clears the old count, so the retry does not start at 1 of 3.
    claimed = None
    for _ in range(20):
        claimed = job_queue._claim_next_job(['translate_transcription'], 'summary')
        if claimed is None or claimed.id == job_id:
            break
    assert claimed is not None and claimed.id == job_id
    assert _job_progress(job_id) == (None, None)

    job_queue._process_job(db.session.get(ProcessingJob, job_id))
    assert _job_progress(job_id) == (3, 3)
    db.session.expire_all()
    assert db.session.get(ProcessingJob, job_id).status == 'completed'


def test_manual_retry_clears_progress(ctx):
    user = _mk_user(ctx)
    rec = Recording(user_id=user.id, transcription=_segments(3), status='COMPLETED')
    db.session.add(rec)
    db.session.commit()
    job = ProcessingJob(user_id=user.id, recording_id=rec.id, job_type='translate_transcription',
                        status='failed', progress_done=2, progress_total=5, error_message='x')
    db.session.add(job)
    db.session.commit()
    client = app.test_client()
    _login(client, user)
    assert client.post(f'/api/recordings/jobs/{job.id}/retry').status_code == 200
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job.id)
    assert (job.status, job.progress_done, job.progress_total) == ('queued', None, None)


# --- endpoint ------------------------------------------------------------

def _translate_jobs(rid):
    return ProcessingJob.query.filter_by(recording_id=rid, job_type='translate_transcription').all()


def test_same_language_returns_message_and_enqueues_nothing(ctx):
    user = _mk_user(ctx)
    rec = Recording(user_id=user.id, transcription="Ciao a tutti, questa è una prova.", status='COMPLETED',
                    transcription_language='it', transcription_requested_language='it')
    db.session.add(rec)
    db.session.commit()
    client = app.test_client()
    _login(client, user)

    resp = client.post(f'/recording/{rec.id}/translate_transcription', json={'target_language': 'Italian'})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['same_language'] is True
    assert data['job_id'] is None
    assert data['language'] == 'Italian'
    assert data['message'] == 'The transcript is already in Italian.'
    assert _translate_jobs(rec.id) == []


def test_same_language_uses_the_default_target(ctx):
    user = _mk_user(ctx, output_language='Italian')
    rec = Recording(user_id=user.id, transcription="Ciao a tutti, questa è una prova.", status='COMPLETED',
                    transcription_requested_language='it')
    db.session.add(rec)
    db.session.commit()
    client = app.test_client()
    _login(client, user)

    data = client.post(f'/recording/{rec.id}/translate_transcription', json={}).get_json()

    assert data.get('same_language') is True
    assert _translate_jobs(rec.id) == []


def test_auto_detected_recording_still_translates(ctx):
    user = _mk_user(ctx)
    # Detected as Italian, but no language was requested: the source is not
    # certain, so the model gets the text.
    rec = Recording(user_id=user.id, transcription="Ciao a tutti, questa è una prova.", status='COMPLETED',
                    transcription_language='it', transcription_requested_language=None)
    db.session.add(rec)
    db.session.commit()
    client = app.test_client()
    _login(client, user)

    data = client.post(f'/recording/{rec.id}/translate_transcription', json={'target_language': 'Italian'}).get_json()

    assert not data.get('same_language')
    assert data['job_id']
    assert [j.id for j in _translate_jobs(rec.id)] == [data['job_id']]


def test_request_while_running_returns_the_running_job(ctx):
    user = _mk_user(ctx)
    rec = Recording(user_id=user.id, transcription="Hello world, this is a test transcript.", status='COMPLETED')
    db.session.add(rec)
    db.session.commit()
    running = ProcessingJob(user_id=user.id, recording_id=rec.id, job_type='translate_transcription',
                            status='processing', params=json.dumps({'target_language': 'Italian'}),
                            progress_done=4, progress_total=18)
    db.session.add(running)
    db.session.commit()
    client = app.test_client()
    _login(client, user)

    resp = client.post(f'/recording/{rec.id}/translate_transcription', json={'target_language': 'German'})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['already_running'] is True
    assert data['job_id'] == running.id
    assert data['message'] == 'This recording is already being translated.'
    jobs = _translate_jobs(rec.id)
    assert [j.id for j in jobs] == [running.id]
    assert jobs[0].status == 'processing'
