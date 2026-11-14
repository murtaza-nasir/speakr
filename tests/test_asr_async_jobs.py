"""The asr_endpoint connector and the WhisperX ASR service's asynchronous jobs.

A service that advertises async_jobs (jobs_api_version 1) in /health gets
POST /jobs, is polled at its poll_after_seconds and has its result fetched
and then deleted. Every other service, and every service with the
asr_async_jobs setting off, gets the synchronous /asr request. The remote job
id is saved on the processing job, so a retry or a restart polls the same
job instead of submitting the audio again.

The service is a fake behind httpx.MockTransport; nothing leaves the process.
"""

import io
import json
import os
import re
import sys
import time
import uuid
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, SystemSetting, User
import src.services.transcription.connectors.asr_endpoint as asr
from src.services.error_classification import is_permanent_error
from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector, RemoteJobFailed
from src.services.transcription.exceptions import ProviderError, TranscriptionError
from src.services.transcription.provider_tasks import cancel_remote_tasks, provider_task_context
from src.utils.system_settings import ENV_BACKED_SETTINGS
import src.tasks.processing as proc

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

REAL_CLIENT = httpx.Client
BASE = 'http://asr.test:9000'
RESULT = {
    'text': [],
    'language': 'en',
    'segments': [
        {'start': 0.0, 'end': 1.5, 'text': ' Hello there.', 'speaker': 'SPEAKER_00'},
        {'start': 1.5, 'end': 3.0, 'text': ' Good morning.', 'speaker': 'SPEAKER_01'},
    ],
}
SYNC_RESULT = {'language': 'en', 'segments': [{'start': 0.0, 'end': 1.0, 'text': 'sync', 'speaker': 'SPEAKER_00'}]}

CAPABILITIES = {'async_jobs': True, 'jobs_api_version': 1, 'dedupe': True, 'idempotency_key': True,
                'progress': True, 'cancel': True, 'list_jobs': False, 'job_result_ttl_seconds': 86400,
                'max_async_jobs': 16}

RUNNING = [
    ('queued', None, 0.0),
    ('running', 'whisper', 0.2),
    ('running', 'align', 0.7),
    ('done', None, 1.0),
]


