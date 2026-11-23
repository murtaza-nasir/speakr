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
        # None: a fraction is an estimate until the job is done, as the service
        # reports it. False: every fraction is measured.
        self.estimated = None
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
                'progress': {'stage': stage, 'fraction': fraction,
                             'fraction_estimated': (status != 'done') if self.estimated is None else self.estimated,
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
    # Only a measured fraction is stored; the estimates of a running job are not.
    assert progress == [(100, 100)]
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
    # The job is running: it is marked for cancel (202) and the service is told at once.
    assert client.delete(f'/api/recordings/jobs/{job_id}').status_code == 202
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
         patch.object(proc.llm_config, "is_configured", return_value=True), \
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
        fake.estimated = False  # a measured fraction is stored
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


# --- a job that ends for good leaves the service ------------------------------------------
#
# Every final exit sends DELETE /jobs/<id>; an attempt that will be resumed
# (a timeout, failed polls) keeps the job.

def test_a_failed_job_is_deleted_on_the_service(clock):
    fake = FakeWhisperX(script=_failing('out_of_memory', True))
    store = []
    with pytest.raises(RemoteJobFailed):
        _connector(fake).transcribe(_request(store=store))
    assert fake.deleted == ['job0001'] and 'job0001' not in fake.jobs
    assert store == [{'job_id': 'job0001', 'base_url': BASE}, None]


def test_a_job_cancelled_on_the_service_is_deleted_there(clock):
    fake = FakeWhisperX(script=[('queued', None, 0.0), ('cancelled', None, None)])
    with pytest.raises(RemoteJobFailed):
        _connector(fake).transcribe(_request())
    assert fake.deleted == ['job0001']


def test_a_job_that_ended_without_a_result_is_deleted(clock):
    fake = FakeWhisperX()
    handle = fake.handle

    def no_result(request):
        if request.method == 'GET' and request.url.path.endswith('/result'):
            fake.requests.append(request)
            return httpx.Response(409, json={'status': 'error'})
        return handle(request)

    fake.handle = no_result
    store = []
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert exc.value.permanent is False
    assert fake.deleted == ['job0001'] and store[-1] is None


def test_a_failed_delete_does_not_hide_the_job_error(clock):
    fake = FakeWhisperX(script=_failing('audio_too_long', False))
    handle = fake.handle

    def delete_fails(request):
        if request.method == 'DELETE':
            fake.requests.append(request)
            raise httpx.ConnectError("refused", request=request)
        return handle(request)

    fake.handle = delete_fails
    store = []
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request(store=store))
    assert exc.value.code == 'audio_too_long' and exc.value.permanent is True
    assert len(fake.calls('DELETE')) == 1
    assert store[-1] is None


def test_a_delete_answered_with_an_error_does_not_hide_the_job_error(clock):
    fake = FakeWhisperX(script=_failing('stage_failed', None))
    handle = fake.handle

    def delete_500(request):
        if request.method == 'DELETE':
            fake.requests.append(request)
            return httpx.Response(500, json={'error': 'boom'})
        return handle(request)

    fake.handle = delete_500
    with pytest.raises(RemoteJobFailed) as exc:
        _connector(fake).transcribe(_request())
    assert exc.value.code == 'stage_failed'


def test_an_interrupted_job_is_deleted_before_the_new_submit(clock):
    fake = FakeWhisperX()
    job_id = fake.add_job(script=[('interrupted', None, None)])
    store = []
    response = _connector(fake).transcribe(_request({'job_id': job_id, 'base_url': BASE}, store))
    assert response.segments
    assert fake.deleted == [job_id, 'job0002']
    order = [(r.method, r.url.path) for r in fake.requests if r.method in ('DELETE', 'POST')]
    assert order[:2] == [('DELETE', f'/jobs/{job_id}'), ('POST', '/jobs')]


