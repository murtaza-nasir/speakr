"""Real progress, stage and time fields of the processing queue.

A number is stored for a job only where one exists: the chunks of a chunked
transcription or of a translation, and a fraction the WhisperX ASR service
reports as measured. A fraction the service marks as estimated is not
stored, and a summary job has no number at all. The queue status answer
carries the number, a stage code and timestamps with a UTC marker.
"""

import io
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, User
from src.services import job_control
from src.services.transcription.base import TranscriptionRequest, TranscriptionResponse
from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector, stage_word
import src.tasks.processing as proc

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


def _user():
    user = User(username=f"qp_{uuid.uuid4().hex[:10]}", email=f"{uuid.uuid4().hex[:10]}@example.com",
                password='x')
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user=None, **fields):
    user = user or _user()
    recording = Recording(user_id=user.id, title='Planning call', status=fields.pop('status', 'PROCESSING'),
                          **fields)
    db.session.add(recording)
    db.session.commit()
    return recording


def _job(recording, job_type='transcribe', status='processing', **fields):
    job = ProcessingJob(user_id=recording.user_id, recording_id=recording.id, job_type=job_type,
                        status=status, **fields)
    if status == 'processing' and 'started_at' not in fields:
        job.started_at = datetime.utcnow()
    db.session.add(job)
    db.session.commit()
    return job


def _row(job_id):
    # Read on a separate connection, as the queue poll would.
    with db.engine.connect() as conn:
        return tuple(conn.execute(
            text('SELECT progress_done, progress_total, progress_kind, stage, stage_detail '
                 'FROM processing_job WHERE id = :id'), {'id': job_id}).one())


def _status(user_id):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    response = client.get('/api/recordings/job-queue-status')
    assert response.status_code == 200
    return response.get_json()


def _mine(user_id, job_id):
    return next(j for j in _status(user_id)['jobs'] if j['id'] == job_id)


@pytest.fixture
def ctx():
    with app.app_context():
        yield
        db.session.rollback()


@pytest.fixture
def no_throttle(monkeypatch):
    monkeypatch.setattr(job_control, 'PROGRESS_WRITE_INTERVAL', 0)


class Clock:
    def __init__(self):
        self.t = 500.0

    def now(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(job_control, '_now', c.now)
    return c


# --- the WhisperX ASR service ---------------------------------------------------

def _poll(connector_state, request, status, stage, fraction, estimated):
    ASREndpointConnector._note_progress(
        'job0001', {'status': status, 'progress': {'stage': stage, 'fraction': fraction,
                                                   'fraction_estimated': estimated}},
        request, connector_state)


def _whisperx_request(control):
    return TranscriptionRequest(
        audio_file=io.BytesIO(b'audio'), filename='m.wav',
        report_progress=lambda d, t: control.report(d, t, job_control.KIND_FRACTION),
        report_stage=control.detail)


def test_a_measured_whisperx_fraction_is_stored_and_returned(ctx, no_throttle):
    recording = _recording()
    job = _job(recording)
    control = job_control.JobControl(job.id)
    state = {}
    request = _whisperx_request(control)
    _poll(state, request, 'running', 'whisper', 0.42, False)
    assert _row(job.id) == (42, 100, 'fraction', None, 'transcribing')

    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] == {'fraction': 0.42}
    assert mine['progress_kind'] == 'fraction'
    assert mine['stage'] == 'transcribing'
    assert mine['stage_detail'] == 'transcribing'
    # The fields of earlier releases are still sent.
    assert (mine['progress_done'], mine['progress_total']) == (42, 100)

    _poll(state, request, 'running', 'align', 0.8, False)
    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] == {'fraction': 0.8}
    assert mine['stage_detail'] == 'aligning'