class FakeWhisperX:
    """The jobs API of whisperx-asr-service 0.5.0, as far as Speakr uses it."""

    def __init__(self, capabilities=CAPABILITIES, script=RUNNING, poll_after=7):
        self.capabilities = capabilities
        self.script = list(script)
        self.poll_after = poll_after
        self.requests = []
        self.jobs = {}
        self.by_key = {}
        self.deleted = []
        self.submit_answers = []   # (status, headers, body) answered before a job is accepted
        self.poll_answers = []     # (status, headers, body) answered before the job state
        self.drop_submit_response = False
        self._ids = 0

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    def calls(self, method=None, path=None):
        return [r for r in self.requests
                if (method is None or r.method == method) and (path is None or r.url.path == path)]

    def add_job(self, script=None, job_id=None):
        self._ids += 1
        job_id = job_id or f"job{self._ids:04d}"
        self.jobs[job_id] = {'script': list(script if script is not None else self.script), 'state': None,
                             'error': None}
        return job_id

    def _state(self, job_id):
        job = self.jobs[job_id]
        if job['state'] is None or (job['state'][0] not in ('done', 'error', 'cancelled', 'interrupted')
                                    and job['script']):
            step = job['script'].pop(0) if job['script'] else job['state']
            job['state'] = step
        status, stage, fraction = job['state'][:3]
        error = job['state'][3] if len(job['state']) > 3 else None
        return {'job_id': job_id, 'status': status,
                'progress': {'stage': stage, 'fraction': fraction, 'fraction_estimated': status != 'done',
                             'elapsed_seconds': 1.0, 'eta_seconds': None},
                'error': error, 'deduplicated': False, 'submissions': 1,
                'poll_after_seconds': self.poll_after,
                'status_url': f"/jobs/{job_id}", 'result_url': f"/jobs/{job_id}/result"}

    def handle(self, request):
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == 'GET' and path in ('/health', '/'):
            body = {'status': 'healthy'}
            if self.capabilities is not None:
                body['capabilities'] = self.capabilities
            return httpx.Response(200, json=body)
        if method == 'POST' and path == '/asr':
            return httpx.Response(200, json=SYNC_RESULT)
        if method == 'POST' and path == '/jobs':
            if not self.capabilities:
                return httpx.Response(404, json={'error': 'not_found'})
            if self.submit_answers:
                status, headers, body = self.submit_answers.pop(0)
                return httpx.Response(status, headers=headers, json=body)
            params = tuple(sorted(request.url.params.multi_items()))
            key = (request.headers.get('Idempotency-Key'), params)
            if key[0] and key in self.by_key and self.by_key[key] in self.jobs:
                job_id = self.by_key[key]
                body = self._state(job_id)
                body['deduplicated'] = True
                return httpx.Response(200, json=body)
            job_id = self.add_job()
            if key[0]:
                self.by_key[key] = job_id
            if self.drop_submit_response:
                self.drop_submit_response = False
                raise httpx.ReadError("connection dropped", request=request)
            body = {'job_id': job_id, 'status': 'queued', 'deduplicated': False,
                    'poll_after_seconds': self.poll_after}
            return httpx.Response(202, json=body, headers={'Location': f"/jobs/{job_id}"})
        match = re.match(r'^/jobs/([^/]+)(/result)?$', path)
        if match:
            job_id, result = match.group(1), match.group(2)
            if method == 'DELETE':
                self.deleted.append(job_id)
                if job_id not in self.jobs:
                    return httpx.Response(404, json={'error': 'not_found'})
                job = self.jobs.pop(job_id)
                return httpx.Response(200, json={'job_id': job_id, 'status': 'cancelled'})
            if job_id not in self.jobs:
                return httpx.Response(404, json={'error': 'not_found'})
            if not result:
                if self.poll_answers:
                    status, headers, body = self.poll_answers.pop(0)
                    return httpx.Response(status, headers=headers, json=body)
                body = self._state(job_id)
                headers = {'Retry-After': str(self.poll_after)} if body['status'] in ('queued', 'running') else {}
                return httpx.Response(200, json=body, headers=headers)
            state = self.jobs[job_id]['state']
            if state is None or state[0] in ('queued', 'running'):
                return httpx.Response(202, json={'job_id': job_id})
            if state[0] != 'done':
                return httpx.Response(409, json={'job_id': job_id, 'status': state[0]})
            return httpx.Response(200, json=RESULT, headers={'X-Diarization-Status': 'ok'})
        return httpx.Response(404, json={'detail': 'Not Found'})


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(asr, '_now', c.now)
    monkeypatch.setattr(asr, '_sleep', c.sleep)
    monkeypatch.delenv('ASR_ASYNC_JOBS', raising=False)
    monkeypatch.setattr(ASREndpointConnector, 'timeout', 600)
    return c


def _connector(fake):
    connector = ASREndpointConnector({'base_url': BASE, 'timeout': 600})

    def client(**kwargs):
        kwargs.pop('transport', None)
        return REAL_CLIENT(transport=fake.transport, **kwargs)

    connector._http_client = client
    return connector


def _request(saved=None, store=None, task_key='speakr-1-2-abcdef0123456789', progress=None, **kwargs):
    return TranscriptionRequest(audio_file=io.BytesIO(b'audio bytes'), filename='m.wav', mime_type='audio/wav',
                                diarize=True, provider_task=saved,
                                save_provider_task=store.append if store is not None else None,
                                task_key=task_key,
                                report_progress=(lambda d, t: progress.append((d, t))) if progress is not None else None,
                                **kwargs)


# --- capability detection -------------------------------------------------------

def test_a_service_without_capabilities_gets_the_synchronous_request(clock):
    fake = FakeWhisperX(capabilities=None)
    response = _connector(fake).transcribe(_request())
    assert response.segments[0].text == 'sync'
    assert fake.calls('POST', '/asr') and not fake.calls('POST', '/jobs')