def test_a_job_unknown_to_the_service_gets_no_delete(clock):
    fake = FakeWhisperX()
    _connector(fake).transcribe(_request({'job_id': 'gone0001', 'base_url': BASE}))
    assert fake.deleted == ['job0001']


def test_failed_polls_beyond_the_limit_send_no_delete(clock):
    fake = FakeWhisperX()
    fake.poll_answers = [(503, {}, {})] * 10
    store = []
    with pytest.raises(TranscriptionError):
        _connector(fake).transcribe(_request(store=store))
    assert fake.deleted == [] and store == [{'job_id': 'job0001', 'base_url': BASE}]


def test_a_shutdown_while_polling_sends_no_delete(clock, monkeypatch):
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)])

    def stop(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(asr, '_sleep', stop)
    store = []
    with pytest.raises(KeyboardInterrupt):
        _connector(fake).transcribe(_request(store=store))
    assert fake.deleted == [] and store == [{'job_id': 'job0001', 'base_url': BASE}]


def test_a_job_submitted_for_a_deleted_processing_job_is_removed(clock):
    """The save reports False when the processing job no longer exists."""
    fake = FakeWhisperX()
    saves = []

    def save(task):
        saves.append(task)
        return False

    request = _request()
    request.save_provider_task = save
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(request)
    assert 'no longer exists' in str(exc.value)
    assert fake.deleted == ['job0001']
    assert not fake.calls('GET', '/jobs/job0001')


def test_a_job_cancelled_with_its_processing_job_is_not_submitted_again(clock):
    """Deleting the recording cancels the job; the poller finds it gone and stops."""
    fake = FakeWhisperX()
    request = _request({'job_id': 'gone0001', 'base_url': BASE})
    request.save_provider_task = lambda task: False
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(request)
    assert 'no longer exists' in str(exc.value)
    assert not fake.calls('POST', '/jobs')


def test_the_synchronous_fallback_removes_the_saved_job(clock, monkeypatch):
    monkeypatch.setenv('ASR_ASYNC_JOBS', 'false')
    fake = FakeWhisperX()
    job_id = fake.add_job(script=[('running', 'whisper', 0.5)])
    store = []
    response = _connector(fake).transcribe(_request({'job_id': job_id, 'base_url': BASE}, store))
    assert response.segments
    assert fake.deleted == [job_id] and store == [None]


def test_the_synchronous_fallback_keeps_a_saved_job_it_could_not_remove(clock, monkeypatch):
    monkeypatch.setenv('ASR_ASYNC_JOBS', 'false')
    fake = FakeWhisperX()
    job_id = fake.add_job(script=[('running', 'whisper', 0.5)])
    handle = fake.handle

    def delete_fails(request):
        if request.method == 'DELETE':
            raise httpx.ConnectError("refused", request=request)
        return handle(request)

    fake.handle = delete_fails
    store = []
    response = _connector(fake).transcribe(_request({'job_id': job_id, 'base_url': BASE}, store))
    assert response.segments
    assert store == []


def test_the_synchronous_fallback_leaves_a_job_of_another_service_url_alone(clock, monkeypatch):
    monkeypatch.setenv('ASR_ASYNC_JOBS', 'false')
    fake = FakeWhisperX()
    store = []
    _connector(fake).transcribe(_request({'job_id': 'elsewhere', 'base_url': 'http://other:9000'}, store))
    assert not fake.calls('DELETE') and store == [None]


# --- the processing job ends for good ------------------------------------------------------

from src.services.job_queue import job_queue
from src.services.transcription.provider_tasks import release_remote_task
from src.tasks.processing import RecordingDeletedDuringJob


@pytest.fixture
def remote(monkeypatch):
    """Records the cancels sent to the service; `remote.answer` is what the service confirms."""
    class Remote:
        answer = True
        cancelled = []

    state = Remote()
    state.cancelled = []

    def cancel(base_url, job_id):
        state.cancelled.append(job_id)
        return state.answer

    monkeypatch.setattr(asr, 'cancel_remote_job', cancel)
    return state


def _saved_job(audio, remote_id, status='processing'):
    recording = _recording()
    job = _job(recording)
    provider_task_context(recording.id, 'asr_endpoint', audio).save({'job_id': remote_id, 'base_url': BASE})
    if status != 'processing':
        job.status = status
        db.session.commit()
    db.session.expire_all()
    return recording.id, job.id


def _task(job_id):
    db.session.expire_all()
    return db.session.get(ProcessingJob, job_id).provider_task


def test_the_save_reports_a_deleted_processing_job(audio):
    with app.app_context():
        recording = _recording()
        job = _job(recording)
        context = provider_task_context(recording.id, 'asr_endpoint', audio)
        assert context.save({'job_id': 'job0020', 'base_url': BASE}) is True
        db.session.delete(job)
        db.session.commit()
        assert context.save(None) is False
        assert context.save({'job_id': 'job0021', 'base_url': BASE}) is False


def test_release_clears_the_task_once_the_service_confirms(audio, remote):
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0022')
        job = db.session.get(ProcessingJob, job_id)
        remote.answer = False
        assert release_remote_task(job) is False
        db.session.commit()
        assert json.loads(_task(job_id))['task']['job_id'] == 'job0022'
        remote.answer = True
        job = db.session.get(ProcessingJob, job_id)
        assert release_remote_task(job) is True
        db.session.commit()
        assert _task(job_id) is None
    assert remote.cancelled == ['job0022', 'job0022']


def test_release_leaves_the_task_of_another_provider(audio, remote):
    with app.app_context():
        recording = _recording()
        job = _job(recording)
        job.provider_task = json.dumps({'provider': 'assemblyai', 'task': {'transcript_id': 'x'}})
        db.session.commit()
        assert release_remote_task(job) is False
        assert job.provider_task is not None
    assert remote.cancelled == []


def test_release_never_raises(audio, monkeypatch):
    def explode(base_url, job_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(asr, 'cancel_remote_job', explode)
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0023')
        assert release_remote_task(db.session.get(ProcessingJob, job_id)) is False
        assert _task(job_id) is not None


def _process(job_id, error=None, retry_count=0):
    """Run the queue's job handler with the transcription step replaced."""
    job_queue.init_app(app)
    with app.app_context():
        job = db.session.get(ProcessingJob, job_id)
        job.retry_count = retry_count
        db.session.commit()
        runner = Mock(side_effect=error) if error is not None else Mock(return_value=None)
        with patch.object(job_queue, '_run_audio_job', runner), \
             patch.object(job_queue, '_emit_failure_webhook'), \
             patch.object(job_queue, '_emit_failure_email'), \
             patch.object(job_queue, '_emit_completion_webhook'), \
             patch.object(job_queue, '_emit_completion_email'):
            job_queue._process_job(job)
    with app.app_context():
        job = db.session.get(ProcessingJob, job_id)
        if job is None:
            return None, None, None
        return job.status, job.provider_task, job.error_message


def test_a_permanent_failure_removes_the_remote_job(audio, remote):
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0030')
    error = RemoteJobFailed("ASR job failed: audio_too_long", permanent=True)
    status, task, message = _process(job_id, error)
    assert status == 'failed' and task is None
    assert 'audio_too_long' in message
    assert remote.cancelled == ['job0030']


def test_a_failure_with_retries_left_keeps_the_remote_job(audio, remote):
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0031')
    status, task, _ = _process(job_id, TranscriptionError("ASR job job0031 timed out after 600s (status running)"))
    assert status == 'queued'
    assert json.loads(task)['task']['job_id'] == 'job0031'
    assert remote.cancelled == []


def test_the_last_failed_retry_removes_the_remote_job(audio, remote):
    from src.services.job_queue import MAX_RETRIES
    with app.app_context():
        recording_id, job_id = _saved_job(audio, 'job0032')
    error = TranscriptionError("ASR job job0032 could not be polled: HTTP 503")
    status, task, message = _process(job_id, error, retry_count=MAX_RETRIES - 1)
    assert status == 'failed' and task is None
    assert remote.cancelled == ['job0032']
    # The original error is what the job and the recording report.
    assert 'could not be polled' in message
    with app.app_context():
        assert db.session.get(Recording, recording_id).status == 'FAILED'


def test_a_final_failure_keeps_the_task_while_the_service_is_unreachable(audio, remote):
    remote.answer = False
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0033')
    status, task, message = _process(job_id, RemoteJobFailed("ASR job failed", permanent=True))
    assert status == 'failed' and 'ASR job failed' in message
    assert json.loads(task)['task']['job_id'] == 'job0033'
    assert remote.cancelled == ['job0033']


def test_a_recording_deleted_while_the_job_ran_removes_the_remote_job(audio, remote):
    with app.app_context():
        recording_id, job_id = _saved_job(audio, 'job0034')
    status, task, message = _process(job_id, RecordingDeletedDuringJob(recording_id))
    assert status is None                            # the job is removed (#625)
    assert remote.cancelled == ['job0034']


def test_a_completed_job_with_a_task_left_over_removes_the_remote_job(audio, remote):
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0035')
    status, task, _ = _process(job_id)
    assert status == 'completed' and task is None
    assert remote.cancelled == ['job0035']


def test_a_completed_job_without_a_task_sends_nothing(audio, remote):
    with app.app_context():
        recording = _recording()
        job_id = _job(recording).id
    status, task, _ = _process(job_id)
    assert status == 'completed' and task is None and remote.cancelled == []


def test_resetting_a_recording_clears_the_task_of_its_queued_job(audio, remote):
    with app.app_context():
        recording_id, job_id = _saved_job(audio, 'job0036', status='queued')
        user_id = db.session.get(Recording, recording_id).user_id
    client = app.test_client()
    _login(client, user_id)
    assert client.post(f'/recording/{recording_id}/reset_status').status_code == 200
    with app.app_context():
        job = db.session.get(ProcessingJob, job_id)
        assert job.status == 'failed' and job.provider_task is None
    assert remote.cancelled == ['job0036']


def test_cleanup_of_old_jobs_tries_the_remote_job_once_more(audio, remote):
    from datetime import datetime, timedelta
    job_queue.init_app(app)
    with app.app_context():
        _, job_id = _saved_job(audio, 'job0037', status='failed')
        job = db.session.get(ProcessingJob, job_id)
        job.completed_at = datetime.utcnow() - timedelta(hours=48)
        db.session.commit()
    job_queue.cleanup_old_jobs(max_age_hours=24)
    with app.app_context():
        assert db.session.get(ProcessingJob, job_id) is None
    assert 'job0037' in remote.cancelled


# --- a job nothing saved (chunks, incognito mode, the voice check) --------------
#
# Without save_provider_task no retry can resume the job and no processing
# job removes it later, so the connector deletes it on every failure. The
# callers give each request a task key, so a retry attaches to a job the
# service still holds.

def test_a_timeout_without_a_saved_task_deletes_the_job(clock):
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)], poll_after=60)
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(_request(task_key=None))
    assert 'timed out' in str(exc.value) and 'resumes' not in str(exc.value)
    assert fake.deleted == ['job0001'] and fake.jobs == {}