def test_an_estimated_whisperx_fraction_is_not_stored(ctx, no_throttle):
    recording = _recording()
    job = _job(recording)
    control = job_control.JobControl(job.id)
    state = {}
    request = _whisperx_request(control)
    _poll(state, request, 'running', 'whisper', 0.3, True)
    # The stage is known, the number is not.
    assert _row(job.id) == (None, None, None, None, 'transcribing')
    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] is None and mine['progress_kind'] is None
    assert mine['stage_detail'] == 'transcribing'

    # A measured fraction followed by an estimate: the number is taken back.
    _poll(state, request, 'running', 'diarize', 0.5, False)
    assert _mine(recording.user_id, job.id)['progress'] == {'fraction': 0.5}
    _poll(state, request, 'running', 'diarize', 0.6, True)
    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] is None
    assert mine['stage_detail'] == 'diarizing'


def test_the_service_stage_names_in_speakr_words():
    assert stage_word('queued', None) == 'waiting'
    assert stage_word('running', 'whisper') == 'transcribing'
    assert stage_word('running', 'transcription') == 'transcribing'
    assert stage_word('running', 'align') == 'aligning'
    assert stage_word('running', 'diarization') == 'diarizing'
    assert stage_word('running', 'load_audio') == 'preparing'
    assert stage_word('running', 'something_new') is None
    assert stage_word('running', None) is None


# --- chunked transcription --------------------------------------------------------

class _ChunkConnector:
    PROVIDER_NAME = 'fake'
    supports_diarization = False
    specifications = MagicMock()

    def __init__(self, on_chunk=None):
        self.calls = 0
        self.on_chunk = on_chunk

    def transcribe(self, request):
        self.calls += 1
        if self.on_chunk:
            self.on_chunk(self.calls)
        return TranscriptionResponse(text=f"part {self.calls}", provider='fake', model='fake')


def _chunks(tmp_path, count):
    chunks = []
    for i in range(count):
        path = tmp_path / f"chunk_{i}.mp3"
        path.write_bytes(b'chunk %d' % i)
        chunks.append({'index': i, 'path': str(path), 'filename': path.name, 'size_mb': 0.1,
                       'start_time': i * 10.0, 'end_time': (i + 1) * 10.0, 'duration': 10.0})
    return chunks


def _run_chunks(tmp_path, connector, count=3):
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b'long audio')
    with patch.object(proc, 'chunking_service') as chunking, patch.object(proc.time, 'sleep'):
        chunking.create_chunks.return_value = _chunks(tmp_path, count)
        chunking.merge_transcriptions.side_effect = lambda results: ' '.join(r['transcription'] for r in results)
        return proc.transcribe_chunks_with_connector(connector, str(audio), 'long.mp3', 'audio/mpeg', 'en')


def test_chunks_finished_of_total_are_stored_and_returned(ctx, no_throttle, tmp_path):
    recording = _recording()
    job = _job(recording)
    seen = []
    connector = _ChunkConnector(on_chunk=lambda n: seen.append(_row(job.id)[:3]))
    with job_control.running(job_control.JobControl(job.id)):
        text_out = _run_chunks(tmp_path, connector)
    assert text_out == 'part 1 part 2 part 3'
    # Read as each chunk was sent: the chunks finished before it.
    assert seen == [(0, 3, 'chunks'), (1, 3, 'chunks'), (2, 3, 'chunks')]
    assert _row(job.id)[:3] == (3, 3, 'chunks')

    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] == {'done': 3, 'total': 3}
    assert mine['progress_kind'] == 'chunks'


def test_chunks_outside_a_job_store_nothing(ctx, tmp_path):
    # Incognito mode and the voice check run without a processing job.
    assert job_control.current().job_id is None
    recording = _recording()
    job = _job(recording)
    assert _run_chunks(tmp_path, _ChunkConnector()) == 'part 1 part 2 part 3'
    assert _row(job.id) == (None, None, None, None, None)


# --- jobs without a number --------------------------------------------------------

def test_a_summary_job_has_no_progress(ctx):
    recording = _recording(status='SUMMARIZING', transcription='A transcript long enough to summarize.')
    job = _job(recording, job_type='summarize')
    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] is None
    assert mine['progress_kind'] is None
    assert mine['stage'] == 'summarizing'
    assert mine['started_at'] is not None