@pytest.mark.parametrize('capabilities', [
    {**CAPABILITIES, 'async_jobs': False, 'jobs_api_version': None},
    {**CAPABILITIES, 'jobs_api_version': 2},
    {**CAPABILITIES, 'jobs_api_version': True},
    'yes',
])
def test_capabilities_that_do_not_offer_version_1_get_the_synchronous_request(clock, capabilities):
    fake = FakeWhisperX(capabilities=capabilities)
    assert _connector(fake).transcribe(_request()).segments[0].text == 'sync'
    assert not fake.calls('POST', '/jobs')


def test_capabilities_are_read_from_the_root_when_health_has_no_json(clock):
    fake = FakeWhisperX()
    handle = fake.handle

    def no_health(request):
        if request.url.path == '/health':
            fake.requests.append(request)
            return httpx.Response(502, text='<html>bad gateway</html>')
        return handle(request)

    fake.handle = no_health
    assert _connector(fake).transcribe(_request()).segments[0].text == 'Hello there.'
    assert fake.calls('GET', '/') and fake.calls('POST', '/jobs')


def test_the_setting_off_in_env_keeps_the_synchronous_request(clock, monkeypatch):
    monkeypatch.setenv('ASR_ASYNC_JOBS', 'false')
    fake = FakeWhisperX()
    store = []
    saved = {'job_id': 'job0001', 'base_url': BASE}
    assert _connector(fake).transcribe(_request(saved, store)).segments[0].text == 'sync'
    assert not fake.calls(path='/health') and not fake.calls('POST', '/jobs')
    # A job saved while the jobs API was in use is forgotten.
    assert store == [None]


def test_the_setting_off_in_system_settings_keeps_the_synchronous_request(clock):
    fake = FakeWhisperX()
    with app.app_context():
        row = SystemSetting.query.filter_by(key='asr_async_jobs').first()
        before = (row.value, row.setting_type) if row else None
        SystemSetting.set_setting('asr_async_jobs', 'false', None, 'boolean')
        try:
            assert _connector(fake).transcribe(_request()).segments[0].text == 'sync'
        finally:
            if before is None:
                db.session.delete(SystemSetting.query.filter_by(key='asr_async_jobs').first())
                db.session.commit()
            else:
                SystemSetting.set_setting('asr_async_jobs', before[0], None, before[1])
    assert not fake.calls('POST', '/jobs')


def test_the_setting_is_env_backed_and_on_by_default():
    env, default, setting_type, _ = ENV_BACKED_SETTINGS['asr_async_jobs']
    assert (env, default, setting_type) == ('ASR_ASYNC_JOBS', True, 'boolean')


def test_a_jobs_api_that_answers_404_falls_back_to_the_synchronous_request(clock):
    fake = FakeWhisperX()
    fake.submit_answers.append((404, {}, {'error': 'not_found'}))
    assert _connector(fake).transcribe(_request()).segments[0].text == 'sync'
    assert len(fake.calls('POST', '/jobs')) == 1 and len(fake.calls('POST', '/asr')) == 1


# --- submit, poll, result, delete -----------------------------------------------

def test_submit_poll_result_delete(clock):
    fake = FakeWhisperX()
    store, progress = [], []
    response = _connector(fake).transcribe(_request(store=store, progress=progress, language='en',
                                                    min_speakers=2, max_speakers=4, hotwords='Speakr'))
    assert [s.text for s in response.segments] == ['Hello there.', 'Good morning.']
    assert response.speakers == ['SPEAKER_00', 'SPEAKER_01']

    order = [(r.method, r.url.path) for r in fake.requests]
    assert order == [('GET', '/health'), ('POST', '/jobs')] + [('GET', '/jobs/job0001')] * 4 + [
        ('GET', '/jobs/job0001/result'), ('DELETE', '/jobs/job0001')]

    submit = fake.calls('POST', '/jobs')[0]
    params = dict(submit.url.params)
    assert 'output' not in params and 'encode' not in params
    assert params['language'] == 'en' and params['diarize'] == 'true' and params['min_speakers'] == '2'
    assert params['hotwords'] == 'Speakr'
    assert b'audio bytes' in submit.read()
    assert fake.calls(path='/jobs/job0001/result')[0].url.params['output_format'] == 'json'

    # Saved at once, forgotten once the result is stored and deleted.
    assert store == [{'job_id': 'job0001', 'base_url': BASE}, None]
    assert progress == [(0, 100), (20, 100), (70, 100), (100, 100)]
    assert fake.deleted == ['job0001']