def test_failed_polls_without_a_saved_task_delete_the_job(clock):
    fake = FakeWhisperX()
    fake.poll_answers = [(503, {}, {})] * 10
    with pytest.raises(TranscriptionError):
        _connector(fake).transcribe(_request())
    assert fake.deleted == ['job0001'] and fake.jobs == {}


def test_a_shutdown_without_a_saved_task_deletes_the_job(clock, monkeypatch):
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)])

    def stop(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(asr, '_sleep', stop)
    with pytest.raises(KeyboardInterrupt):
        _connector(fake).transcribe(_request())
    assert fake.deleted == ['job0001']


def test_an_unreadable_result_without_a_saved_task_deletes_the_job(clock, monkeypatch):
    fake = FakeWhisperX()
    connector = _connector(fake)
    monkeypatch.setattr(connector, '_parse_response', Mock(side_effect=ValueError('bad result')))
    with pytest.raises(TranscriptionError):
        connector.transcribe(_request())
    assert fake.deleted == ['job0001']


def test_a_failed_job_without_a_saved_task_is_deleted_once(clock):
    fake = FakeWhisperX(script=_failing('stage_failed', True))
    with pytest.raises(RemoteJobFailed):
        _connector(fake).transcribe(_request())
    assert fake.deleted == ['job0001']


def test_a_lost_job_without_a_saved_task_gets_no_delete(clock):
    fake = FakeWhisperX()
    handle = fake.handle

    def forgets(request):
        # The service loses every job right after it accepts it.
        response = handle(request)
        if request.method == 'POST' and request.url.path == '/jobs':
            fake.jobs.clear()
        return response

    fake.handle = forgets
    with pytest.raises(TranscriptionError):
        _connector(fake).transcribe(_request())
    assert len(fake.calls('POST', '/jobs')) == 2 and fake.deleted == []


def test_a_failed_delete_does_not_hide_the_timeout(clock):
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)], poll_after=60)
    handle = fake.handle

    def delete_fails(request):
        if request.method == 'DELETE':
            fake.requests.append(request)
            raise httpx.ConnectError("refused", request=request)
        return handle(request)

    fake.handle = delete_fails
    with pytest.raises(TranscriptionError) as exc:
        _connector(fake).transcribe(_request())
    assert 'timed out' in str(exc.value) and len(fake.calls('DELETE')) == 1