def test_a_translation_row_written_before_the_unit_column_counts_chunks(ctx):
    recording = _recording(status='COMPLETED')
    job = _job(recording, job_type='translate_transcription', progress_done=4, progress_total=18)
    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] == {'done': 4, 'total': 18}
    assert mine['progress_kind'] == 'chunks'
    assert mine['stage'] == 'translating'


def test_a_finished_job_reports_no_progress(ctx):
    recording = _recording(status='COMPLETED')
    job = _job(recording, status='completed', progress_done=3, progress_total=3, progress_kind='chunks',
               started_at=datetime.utcnow(), completed_at=datetime.utcnow())
    mine = _mine(recording.user_id, job.id)
    assert mine['progress'] is None and mine['progress_kind'] is None
    assert mine['stage'] == 'completed'


# --- throttled writes -------------------------------------------------------------

def test_progress_writes_are_spaced(ctx, clock):
    recording = _recording()
    job = _job(recording)
    control = job_control.JobControl(job.id)
    writes = []
    real_update = control._update
    control._update = lambda **values: (writes.append(values['progress_done']), real_update(**values))[1]

    control.report(1, 100, job_control.KIND_FRACTION)       # the first value is written at once
    for done in range(2, 40):                               # a poll every 50 ms
        clock.t += 0.05
        control.report(done, 100, job_control.KIND_FRACTION)
    assert writes == [1]
    assert _row(job.id)[:2] == (1, 100)

    clock.t += job_control.PROGRESS_WRITE_INTERVAL          # the interval has passed
    control.report(40, 100, job_control.KIND_FRACTION)
    assert writes == [1, 40]
    assert _row(job.id)[:3] == (40, 100, 'fraction')

    # The same value again is not a write, however much time has passed.
    clock.t += 60
    control.report(40, 100, job_control.KIND_FRACTION)
    assert writes == [1, 40]


def test_a_stage_detail_shares_the_throttle(ctx, clock):
    recording = _recording()
    job = _job(recording)
    control = job_control.JobControl(job.id)
    control.report(10, 100, job_control.KIND_FRACTION)
    clock.t += 0.5
    control.detail('aligning')
    assert _row(job.id) == (10, 100, 'fraction', None, None)
    clock.t += job_control.PROGRESS_WRITE_INTERVAL
    control.report(20, 100, job_control.KIND_FRACTION)
    assert _row(job.id) == (20, 100, 'fraction', None, 'aligning')


def test_progress_is_written_only_to_a_running_job(ctx, no_throttle):
    recording = _recording()
    job = _job(recording, status='completed')
    job_control.JobControl(job.id).report(1, 2)
    assert _row(job.id)[:2] == (None, None)


def test_a_failed_write_never_fails_the_job(ctx, no_throttle, monkeypatch):
    recording = _recording()
    job = _job(recording)
    control = job_control.JobControl(job.id)

    def broken(self, *args, **kwargs):
        raise RuntimeError('database is away')

    engine_type = type(db.engine)
    monkeypatch.setattr(engine_type, 'begin', broken)
    monkeypatch.setattr(engine_type, 'connect', broken)
    control.report(1, 2)            # logged and ignored
    control.stage('titling')
    assert control.cancel_requested() is False
    control.check_cancel()


def test_the_pipeline_step_is_written_at_once_and_clears_the_number(ctx, clock):
    recording = _recording()
    job = _job(recording)
    control = job_control.JobControl(job.id)
    control.report(2, 5)
    clock.t += 0.1
    control.stage('titling')
    assert _row(job.id) == (None, None, None, 'titling', None)
    mine = _mine(recording.user_id, job.id)
    assert mine['stage'] == 'titling' and mine['progress'] is None