def test_poll_after_seconds_is_followed(clock):
    fake = FakeWhisperX(poll_after=13)
    _connector(fake).transcribe(_request())
    assert clock.sleeps == [13, 13, 13]


@pytest.mark.parametrize('poll_after,expected', [(0.2, 2.0), (900, 60.0), ('7', 7.0)])
def test_poll_after_seconds_is_kept_within_bounds(clock, poll_after, expected):
    fake = FakeWhisperX(poll_after=poll_after)
    _connector(fake).transcribe(_request())
    assert set(clock.sleeps) == {expected}


def test_without_poll_after_seconds_the_retry_after_header_or_the_default_applies():
    response = httpx.Response(200, headers={'Retry-After': '25'})
    assert asr._poll_wait({}, response) == 25.0
    assert asr._poll_wait({}, httpx.Response(200)) == asr.POLL_DEFAULT_SECONDS


def test_a_result_still_pending_is_polled_again(clock):
    fake = FakeWhisperX()
    handle = fake.handle
    pending = {'left': 1}

    def result_pending(request):
        if request.url.path.endswith('/result') and pending['left']:
            pending['left'] -= 1
            fake.requests.append(request)
            return httpx.Response(202, json={}, headers={'Retry-After': '5'})
        return handle(request)

    fake.handle = result_pending
    assert _connector(fake).transcribe(_request()).segments
    assert len(fake.calls(path='/jobs/job0001/result')) == 2


def test_failed_polls_are_tolerated_up_to_a_limit(clock):
    fake = FakeWhisperX()
    fake.poll_answers = [(502, {}, {}), (503, {}, {})]
    assert _connector(fake).transcribe(_request()).segments


def test_too_many_failed_polls_end_the_attempt_and_keep_the_job(clock):
    fake = FakeWhisperX()
    fake.poll_answers = [(502, {}, {})] * (asr.MAX_POLL_FAILURES + 1)
    store = []
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert not getattr(exc.value, 'permanent', False)
    assert not is_permanent_error(str(exc.value))
    assert store == [{'job_id': 'job0001', 'base_url': BASE}]


# --- idempotency and dedupe ---------------------------------------------------------

def test_the_idempotency_key_names_the_job_and_changes_with_the_parameters(clock):
    fake = FakeWhisperX()
    _connector(fake).transcribe(_request())
    _connector(fake).transcribe(_request())
    _connector(fake).transcribe(_request(language='de'))
    keys = [r.headers.get('Idempotency-Key') for r in fake.calls('POST', '/jobs')]
    assert keys[0] == keys[1] != keys[2]
    assert keys[0].startswith('speakr-1-2-abcdef0123456789-')


def test_no_idempotency_key_outside_a_processing_job(clock):
    fake = FakeWhisperX()
    _connector(fake).transcribe(_request(task_key=None))
    assert 'Idempotency-Key' not in fake.calls('POST', '/jobs')[0].headers


def test_a_retry_after_a_dropped_submit_attaches_to_the_running_job(clock):
    fake = FakeWhisperX()
    fake.drop_submit_response = True
    store = []
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert not is_permanent_error(str(exc.value))
    assert store == []
    # The retry of the Speakr job sends the same key and gets the same job.
    response = _connector(fake).transcribe(_request(store=store))
    assert response.segments
    assert list(fake.by_key.values()) == ['job0001'] and fake._ids == 1
    assert store == [{'job_id': 'job0001', 'base_url': BASE}, None]


# --- errors -----------------------------------------------------------------------