def test_the_audio_task_key_follows_the_scope_the_part_and_the_bytes(tmp_path):
    from src.services.transcription.provider_tasks import audio_task_key
    one, same, other = (tmp_path / n for n in ('one.mp3', 'same.mp3', 'other.mp3'))
    one.write_bytes(b'first audio')
    same.write_bytes(b'first audio')
    other.write_bytes(b'second audio')
    key = audio_task_key('speakr-7', str(one), part='chunk0')
    assert key.startswith('speakr-7-chunk0-')
    assert key == audio_task_key('speakr-7', str(same), part='chunk0')
    assert key != audio_task_key('speakr-7', str(other), part='chunk0')
    assert key != audio_task_key('speakr-7', str(one), part='chunk1')
    assert key != audio_task_key('speakr-8', str(one), part='chunk0')
    assert audio_task_key('speakr-7', str(tmp_path / 'missing.mp3')) is None


def _chunked(fake, tmp_path, monkeypatch, contents, **kwargs):
    """Run transcribe_chunks_with_connector over chunk files with these bytes."""
    chunks = []
    for i, content in enumerate(contents):
        path = tmp_path / f"chunk{i}.mp3"
        path.write_bytes(content)
        chunks.append({'index': i, 'path': str(path), 'filename': path.name, 'size_mb': 0.1,
                       'start_time': i * 10.0, 'end_time': (i + 1) * 10.0, 'duration': 10.0})
    chunker = MagicMock()
    chunker.create_chunks.return_value = chunks
    chunker.merge_transcriptions.side_effect = lambda results: ' '.join(r['transcription'] for r in results)
    monkeypatch.setattr(proc, 'chunking_service', chunker)
    monkeypatch.setattr(proc.time, 'sleep', lambda seconds: None)
    with app.app_context():
        return proc.transcribe_chunks_with_connector(
            _connector(fake), str(tmp_path / 'long.mp3'), 'long.mp3', 'audio/mpeg', 'en', **kwargs)


