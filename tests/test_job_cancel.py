"""Cancel of a waiting or running processing job.

DELETE /api/recordings/jobs/<id> removes a waiting job at once and marks a
running one; the worker stops at its next safe point (between chunks, at a
WhisperX poll, before an LLM call), the WhisperX job is deleted on the
service, the temp files go and the recording is settled: COMPLETED when a
transcript is stored, as it was before a canceled reprocess, FAILED with the
canceled message after a canceled first transcription. A cancel sends no
webhook and no email.

SHARED-DB: every assertion is scoped to the rows the test created.
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Event, ProcessingJob, Recording, User
from src.services import job_cancel, job_control
from src.services.job_cancel import CANCELED_MESSAGE
from src.services.job_queue import FairJobQueue, job_queue
from src.services.transcription.base import TranscriptionRequest, TranscriptionResponse
from src.utils.error_formatting import is_transcription_error
import src.tasks.processing as proc
from tests.test_asr_async_jobs import BASE, FakeWhisperX, _connector

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

TRANSCRIPT = "Speaker 1: We agreed to move the launch to the next quarter."
SUMMARY = "The launch moves to the next quarter."
NOTICES = ('_emit_failure_webhook', '_emit_failure_email', '_emit_completion_webhook',
           '_emit_completion_email', '_emit_started_webhook')


@pytest.fixture(autouse=True)
def _queue_app():
    job_queue.init_app(app)
    with app.app_context():
        # Jobs other test files left waiting would be claimed first.
        ProcessingJob.query.filter(ProcessingJob.status == 'queued').update(
            {'status': 'completed', 'completed_at': datetime(2020, 1, 1)}, synchronize_session=False)
        db.session.commit()
        yield
        db.session.rollback()


@pytest.fixture
def notices():
    """Every notice the queue can send, as mocks."""
    patches = [patch.object(job_queue, name) for name in NOTICES]
    mocks = {name: p.start() for name, p in zip(NOTICES, patches)}
    with patch('src.services.webhook_dispatch.emit_webhook_event') as raw:
        mocks['raw'] = raw
        yield mocks
    for p in patches:
        p.stop()


def _assert_no_notice(notices):
    for name in ('_emit_failure_webhook', '_emit_failure_email', '_emit_completion_webhook',
                 '_emit_completion_email'):
        assert not notices[name].called, name
    sent = [call.kwargs.get('event_type') for call in notices['raw'].call_args_list]
    assert not [e for e in sent if e and ('failed' in e or 'completed' in e or 'cancel' in e)], sent


def _user(admin=False):
    suffix = uuid.uuid4().hex[:10]
    user = User(username=f"jc_{suffix}", email=f"jc_{suffix}@local.test", password='x', is_admin=admin)
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user=None, status='QUEUED', **fields):
    user = user or _user()
    fields.setdefault('title', 'Launch sync')
    recording = Recording(user_id=user.id, original_filename='m.wav',
                          audio_path='local://m.wav', status=status, **fields)
    db.session.add(recording)
    db.session.commit()
    return recording


class _Ids:
    """The ids of a row, readable after the row is gone."""

    def __init__(self, row):
        self.id = row.id
        self.recording_id = getattr(row, 'recording_id', None)
        self.user_id = row.user_id


def _job(recording, job_type='transcribe', status='queued', **fields):
    job = ProcessingJob(user_id=recording.user_id, recording_id=recording.id, job_type=job_type,
                        status=status, **fields)
    if status == 'processing':
        job.started_at = datetime.utcnow()
    db.session.add(job)
    db.session.commit()
    return _Ids(job)


def _client(user_id):
    # The tests hold one app context, and flask-login keeps the user of the
    # last request on it: a request as someone else starts without it.
    from flask import g, has_app_context
    if has_app_context():
        g.pop('_login_user', None)
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    return client


def _cancel(user_id, job_id):
    return _client(user_id).delete(f'/api/recordings/jobs/{job_id}')


def _mark(job_id):
    """What the route does for a running job, on a connection of its own:
    the worker's session is another thread's in production."""
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE processing_job SET cancel_requested_at = :at "
                          "WHERE id = :id AND status = 'processing'"),
                     {'at': datetime.utcnow(), 'id': job_id})


def _fresh(model, row_id):
    db.session.expire_all()
    return db.session.get(model, row_id)


def _exists(job_id):
    with db.engine.connect() as conn:
        return conn.execute(text('SELECT status FROM processing_job WHERE id = :id'),
                            {'id': job_id}).first()


def _claim(job_id, job_types, queue='transcription'):
    for _ in range(50):
        claimed = job_queue._claim_next_job(job_types, queue)
        if claimed is None or claimed.id == job_id:
            return claimed
    return None


def _reprocess_snapshot(recording, kind='transcription'):
    """Queue a reprocess as reprocessing.py does: snapshot, clear, enqueue."""
    state = job_cancel.snapshot_for_reprocess(recording, kind)
    if kind == 'transcription':
        recording.transcription = None
    recording.summary = None
    Event.query.filter_by(recording_id=recording.id).delete()
    db.session.commit()
    job_type = 'reprocess_transcription' if kind == 'transcription' else 'reprocess_summary'
    return job_queue.enqueue(user_id=recording.user_id, recording_id=recording.id, job_type=job_type,
                             params={'user_id': recording.user_id}, restore_state=state)


def _completed_recording(user=None):
    recording = _recording(user, status='COMPLETED', transcription=TRANSCRIPT, summary=SUMMARY,
                           resolved_hotwords='Speakr', transcription_requested_language='en')
    db.session.add(Event(recording_id=recording.id, title='Launch review',
                         start_datetime=datetime(2026, 12, 1, 9, 30), attendees='["Ada"]'))
    db.session.commit()
    return recording


# --- a waiting job --------------------------------------------------------------

def test_a_waiting_first_transcription_is_removed_and_the_recording_is_canceled(notices):
    recording = _recording()
    job = _job(recording, is_new_upload=True)
    response = _cancel(recording.user_id, job.id)
    assert response.status_code == 200
    assert response.get_json()['canceled'] is True
    assert response.get_json()['cancel_requested'] is False
    assert _exists(job.id) is None

    recording = _fresh(Recording, recording.id)
    assert recording.status == 'FAILED'
    assert recording.error_message == CANCELED_MESSAGE
    assert is_transcription_error(recording.transcription)
    card = json.loads(recording.transcription[len('ERROR_JSON:'):])
    assert card['t'] == 'Processing canceled' and 'Reprocess' in card['g']
    # The audio is kept and the status is one the reprocess route accepts.
    assert recording.audio_path == 'local://m.wav'
    from src.services.reprocessing import IN_PROGRESS
    assert recording.status not in IN_PROGRESS
    _assert_no_notice(notices)


def test_a_waiting_reprocess_puts_the_recording_back(notices):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording)
    recording = _fresh(Recording, recording.id)
    assert (recording.transcription, recording.summary, recording.status) == (None, None, 'QUEUED')
    assert Event.query.filter_by(recording_id=recording.id).count() == 0

    assert _cancel(recording.user_id, job_id).status_code == 200
    recording = _fresh(Recording, recording.id)
    assert recording.status == 'COMPLETED'
    assert recording.transcription == TRANSCRIPT
    assert recording.summary == SUMMARY
    assert recording.resolved_hotwords == 'Speakr'
    assert recording.error_message is None
    events = Event.query.filter_by(recording_id=recording.id).all()
    assert [(e.title, e.start_datetime, e.attendees) for e in events] == [
        ('Launch review', datetime(2026, 12, 1, 9, 30), '["Ada"]')]
    assert _exists(job_id) is None
    _assert_no_notice(notices)


def test_the_reprocess_service_stores_what_it_clears():
    from src.services.reprocessing import queue_summary_reprocess, queue_transcription_reprocess
    user = _user()
    recording = _completed_recording(user)
    storage = MagicMock()
    storage.exists.return_value = True
    with patch('src.services.storage.get_storage_service', return_value=storage):
        job_id = queue_transcription_reprocess(recording, user)
    state = json.loads(db.session.get(ProcessingJob, job_id).restore_state)
    assert state['kind'] == 'transcription'
    assert (state['transcription'], state['summary'], state['status']) == (TRANSCRIPT, SUMMARY, 'COMPLETED')
    assert [e['title'] for e in state['events']] == ['Launch review']
    assert _cancel(user.id, job_id).status_code == 200
    assert _fresh(Recording, recording.id).transcription == TRANSCRIPT

    other = _completed_recording(user)
    with patch('src.services.llm.client', MagicMock()):
        job_id = queue_summary_reprocess(other, user)
    assert _fresh(Recording, other.id).summary is None
    assert json.loads(db.session.get(ProcessingJob, job_id).restore_state)['kind'] == 'summary'
    assert _cancel(user.id, job_id).status_code == 200
    other = _fresh(Recording, other.id)
    assert (other.summary, other.status, other.transcription) == (SUMMARY, 'COMPLETED', TRANSCRIPT)
    assert Event.query.filter_by(recording_id=other.id).count() == 1


def test_a_canceled_reprocess_of_a_failed_recording_is_failed_as_before():
    recording = _recording(status='FAILED', transcription='Transcription failed: service away',
                           error_message='service away')
    job_id = _reprocess_snapshot(recording)
    assert _cancel(recording.user_id, job_id).status_code == 200
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.error_message) == ('FAILED', 'service away')
    assert recording.transcription == 'Transcription failed: service away'


def test_a_waiting_job_with_a_saved_asr_job_cancels_it_on_the_service(monkeypatch):
    import src.services.transcription.connectors.asr_endpoint as asr
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append(job_id) or True)
    recording = _recording()
    job = _job(recording, provider_task=json.dumps(
        {'provider': 'asr_endpoint', 'audio_sha256': 'x', 'task': {'job_id': 'job0042', 'base_url': BASE}}))
    assert _cancel(recording.user_id, job.id).status_code == 200
    assert cancelled == ['job0042']


def test_a_running_job_with_a_saved_asr_job_cancels_it_on_the_service_at_once(monkeypatch):
    import src.services.transcription.connectors.asr_endpoint as asr
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append(job_id) or True)
    recording = _recording(status='PROCESSING')
    job = _job(recording, status='processing', provider_task=json.dumps(
        {'provider': 'asr_endpoint', 'audio_sha256': 'x', 'task': {'job_id': 'job0043', 'base_url': BASE}}))
    assert _cancel(recording.user_id, job.id).status_code == 202
    assert cancelled == ['job0043']
    # The saved task stays until the worker has stopped, which removes the row.
    assert _fresh(ProcessingJob, job.id).provider_task is not None


def test_dependent_jobs_are_canceled_with_the_transcription():
    recording = _recording(status='PROCESSING')
    transcribe = _job(recording, status='queued')
    summary = _job(recording, job_type='summarize', status='queued')
    translation = _job(recording, job_type='translate_transcription', status='processing')
    index = _job(recording, job_type='index_chunks', status='queued')
    other = _job(_recording(db.session.get(User, recording.user_id)), job_type='summarize', status='queued')

    assert _cancel(recording.user_id, transcribe.id).status_code == 200
    assert _exists(transcribe.id) is None
    assert _exists(summary.id) is None                        # waited: removed
    translation = _fresh(ProcessingJob, translation.id)
    assert translation.status == 'processing' and translation.cancel_requested_at is not None
    assert _exists(index.id) is not None                      # not a dependent
    assert _exists(other.id) is not None                      # another recording


def test_a_canceled_summary_cancels_nothing_else():
    recording = _recording(status='SUMMARIZING', transcription=TRANSCRIPT)
    summary = _job(recording, job_type='summarize', status='queued')
    translation = _job(recording, job_type='translate_transcription', status='queued')
    assert _cancel(recording.user_id, summary.id).status_code == 200
    assert _exists(translation.id) is not None
    assert _fresh(Recording, recording.id).status == 'COMPLETED'


def test_a_canceled_translation_leaves_the_recording_alone():
    recording = _recording(status='COMPLETED', transcription=TRANSCRIPT, summary=SUMMARY)
    translation = _job(recording, job_type='translate_transcription', status='queued')
    assert _cancel(recording.user_id, translation.id).status_code == 200
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.transcription, recording.summary) == ('COMPLETED', TRANSCRIPT, SUMMARY)


@pytest.mark.parametrize('job_type', ['stitch', 'merge'])
def test_assembly_jobs_are_not_canceled(job_type):
    recording = _recording()
    job = _job(recording, job_type=job_type, status='queued')
    response = _cancel(recording.user_id, job.id)
    assert response.status_code == 409
    assert response.get_json()['code'] == 'job_not_cancelable'
    assert _exists(job.id) is not None
    status = _client(recording.user_id).get('/api/recordings/job-queue-status').get_json()
    assert next(j for j in status['jobs'] if j['id'] == job.id)['can_cancel'] is False


# --- who may cancel -------------------------------------------------------------

def test_another_user_gets_403_and_a_missing_job_404():
    recording = _recording()
    job = _job(recording, status='processing')
    stranger = _user()
    assert _cancel(stranger.id, job.id).status_code == 403
    assert _fresh(ProcessingJob, job.id).cancel_requested_at is None
    assert _cancel(recording.user_id, 987654321).status_code == 404


def test_an_administrator_may_cancel_an_active_job_but_not_remove_a_finished_one():
    recording = _recording()
    running = _job(recording, status='processing')
    done = _job(_recording(db.session.get(User, recording.user_id), status='COMPLETED'),
                status='completed', completed_at=datetime.utcnow())
    admin = _user(admin=True)
    admin_id = admin.id
    try:
        assert _cancel(admin_id, running.id).status_code == 202
        assert _fresh(ProcessingJob, running.id).cancel_requested_at is not None
        assert _cancel(admin_id, done.id).status_code == 403
        assert _exists(done.id) is not None
    finally:
        # SHARED-DB: other files count the administrators.
        db.session.rollback()
        db.session.delete(db.session.get(User, admin_id))
        db.session.commit()


# --- a running job: the request -------------------------------------------------

def test_a_running_job_is_marked_and_stays_listed_until_its_worker_stops():
    recording = _recording(status='PROCESSING')
    job = _job(recording, status='processing')
    response = _cancel(recording.user_id, job.id)
    assert response.status_code == 202
    body = response.get_json()
    assert body['cancel_requested'] is True and body['canceled'] is False
    job = _fresh(ProcessingJob, job.id)
    assert job.status == 'processing' and job.cancel_requested_at is not None
    status = _client(recording.user_id).get('/api/recordings/job-queue-status').get_json()
    mine = next(j for j in status['jobs'] if j['id'] == job.id)
    assert mine['cancel_requested'] is True and mine['job_status'] == 'processing'
    # Nothing was changed on the recording yet.
    assert _fresh(Recording, recording.id).status == 'PROCESSING'


def test_a_second_cancel_gives_the_same_answer_and_keeps_the_first_time():
    recording = _recording(status='PROCESSING')
    job = _job(recording, status='processing')
    assert _cancel(recording.user_id, job.id).status_code == 202
    first = _fresh(ProcessingJob, job.id).cancel_requested_at
    time.sleep(0.01)
    assert _cancel(recording.user_id, job.id).status_code == 202
    assert _fresh(ProcessingJob, job.id).cancel_requested_at == first

    waiting = _job(_recording(db.session.get(User, recording.user_id)), status='queued')
    assert _cancel(recording.user_id, waiting.id).status_code == 200
    assert _cancel(recording.user_id, waiting.id).status_code == 404


def test_a_job_that_ends_while_the_cancel_is_on_its_way_is_not_canceled():
    recording = _recording(status='COMPLETED', transcription=TRANSCRIPT)
    job = _job(recording, status='completed', completed_at=datetime.utcnow())
    assert job_cancel.cancel_job(job.id) == 'finished'
    assert job_control.request_cancel(job.id) is False
    assert _fresh(ProcessingJob, job.id).cancel_requested_at is None

    # The route read the job as running, and it ended before the mark.
    running = _job(_recording(db.session.get(User, recording.user_id), status='PROCESSING'), status='processing')
    with patch('src.services.job_cancel.cancel_job', return_value='finished'):
        response = _cancel(recording.user_id, running.id)
    assert response.status_code == 409
    assert response.get_json()['code'] == 'job_already_finished'
    assert _exists(running.id) is not None


def test_a_job_claimed_between_the_two_statements_is_marked():
    # The delete of a waiting job finds none (a worker claimed it): the mark follows.
    recording = _recording()
    job = _job(recording, status='queued')
    real_get = db.session.get
    seen = {'n': 0}

    def get_then_claim(model, ident, *args, **kwargs):
        row = real_get(model, ident, *args, **kwargs)
        if model is ProcessingJob and ident == job.id and seen['n'] == 0:
            seen['n'] += 1
            with db.engine.begin() as conn:
                conn.execute(text("UPDATE processing_job SET status = 'processing' WHERE id = :id"),
                             {'id': job.id})
        return row

    with patch.object(db.session, 'get', side_effect=get_then_claim):
        assert job_cancel.cancel_job(job.id) == 'cancel_requested'
    job = _fresh(ProcessingJob, job.id)
    assert job.status == 'processing' and job.cancel_requested_at is not None


# --- a running job: the worker ----------------------------------------------------

class _Connector:
    """A transcription service that answers at once."""
    PROVIDER_NAME = 'fake'
    supports_diarization = False
    default_diarize = False
    SUPPORTS_TASK_RESUME = False
    specifications = MagicMock(preserve_original_audio=False)
    model = 'fake'

    def __init__(self, on_call=None):
        self.calls = 0
        self.on_call = on_call

    def transcribe(self, request):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        return TranscriptionResponse(text=f"Spoken part {self.calls}.", provider='fake', model='fake')


def _run_transcription(job, connector, tmp_path, *, chunks=0, converted=None, on_usage=None):
    """Run a claimed transcription job through the queue with the real task code."""
    audio = tmp_path / 'm.wav'
    audio.write_bytes(b'audio bytes')
    recording_id = job.recording_id
    conversion = MagicMock(was_converted=bool(converted), output_path=converted, mime_type='audio/mpeg',
                           original_codec='wav', final_codec='mp3', original_size_mb=1.0, final_size_mb=0.5)

    def run_audio_job(job_row, recording, params, job_type='transcribe'):
        return proc.transcribe_audio_task(app.app_context(), recording_id, str(audio), 'm.wav', datetime.utcnow())

    chunk_list = []
    for i in range(chunks):
        path = tmp_path / f'chunk_{i}.mp3'
        path.write_bytes(b'chunk %d' % i)
        chunk_list.append({'index': i, 'path': str(path), 'filename': path.name, 'size_mb': 0.1,
                           'start_time': i * 10.0, 'end_time': (i + 1) * 10.0, 'duration': 10.0})

    with patch.object(FairJobQueue, '_run_audio_job', side_effect=run_audio_job), \
         patch('src.services.transcription.get_connector', return_value=connector), \
         patch.object(proc, 'is_video_file', return_value=False), \
         patch.object(proc, 'convert_if_needed', return_value=conversion), \
         patch.object(proc.llm_config, 'is_configured', return_value=True), \
         patch.object(proc, 'ENABLE_INQUIRE_MODE', False), \
         patch.object(proc, 'ENABLE_AUTO_EXPORT', False), \
         patch.object(proc.time, 'sleep'), \
         patch.object(proc.transcription_tracker, 'check_budget', return_value=(True, 0, None)), \
         patch.object(proc.transcription_tracker, 'record_usage', side_effect=on_usage) as usage, \
         patch.object(proc, 'generate_title_task') as title, \
         patch.object(proc, 'generate_summary_only_task') as summary, \
         patch.object(proc, 'chunking_service') as chunking:
        chunking.needs_chunking.return_value = bool(chunks)
        chunking.get_audio_duration.return_value = 60.0
        chunking.create_chunks.return_value = chunk_list
        chunking.merge_transcriptions.side_effect = lambda results: ' '.join(r['transcription'] for r in results)
        job_queue._process_job(job)
    db.session.expire_all()
    return SimpleNamespaceLike(title=title, summary=summary, usage=usage)


class SimpleNamespaceLike:
    def __init__(self, **values):
        self.__dict__.update(values)


def test_a_cancel_before_the_job_starts_runs_nothing(notices, tmp_path):
    recording = _recording()
    job = _job(recording, status='queued', is_new_upload=True)
    claimed = _claim(job.id, ['transcribe'])
    _mark(job.id)
    connector = _Connector()
    _run_transcription(claimed, connector, tmp_path)
    assert connector.calls == 0
    assert _exists(job.id) is None
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.error_message) == ('FAILED', CANCELED_MESSAGE)
    _assert_no_notice(notices)


def test_a_cancel_between_chunks_stops_there_and_stores_nothing(notices, tmp_path):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording)
    claimed = _claim(job_id, ['reprocess_transcription'])
    converted = tmp_path / 'converted.mp3'
    converted.write_bytes(b'converted audio')
    connector = _Connector(on_call=lambda n: _mark(job_id) if n == 2 else None)
    ran = _run_transcription(claimed, connector, tmp_path, chunks=5, converted=str(converted))

    assert connector.calls == 2                      # chunks 3 to 5 were never sent
    assert _exists(job_id) is None
    assert not converted.exists()                    # the derived temp file went
    assert not ran.title.called and not ran.summary.called
    recording = _fresh(Recording, recording.id)
    # No half transcript: the recording is as it was before the reprocess.
    assert (recording.status, recording.transcription, recording.summary) == ('COMPLETED', TRANSCRIPT, SUMMARY)
    assert Event.query.filter_by(recording_id=recording.id).count() == 1
    _assert_no_notice(notices)


def test_a_cancel_during_the_service_call_drops_the_result(notices, tmp_path):
    # A service that cannot be interrupted: the cancel takes effect when it answers.
    recording = _recording()
    job = _job(recording, status='queued', is_new_upload=True)
    claimed = _claim(job.id, ['transcribe'])
    connector = _Connector(on_call=lambda n: _mark(job.id))
    ran = _run_transcription(claimed, connector, tmp_path)
    assert connector.calls == 1
    assert _exists(job.id) is None
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.error_message) == ('FAILED', CANCELED_MESSAGE)
    assert 'Spoken part' not in (recording.transcription or '')
    assert not ran.usage.called and not ran.title.called
    _assert_no_notice(notices)


def test_a_cancel_after_the_transcript_is_stored_keeps_it_and_skips_the_llm(notices, tmp_path):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording)
    claimed = _claim(job_id, ['reprocess_transcription'])
    # The usage is recorded after the transcript is committed.
    ran = _run_transcription(claimed, _Connector(), tmp_path, on_usage=lambda **kwargs: _mark(job_id))
    assert ran.usage.called
    assert not ran.title.called and not ran.summary.called      # before the LLM calls
    assert _exists(job_id) is None
    recording = _fresh(Recording, recording.id)
    assert recording.status == 'COMPLETED'
    assert recording.transcription == 'Spoken part 1.'            # the new, complete transcript
    assert recording.completed_at is not None
    _assert_no_notice(notices)


def test_a_cancel_at_a_whisperx_poll_deletes_the_remote_job(notices, tmp_path, monkeypatch):
    import src.services.transcription.connectors.asr_endpoint as asr
    monkeypatch.setattr(asr, '_sleep', lambda seconds: None)
    monkeypatch.delenv('ASR_ASYNC_JOBS', raising=False)
    monkeypatch.setattr(asr.ASREndpointConnector, 'timeout', 600)

    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording)
    claimed = _claim(job_id, ['reprocess_transcription'])
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)])       # never finishes on its own
    real_handle = fake.handle

    def handle(request):
        response = real_handle(request)
        if request.method == 'GET' and len(fake.calls('GET', '/jobs/job0001')) == 3:
            _mark(job_id)
        return response

    fake.handle = handle
    ran = _run_transcription(claimed, _connector(fake), tmp_path)

    assert len(fake.calls('GET', '/jobs/job0001')) == 3             # no poll after the cancel
    assert fake.deleted == ['job0001']                              # removed from the service
    assert not fake.calls('GET', '/jobs/job0001/result')
    assert _exists(job_id) is None
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.transcription, recording.summary) == ('COMPLETED', TRANSCRIPT, SUMMARY)
    assert not ran.title.called
    _assert_no_notice(notices)


def test_a_chunk_request_canceled_at_its_poll_deletes_its_remote_job(monkeypatch):
    import io
    import src.services.transcription.connectors.asr_endpoint as asr
    monkeypatch.setattr(asr, '_sleep', lambda seconds: None)
    monkeypatch.delenv('ASR_ASYNC_JOBS', raising=False)
    monkeypatch.setattr(asr.ASREndpointConnector, 'timeout', 600)
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)])
    polls = {'n': 0}

    def should_cancel():
        polls['n'] += 1
        return polls['n'] > 3

    request = TranscriptionRequest(audio_file=io.BytesIO(b'chunk bytes'), filename='chunk_0.mp3',
                                   mime_type='audio/mpeg', task_key='speakr-1-chunk0-abc',
                                   should_cancel=should_cancel)
    with pytest.raises(job_control.JobCanceled):
        _connector(fake).transcribe(request)
    assert fake.deleted == ['job0001']


def test_a_summary_canceled_before_the_llm_call_puts_the_summary_back(notices):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording, kind='summary')
    claimed = _claim(job_id, ['reprocess_summary'], queue='summary')
    real_prompt = proc.build_summary_prompt

    def prompt_then_cancel(*args, **kwargs):
        _mark(job_id)                                # while the prompt is built
        return real_prompt(*args, **kwargs)

    with patch.object(proc.llm_config, 'is_configured', return_value=True), \
         patch.object(proc, 'build_summary_prompt', side_effect=prompt_then_cancel), \
         patch.object(proc, 'call_llm_completion') as llm:
        job_queue._process_job(claimed)
    assert not llm.called
    assert _exists(job_id) is None
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.summary, recording.transcription) == ('COMPLETED', SUMMARY, TRANSCRIPT)
    assert Event.query.filter_by(recording_id=recording.id).count() == 1
    _assert_no_notice(notices)


def test_a_summary_canceled_during_the_llm_call_drops_the_reply(notices):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording, kind='summary')
    claimed = _claim(job_id, ['reprocess_summary'], queue='summary')

    def llm(**kwargs):
        _mark(job_id)
        reply = MagicMock()
        reply.choices[0].message.content = 'A summary nobody asked for any more.'
        reply.choices[0].finish_reason = 'stop'
        return reply

    with patch.object(proc.llm_config, 'is_configured', return_value=True), \
         patch.object(proc, 'call_llm_completion', side_effect=llm) as call:
        job_queue._process_job(claimed)
    assert call.called
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.summary) == ('COMPLETED', SUMMARY)
    assert _exists(job_id) is None
    _assert_no_notice(notices)


def test_a_translation_canceled_between_chunks_stores_nothing(notices, monkeypatch):
    from src.services import transcription_translation as tt
    monkeypatch.setattr(job_control, 'PROGRESS_WRITE_INTERVAL', 0)
    segments = json.dumps([{'speaker': 'A', 'sentence': f'Sentence number {i}.', 'start_time': i,
                            'end_time': i + 1} for i in range(60)])
    recording = _recording(status='COMPLETED', transcription=segments)
    job_id = job_queue.enqueue(user_id=recording.user_id, recording_id=recording.id,
                               job_type='translate_transcription',
                               params={'user_id': recording.user_id, 'target_language': 'Italian'})
    claimed = _claim(job_id, ['translate_transcription'], queue='summary')
    calls = []

    def llm(messages, user_id):
        calls.append(1)
        _mark(job_id)
        content = messages[-1]['content']
        payload = json.loads(content[content.find('{'):content.rfind('}') + 1])
        return json.dumps({k: v + ' [it]' for k, v in payload.items()})

    monkeypatch.setattr(tt, '_call_llm', llm)
    job_queue._process_job(claimed)
    assert len(calls) == 1                           # 3 chunks; the cancel landed after the first
    assert _exists(job_id) is None
    recording = _fresh(Recording, recording.id)
    assert recording.status == 'COMPLETED'
    assert not recording.transcription_translation
    _assert_no_notice(notices)


def test_a_cancel_after_the_last_safe_point_comes_too_late(notices):
    recording = _recording(status='SUMMARIZING', transcription=TRANSCRIPT)
    job = _job(recording, job_type='summarize', status='queued', restore_state='{"kind": "summary", "v": 1}')
    claimed = _claim(job.id, ['summarize'], queue='summary')

    def finish_then_cancel(job_row, rec, params, job_type=None):
        rec.summary = 'A finished summary.'
        rec.status = 'COMPLETED'
        db.session.commit()
        _mark(job.id)

    with patch.object(FairJobQueue, '_run_summarization', side_effect=finish_then_cancel):
        job_queue._process_job(claimed)
    job = _fresh(ProcessingJob, job.id)
    assert job.status == 'completed'
    assert job.cancel_requested_at is None           # the mark is taken back
    assert job.restore_state is None                 # and the copy of the old state is dropped
    assert _fresh(Recording, recording.id).summary == 'A finished summary.'
    assert not notices['_emit_failure_webhook'].called


def test_a_job_that_fails_while_a_cancel_waits_reports_no_failure(notices):
    recording = _recording()
    job = _job(recording, status='queued', is_new_upload=True)
    claimed = _claim(job.id, ['transcribe'])

    def fail_after_cancel(job_row, rec, params, job_type=None):
        _mark(job.id)
        raise RuntimeError('the service went away')

    with patch.object(FairJobQueue, '_run_audio_job', side_effect=fail_after_cancel):
        job_queue._process_job(claimed)
    assert _exists(job.id) is None                   # not re-queued for a retry
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.error_message) == ('FAILED', CANCELED_MESSAGE)
    _assert_no_notice(notices)


def test_a_failed_job_drops_the_copy_of_the_old_state(notices):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording)
    claimed = _claim(job_id, ['reprocess_transcription'])

    class Permanent(Exception):
        permanent = True

    with patch.object(FairJobQueue, '_run_audio_job', side_effect=Permanent('audio_too_long')):
        job_queue._process_job(claimed)
    job = _fresh(ProcessingJob, job_id)
    assert job.status == 'failed' and job.restore_state is None
    assert notices['_emit_failure_webhook'].called  # a failure still reports as before


def test_a_restart_finishes_a_cancel_the_worker_never_reached(notices):
    recording = _completed_recording()
    job_id = _reprocess_snapshot(recording)
    _claim(job_id, ['reprocess_transcription'])
    _mark(job_id)
    bystander = _job(_recording(db.session.get(User, recording.user_id), status='PROCESSING'),
                     status='processing')

    job_queue.recover_orphaned_jobs()
    assert _exists(job_id) is None
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.transcription) == ('COMPLETED', TRANSCRIPT)
    assert _fresh(ProcessingJob, bystander.id).status == 'queued'   # put back as before
    _assert_no_notice(notices)


def test_a_canceled_conversion_queues_no_transcription(notices):
    recording = _recording()
    job = _job(recording, job_type='convert', status='queued', is_new_upload=True)
    claimed = _claim(job.id, ['convert'], queue='convert')

    def convert(recording_id, params):
        _mark(job.id)                                 # while ffmpeg runs
        job_control.current().check_cancel()          # the safe point before the enqueue
        raise AssertionError('the transcription was queued after a cancel')

    with patch('src.services.upload_conversion.convert_uploaded_recording', side_effect=convert):
        job_queue._process_job(claimed)
    assert _exists(job.id) is None
    assert ProcessingJob.query.filter_by(recording_id=recording.id).count() == 0
    recording = _fresh(Recording, recording.id)
    assert (recording.status, recording.error_message) == ('FAILED', CANCELED_MESSAGE)
    assert recording.audio_path == 'local://m.wav'
    _assert_no_notice(notices)


def test_the_conversion_checks_for_a_cancel_before_it_queues_the_transcription():
    import inspect
    from src.services import upload_conversion
    source = inspect.getsource(upload_conversion.convert_uploaded_recording)
    assert source.index('check_cancel()') < source.index("job_type='transcribe'")


def test_the_cancel_log_lines_hold_ids_only(caplog, tmp_path):
    import logging
    recording = _recording(title='Zanzibar Lighthouse Budget')
    recording.original_filename = 'heliotrope-marmalade.wav'
    db.session.commit()
    waiting = _job(recording, status='queued')
    with caplog.at_level(logging.DEBUG):
        assert _cancel(recording.user_id, waiting.id).status_code == 200
        other = _recording(db.session.get(User, recording.user_id), title='Zanzibar Lighthouse Budget')
        running = _job(other, status='queued')
        claimed = _claim(running.id, ['transcribe'])
        assert _cancel(other.user_id, running.id).status_code == 202
        _run_transcription(claimed, _Connector(), tmp_path)
    assert _exists(running.id) is None
    text_out = '\n'.join(r.getMessage() for r in caplog.records if 'cancel' in r.getMessage().lower())
    assert f"Job {waiting.id} canceled" in text_out and f"Job {running.id} canceled" in text_out
    assert 'Zanzibar' not in text_out and 'heliotrope' not in text_out