def _failing(code, retryable):
    error = {'code': code, 'message': f'{code} happened'}
    if retryable is not None:
        error['retryable'] = retryable
    return [('queued', None, 0.0), ('error', None, None, error)]


@pytest.mark.parametrize('code', ['audio_too_long', 'file_too_large'])
def test_a_non_retryable_job_error_fails_without_retry(clock, code):
    fake = FakeWhisperX(script=_failing(code, False))
    store = []
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert exc.value.permanent is True and exc.value.code == code
    assert code in str(exc.value)
    assert store == [{'job_id': 'job0001', 'base_url': BASE}, None]
    assert not fake.calls(path='/jobs/job0001/result')


@pytest.mark.parametrize('code', ['out_of_memory', 'unavailable'])
def test_a_retryable_job_error_follows_the_retry_logic(clock, code):
    fake = FakeWhisperX(script=_failing(code, True))
    store = []
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert exc.value.permanent is False
    assert not is_permanent_error(str(exc.value))
    # The service does not reuse a failed job, so the retry submits again.
    assert store[-1] is None


def test_an_error_without_the_retryable_field_follows_the_documented_codes(clock):
    for code, permanent in (('decode_failed', True), ('stage_failed', False)):
        fake = FakeWhisperX(script=_failing(code, None))
        with pytest.raises(RemoteJobFailed) as exc:
            _connector(fake).transcribe(_request())
        assert exc.value.permanent is permanent


def test_the_queue_does_not_retry_a_non_retryable_job_error(clock):
    """FairJobQueue reads `permanent` from the exception before the message."""
    fake = FakeWhisperX(script=_failing('audio_too_long', False))
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request())
    assert getattr(exc.value, 'permanent', False) or is_permanent_error(str(exc.value))


def test_a_job_cancelled_on_the_service_fails_without_retry(clock):
    fake = FakeWhisperX(script=[('queued', None, 0.0), ('cancelled', None, None)])
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request())
    assert exc.value.permanent is True


def test_a_rejected_submit_without_retry_after_fails_without_retry(clock):
    fake = FakeWhisperX()
    fake.submit_answers.append((413, {}, {'detail': 'File too large'}))
    with pytest.raises(ProviderError) as exc:
        _connector(fake).transcribe(_request())
    assert exc.value.status_code == 413
    assert is_permanent_error(str(exc.value))


# --- busy service -------------------------------------------------------------------

def test_503_with_retry_after_on_submit_is_waited_out(clock):
    fake = FakeWhisperX()
    fake.submit_answers.append((503, {'Retry-After': '40'}, {'error': 'busy', 'reason': 'too_many_jobs',
                                                              'retry_after': 40}))
    assert _connector(fake).transcribe(_request()).segments
    assert clock.sleeps[0] == 40
    assert len(fake.calls('POST', '/jobs')) == 2
    # The second submit sends the audio again.
    assert b'audio bytes' in fake.calls('POST', '/jobs')[1].read()


def test_413_with_retry_after_on_submit_is_waited_out(clock):
    fake = FakeWhisperX()
    fake.submit_answers.append((413, {'Retry-After': '30'}, {'detail': 'busy'}))
    assert _connector(fake).transcribe(_request()).segments
    assert clock.sleeps[0] == 30


def test_retry_after_in_the_body_is_used_without_the_header(clock):
    fake = FakeWhisperX()
    fake.submit_answers.append((503, {}, {'error': 'busy', 'retry_after': 90}))
    assert _connector(fake).transcribe(_request()).segments
    assert clock.sleeps[0] == 90


def test_a_busy_service_beyond_the_timeout_leaves_the_job_to_the_queue_retry(clock, monkeypatch):
    monkeypatch.setattr(ASREndpointConnector, 'timeout', 300)
    fake = FakeWhisperX()
    fake.submit_answers.append((503, {'Retry-After': '3600'}, {'error': 'busy'}))
    with pytest.raises(ProviderError) as exc:
        _connector(fake).transcribe(_request())
    assert exc.value.status_code == 503
    assert not is_permanent_error(str(exc.value))
    assert clock.sleeps == []