def _submit_keys(fake):
    return [r.headers.get('Idempotency-Key') for r in fake.calls('POST', '/jobs')]


def test_a_chunk_that_times_out_three_times_leaves_no_job_on_the_service(clock, tmp_path, monkeypatch):
    fake = FakeWhisperX(script=[('running', 'whisper', 0.1)], poll_after=60)
    merged = _chunked(fake, tmp_path, monkeypatch, [b'chunk audio'], task_scope='speakr-41')
    assert 'transcription failed' in merged
    assert fake.deleted == ['job0001', 'job0002', 'job0003'] and fake.jobs == {}
    keys = _submit_keys(fake)
    assert len(keys) == 3 and len(set(keys)) == 1 and keys[0].startswith('speakr-41-chunk0-')


def test_a_chunk_retry_after_a_dropped_submit_attaches_to_the_same_job(clock, tmp_path, monkeypatch):
    fake = FakeWhisperX()
    fake.drop_submit_response = True
    merged = _chunked(fake, tmp_path, monkeypatch, [b'chunk audio'], task_scope='speakr-42')
    assert 'Hello there.' in merged
    assert len(fake.calls('POST', '/jobs')) == 2 and fake._ids == 1
    assert fake.deleted == ['job0001'] and fake.jobs == {}


def test_chunks_get_different_keys_even_with_the_same_bytes(clock, tmp_path, monkeypatch):
    fake = FakeWhisperX()
    _chunked(fake, tmp_path, monkeypatch, [b'same bytes', b'same bytes', b'other bytes'], task_scope='speakr-43')
    keys = _submit_keys(fake)
    assert len(keys) == 3 and len(set(keys)) == 3
    assert fake._ids == 3 and fake.jobs == {}