def test_a_claim_starts_from_a_clean_row(ctx):
    from src.services.job_queue import job_queue
    recording = _recording(status='QUEUED')
    job = _job(recording, status='queued', progress_done=7, progress_total=9, progress_kind='chunks',
               stage='titling', stage_detail='aligning')
    claimed = None
    for _ in range(50):
        claimed = job_queue._claim_next_job(['transcribe'], 'transcription')
        if claimed is None or claimed.id == job.id:
            break
    assert claimed is not None and claimed.id == job.id
    assert _row(job.id) == (None, None, None, None, None)


# --- stage codes and times --------------------------------------------------------

@pytest.mark.parametrize('job_type, status, expected', [
    ('convert', 'queued', 'queued'),
    ('convert', 'processing', 'converting'),
    ('stitch', 'processing', 'assembling'),
    ('merge', 'processing', 'joining'),
    ('transcribe', 'processing', 'transcribing'),
    ('reprocess_transcription', 'processing', 'transcribing'),
    ('summarize', 'processing', 'summarizing'),
    ('reprocess_summary', 'processing', 'summarizing'),
    ('translate_transcription', 'processing', 'translating'),
    ('index_chunks', 'processing', 'indexing'),
    ('transcribe', 'completed', 'completed'),
    ('transcribe', 'failed', 'failed'),
])
def test_stage_codes(job_type, status, expected):
    assert ProcessingJob(job_type=job_type, status=status).job_stage() == expected


def test_a_waiting_job_names_its_queue_and_place(ctx):
    user = _user()
    first = _job(_recording(user, status='QUEUED'), status='queued')
    second = _job(_recording(user, status='QUEUED'), status='queued',
                  created_at=datetime.utcnow() + timedelta(seconds=5))
    jobs = {j['id']: j for j in _status(user.id)['jobs']}
    assert jobs[first.id]['stage'] == 'queued'
    assert jobs[first.id]['queue'] == 'transcription'
    assert jobs[first.id]['queue_position'] == jobs[first.id]['position']
    assert jobs[second.id]['queue_position'] == jobs[first.id]['queue_position'] + 1
    assert jobs[first.id]['started_at'] is None and jobs[first.id]['finished_at'] is None
    assert jobs[first.id]['can_cancel'] is True


def test_timestamps_carry_a_utc_marker(ctx):
    started = datetime(2026, 11, 15, 9, 30, 5)
    recording = _recording(status='COMPLETED')
    job = _job(recording, status='completed', created_at=started - timedelta(seconds=20),
               started_at=started, completed_at=datetime.utcnow())
    answer = _status(recording.user_id)
    mine = next(j for j in answer['jobs'] if j['id'] == job.id)
    assert mine['started_at'] == '2026-11-15T09:30:05Z'
    assert mine['created_at'] == '2026-11-15T09:29:45Z'
    for key in ('created_at', 'started_at', 'completed_at', 'finished_at'):
        parsed = datetime.fromisoformat(mine[key].replace('Z', '+00:00'))
        assert parsed.utcoffset() == timedelta(0)
    assert mine['finished_at'] == mine['completed_at']
    assert datetime.fromisoformat(mine['started_at'].replace('Z', '+00:00')) == started.replace(tzinfo=timezone.utc)
    server_time = datetime.fromisoformat(answer['server_time'].replace('Z', '+00:00'))
    assert abs((server_time - datetime.now(timezone.utc)).total_seconds()) < 60


def test_a_running_job_has_no_finish_time(ctx):
    recording = _recording()
    job = _job(recording)
    mine = _mine(recording.user_id, job.id)
    assert mine['finished_at'] is None and mine['completed_at'] is None
    assert mine['started_at'].endswith('Z')
    assert mine['queue'] is None and mine['queue_position'] is None
    assert mine['cancel_requested'] is False


def test_the_job_dict_keeps_the_naive_convention(ctx):
    recording = _recording()
    job = _job(recording, progress_done=1, progress_total=4, progress_kind='chunks')
    data = job.to_dict()
    assert not data['created_at'].endswith('Z')
    assert data['progress'] == {'done': 1, 'total': 4}
    assert data['stage'] == 'transcribing'