def test_retry_after_as_an_http_date():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    seconds = asr._retry_after(httpx.Response(503, headers={'Retry-After': when}))
    assert 100 <= seconds <= 121


# --- resume ----------------------------------------------------------------------

def test_a_saved_job_is_polled_after_a_restart_without_a_new_submit(clock):
    fake = FakeWhisperX()
    job_id = fake.add_job(script=[('running', 'diarize', 0.9), ('done', None, 1.0)])
    store = []
    response = _connector(fake).transcribe(_request({'job_id': job_id, 'base_url': BASE}, store))
    assert response.segments
    assert not fake.calls('POST', '/jobs')
    assert store == [None] and fake.deleted == [job_id]


def test_a_job_saved_for_another_service_url_is_not_polled(clock):
    fake = FakeWhisperX()
    fake.add_job(job_id='elsewhere')
    _connector(fake).transcribe(_request({'job_id': 'elsewhere', 'base_url': 'http://other:9000'}))
    assert len(fake.calls('POST', '/jobs')) == 1
    assert not fake.calls(path='/jobs/elsewhere')


def test_an_expired_result_after_a_restart_is_submitted_again_once(clock):
    fake = FakeWhisperX()
    store = []
    response = _connector(fake).transcribe(_request({'job_id': 'gone0001', 'base_url': BASE}, store))
    assert response.segments
    assert len(fake.calls('POST', '/jobs')) == 1
    assert store == [None, {'job_id': 'job0001', 'base_url': BASE}, None]


def test_a_second_lost_job_ends_the_attempt(clock):
    fake = FakeWhisperX()
    handle = fake.handle

    def forget_every_job(request):
        response = handle(request)
        if request.method == 'POST' and request.url.path == '/jobs':
            fake.jobs.clear()
        return response

    fake.handle = forget_every_job
    store = []
    with pytest.raises(ProviderError) as exc:
        _connector(fake).transcribe(_request({'job_id': 'gone0001', 'base_url': BASE}, store))
    assert len(fake.calls('POST', '/jobs')) == 1
    assert not is_permanent_error(str(exc.value))
    assert store[-1] is None


def test_an_interrupted_job_is_submitted_again(clock):
    fake = FakeWhisperX()
    job_id = fake.add_job(script=[('interrupted', None, None)])
    response = _connector(fake).transcribe(_request({'job_id': job_id, 'base_url': BASE}))
    assert response.segments
    assert len(fake.calls('POST', '/jobs')) == 1


def test_a_job_still_running_at_the_timeout_is_kept_for_the_retry(clock):
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)], poll_after=60)
    store = []
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert 'timed out' in str(exc.value)
    assert not is_permanent_error(str(exc.value))
    assert store == [{'job_id': 'job0001', 'base_url': BASE}]
    assert fake.deleted == []
    assert sum(clock.sleeps) <= 600


# --- cancel ------------------------------------------------------------------------

def test_cancel_remote_job_sends_delete(monkeypatch):
    fake = FakeWhisperX()
    fake.add_job(job_id='job0042')
    monkeypatch.setattr(asr.httpx, 'Client', lambda **kw: REAL_CLIENT(transport=fake.transport, **kw))
    assert asr.cancel_remote_job(BASE + '/', 'job0042') is True
    assert fake.deleted == ['job0042']
    assert asr.cancel_remote_job(BASE, '../asr') is False
    assert asr.cancel_remote_job(BASE, None) is False
    assert fake.deleted == ['job0042']


def test_cancel_remote_job_survives_an_unreachable_service(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(asr.httpx, 'Client', lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(refuse), **kw))
    assert asr.cancel_remote_job(BASE, 'job0001') is False


# --- persistence on the processing job ---------------------------------------------------

_CREATED_USERS = []