def test_chunked_calls_without_a_scope_do_not_share_keys(clock, tmp_path, monkeypatch):
    fake = FakeWhisperX()
    _chunked(fake, tmp_path, monkeypatch, [b'same bytes'])
    _chunked(fake, tmp_path, monkeypatch, [b'same bytes'])
    keys = _submit_keys(fake)
    assert all(keys) and keys[0] != keys[1]


def test_incognito_mode_sends_a_key_for_the_user_and_the_audio(clock, tmp_path, monkeypatch):
    fake = FakeWhisperX()
    path = tmp_path / 'incognito.wav'
    path.write_bytes(b'incognito audio')

    def passthrough(filepath, **kwargs):
        return Mock(output_path=filepath, mime_type='audio/wav', was_converted=False, was_compressed=False)

    chunker = MagicMock()
    chunker.needs_chunking.return_value = False
    chunker.get_audio_duration.return_value = 1
    monkeypatch.setattr(proc, 'chunking_service', chunker)
    monkeypatch.setattr(proc, 'convert_if_needed', passthrough)
    monkeypatch.setattr(proc, 'is_video_file', lambda *a, **k: False)
    user = Mock(id=77, transcription_language=None)
    with app.app_context(), patch('src.services.transcription.get_registry') as registry:
        registry.return_value.get_active_connector.return_value = _connector(fake)
        for _ in range(2):
            result = proc.transcribe_incognito(str(path), 'incognito.wav', language='en', user=user)
            assert not result.get('error'), result
    keys = _submit_keys(fake)
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0].startswith('speakr-incognito-77-')
    assert fake.jobs == {}


def test_the_voice_check_sends_a_key(clock):
    import src.services.voice_embedding_check as check
    fake = FakeWhisperX()
    with app.app_context(), patch('src.services.transcription.registry.get_registry') as registry:
        registry.return_value.get_active_connector.return_value = _connector(fake)
        try:
            check.probe_backend()
        except check.CanaryUnavailable:
            pass   # the fake result carries no embedding
    keys = _submit_keys(fake)
    assert len(keys) == 1 and keys[0].startswith('speakr-voice-check-')
    assert fake.jobs == {}