@pytest.fixture(autouse=True)
def _remove_rows():
    """SHARED-DB: the users, recordings and jobs a test made are removed afterwards."""
    yield
    if not _CREATED_USERS:
        return
    from src.models import Speaker, SpeakerVoiceSample, TranscriptionUsage
    with app.app_context():
        user_ids = list(_CREATED_USERS)
        rec_ids = [r.id for r in Recording.query.filter(Recording.user_id.in_(user_ids)).all()]
        ProcessingJob.query.filter(ProcessingJob.user_id.in_(user_ids)).delete(synchronize_session=False)
        if rec_ids:
            SpeakerVoiceSample.query.filter(SpeakerVoiceSample.recording_id.in_(rec_ids)).delete(
                synchronize_session=False)
            for recording in Recording.query.filter(Recording.id.in_(rec_ids)).all():
                db.session.delete(recording)  # cascades to chunks, shares, tags
        db.session.flush()
        Speaker.query.filter(Speaker.user_id.in_(user_ids)).delete(synchronize_session=False)
        TranscriptionUsage.query.filter(TranscriptionUsage.user_id.in_(user_ids)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.session.commit()
    _CREATED_USERS.clear()


def _recording(audio_path='local://m.wav'):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"asrjob_{suffix}", email=f"asrjob_{suffix}@local.test",
                password="placeholder-bcrypt-hash")
    db.session.add(user)
    db.session.commit()
    _CREATED_USERS.append(user.id)
    recording = Recording(user_id=user.id, original_filename="m.wav", audio_path=audio_path,
                          status="PROCESSING", title="asr jobs")
    db.session.add(recording)
    db.session.commit()
    return recording


def _job(recording, status="processing", job_type="transcribe"):
    job = ProcessingJob(user_id=recording.user_id, recording_id=recording.id, job_type=job_type, status=status)
    db.session.add(job)
    db.session.commit()
    return job


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio bytes")
    return str(path)


def test_the_context_gives_a_stable_key_and_stores_progress(audio, tmp_path):
    with app.app_context():
        recording = _recording()
        job = _job(recording)
        first = provider_task_context(recording.id, 'asr_endpoint', audio)
        again = provider_task_context(recording.id, 'asr_endpoint', audio)
        assert first.key == again.key
        assert first.key.startswith(f"speakr-{recording.id}-{job.id}-")
        other = tmp_path / "other.wav"
        other.write_bytes(b"other bytes")
        assert provider_task_context(recording.id, 'asr_endpoint', str(other)).key != first.key

        first.report_progress(42, 100)
        db.session.expire_all()
        stored = db.session.get(ProcessingJob, job.id)
        assert (stored.progress_done, stored.progress_total) == (42, 100)

        job.status = 'completed'
        db.session.commit()
        _job(recording, job_type='reprocess_transcription')
        assert provider_task_context(recording.id, 'asr_endpoint', audio).key != first.key


def test_no_context_without_a_running_job(audio):
    with app.app_context():
        recording = _recording()
        assert provider_task_context(recording.id, 'asr_endpoint', audio) == (None, None, None, None)


def test_cancel_remote_tasks_cancels_only_saved_asr_jobs(audio, monkeypatch):
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append((base, job_id)))
    with app.app_context():
        recording = _recording()
        job = _job(recording)
        provider_task_context(recording.id, 'asr_endpoint', audio).save({'job_id': 'job0007', 'base_url': BASE})
        other = _job(recording, status='failed', job_type='reprocess_transcription')
        other.provider_task = json.dumps({'provider': 'assemblyai', 'task': {'transcript_id': 'x'}})
        broken = _job(recording, status='failed', job_type='reprocess_transcription')
        broken.provider_task = '{not json'
        db.session.commit()
        db.session.expire_all()
        cancel_remote_tasks(ProcessingJob.query.filter_by(recording_id=recording.id).all())
    assert cancelled == [(BASE, 'job0007')]


def test_deleting_a_recording_cancels_its_remote_job(audio, monkeypatch):
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append(job_id))
    from src.services.recording_deletion import delete_recording_completely
    with app.app_context():
        recording = _recording()
        _job(recording)
        provider_task_context(recording.id, 'asr_endpoint', audio).save({'job_id': 'job0008', 'base_url': BASE})
        db.session.expire_all()
        recording = db.session.get(Recording, recording.id)
        with patch('src.services.webhook_dispatch.emit_webhook_event'):
            delete_recording_completely(recording, storage=Mock())
    assert cancelled == ['job0008']


def _login(client, user_id):
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True


def test_deleting_a_job_cancels_its_remote_job(audio, monkeypatch):
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append(job_id))
    with app.app_context():
        recording = _recording()
        job = _job(recording)
        provider_task_context(recording.id, 'asr_endpoint', audio).save({'job_id': 'job0009', 'base_url': BASE})
        job_id, user_id = job.id, recording.user_id
    client = app.test_client()
    _login(client, user_id)
    assert client.delete(f'/api/recordings/jobs/{job_id}').status_code == 200
    assert cancelled == ['job0009']


def test_resetting_a_recording_cancels_the_remote_job_of_its_queued_job(audio, monkeypatch):
    cancelled = []
    monkeypatch.setattr(asr, 'cancel_remote_job', lambda base, job_id: cancelled.append(job_id))
    with app.app_context():
        recording = _recording()
        job = _job(recording)
        provider_task_context(recording.id, 'asr_endpoint', audio).save({'job_id': 'job0010', 'base_url': BASE})
        # A restart puts the running job back in the queue with its saved job.
        job.status = 'queued'
        db.session.commit()
        recording_id, user_id = recording.id, recording.user_id
    client = app.test_client()
    _login(client, user_id)
    assert client.post(f'/recording/{recording_id}/reset_status').status_code == 200
    assert cancelled == ['job0010']


# --- through the processing task -------------------------------------------------------

def _run_processing(rid, path, connector):
    conversion = MagicMock(was_converted=False)
    with patch("src.services.transcription.get_connector", return_value=connector), \
         patch.object(proc, "is_video_file", return_value=False), \
         patch.object(proc, "convert_if_needed", return_value=conversion), \
         patch.object(proc, "client", MagicMock()), \
         patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
         patch.object(proc, "generate_title_task"), \
         patch.object(proc, "generate_summary_only_task"), \
         patch.object(proc, "chunking_service") as chunking:
        chunking.needs_chunking.return_value = False
        chunking.get_audio_duration.return_value = 60.0
        proc.transcribe_with_connector(app.app_context(), rid, path, "m.wav", time.time(), mime_type="audio/wav")


def test_processing_sends_the_job_key_saves_the_job_and_resumes_it(clock):
    with app.app_context():
        recording = _recording()
        rid = recording.id
        job = _job(recording)
        job_id = job.id
        path = os.path.join(app.config["UPLOAD_FOLDER"], f"asrjob_{rid}.wav")
        with open(path, "wb") as f:
            f.write(b"audio bytes")
    try:
        # First attempt: the service is still running the job at the timeout.
        fake = FakeWhisperX(script=[('running', 'whisper', 0.25)], poll_after=60)
        with pytest.raises(TranscriptionError):
            _run_processing(rid, path, _connector(fake))
        submit = fake.calls('POST', '/jobs')[0]
        assert submit.headers['Idempotency-Key'].startswith(f"speakr-{rid}-{job_id}-")
        with app.app_context():
            stored = db.session.get(ProcessingJob, job_id)
            assert json.loads(stored.provider_task)['task'] == {'job_id': 'job0001', 'base_url': BASE}
            assert (stored.progress_done, stored.progress_total) == (25, 100)

        # The retry (or a restart) polls the same job; nothing is submitted.
        fake.jobs['job0001']['script'] = [('done', None, 1.0)]
        fake.jobs['job0001']['state'] = None
        _run_processing(rid, path, _connector(fake))
        assert len(fake.calls('POST', '/jobs')) == 1
        assert fake.deleted == ['job0001']
        with app.app_context():
            stored = db.session.get(ProcessingJob, job_id)
            assert stored.provider_task is None
            recording = db.session.get(Recording, rid)
            assert 'Hello there.' in recording.transcription
    finally:
        os.remove(path)
