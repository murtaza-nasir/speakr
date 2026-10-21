"""Tencent Cloud recording-file ASR (TC3 signed submit-and-poll API)."""

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from ..base import (BaseTranscriptionConnector, ConnectorSpecifications,
                    TranscriptionCapability, TranscriptionResponse, TranscriptionSegment)
from ..exceptions import ConfigurationError, ProviderError, TranscriptionError
from ..tencent_state import locked_checkpoint, prune_checkpoints

logger = logging.getLogger(__name__)
HOST = 'asr.tencentcloudapi.com'
INLINE_LIMIT = 5 * 1024 * 1024
FILE_LIMIT = 1024 * 1024 * 1024
MODELS = ('16k_zh_en_meeting', '16k_zh_en_2.0')
# Tencent keeps task results for 24 hours, so a checkpoint is no use for
# recovery after that; it is removed a day later.
CHECKPOINT_MAX_AGE = 2 * 24 * 3600


class TencentASRConnector(BaseTranscriptionConnector):
    PROVIDER_NAME = 'tencent_asr'
    CAPABILITIES = {TranscriptionCapability.DIARIZATION,
                    TranscriptionCapability.TIMESTAMPS, TranscriptionCapability.HOTWORDS}
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=FILE_LIMIT, max_duration_seconds=18000,
        handles_chunking_internally=True, preserve_original_audio=True)

    def _validate_config(self):
        self.secret_id = self.config.get('secret_id', '').strip()
        self.secret_key = self.config.get('secret_key', '').strip()
        if not self.secret_id or not self.secret_key:
            raise ConfigurationError('Tencent ASR requires TENCENT_SECRET_ID and TENCENT_SECRET_KEY')
        self.model = self.config.get('model') or MODELS[0]
        if self.model not in MODELS:
            raise ConfigurationError('Tencent ASR supports: ' + ', '.join(MODELS))
        try:
            self.poll_timeout = float(self.config.get('timeout', 1800))
            self.poll_interval = float(self.config.get('poll_interval', 5))
        except (TypeError, ValueError):
            raise ConfigurationError('Tencent timeout and poll interval must be positive numbers') from None
        if not (0 < self.poll_interval <= self.poll_timeout <= 10800):
            raise ConfigurationError('Tencent requires 0 < poll_interval <= timeout <= 10800 seconds')
        self.hotword_id = self.config.get('hotword_id', '').strip()
        self.default_diarize = self.config.get('diarize', True)
        self.state_dir = self.config.get('state_dir')
        self.staging_prefix = (self.config.get('staging_prefix') or 'tencent-asr').strip('/')
        if not self.staging_prefix or '..' in self.staging_prefix.split('/'):
            raise ConfigurationError('Tencent staging prefix must be a nonempty object prefix without ..')

    def _headers(self, action, body, timestamp):
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d')
        content_type = 'application/json; charset=utf-8'
        canonical = ('POST\n/\n\n' + f'content-type:{content_type}\nhost:{HOST}\n'
                     + '\ncontent-type;host\n' + hashlib.sha256(body).hexdigest())
        scope = date + '/asr/tc3_request'
        message = f'TC3-HMAC-SHA256\n{timestamp}\n{scope}\n' + hashlib.sha256(canonical.encode()).hexdigest()
        def sign(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()
        key = sign(sign(sign(('TC3' + self.secret_key).encode(), date), 'asr'), 'tc3_request')
        signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
        return {
            'Authorization': f'TC3-HMAC-SHA256 Credential={self.secret_id}/{scope}, '
                             f'SignedHeaders=content-type;host, Signature={signature}',
            'Content-Type': content_type, 'Host': HOST,
            'X-TC-Action': action, 'X-TC-Version': '2019-06-14',
            'X-TC-Timestamp': str(timestamp),
        }

    def _call(self, client, action, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        response = client.post('https://' + HOST, content=body,
                               headers=self._headers(action, body, int(time.time())))
        if response.status_code != 200:
            raise ProviderError(f'Tencent {action}: HTTP {response.status_code}',
                                provider=self.PROVIDER_NAME, status_code=response.status_code)
        try:
            data = response.json()['Response']
            if not isinstance(data, dict):
                raise ValueError('Invalid envelope')
            error = data.get('Error')
            if error:
                if not isinstance(error, dict):
                    raise ValueError('Invalid error')
                code = str(error.get('Code', 'Unknown'))
                hint = ''
                if 'Amount' in code or 'ServiceIsolate' in code or 'UserNotRegistered' in code:
                    hint = ' Check activation and recording-file ASR quota for the selected engine; real-time packages do not apply.'
                elif code.startswith(('AuthFailure', 'UnauthorizedOperation')):
                    hint = ' Check credentials, CAM permissions and system clock.'
                raise ProviderError(f'Tencent {action}: {code}.{hint}', provider=self.PROVIDER_NAME,
                                    status_code=(429 if code.startswith('RequestLimitExceeded')
                                                 else 500 if code.startswith('InternalError') else 400))
            if not isinstance(data['Data'], dict):
                raise ValueError('Invalid task data')
            return data['Data']
        except (KeyError, TypeError, ValueError):
            # Never include response bodies, signed URLs or credentials in errors.
            raise ProviderError(f'Tencent {action}: invalid JSON response', provider=self.PROVIDER_NAME) from None

    def _parameters(self, request):
        # Shared UI settings can still contain a model from the previous provider.
        model = request.model if request.model in MODELS else self.model
        params = {'EngineModelType': model, 'ChannelNum': 1, 'ResTextFormat': 3,
                  'SpeakerDiarization': int(request.diarize or model == MODELS[0])}
        words = []
        if request.hotwords:
            given = list(dict.fromkeys(w.strip() for w in request.hotwords.split(',') if w.strip()))
            # Tencent rejects the whole task for one word it cannot take, and
            # the same hotwords are used with every connector, so those words
            # are left out of the request.
            words = [w for w in given if len(w.encode('utf-8')) <= 30 and '|' not in w][:128]
            if len(words) < len(given):
                logger.warning('Tencent ASR: %d of %d hotwords left out (at most 128 words, '
                               '30 UTF-8 bytes per word, no | characters)', len(given) - len(words), len(given))
        if words:
            params['HotwordList'] = ','.join(w + '|5' for w in words)
        elif self.hotword_id:
            params['HotwordId'] = self.hotword_id
        return params

    def _stage(self, path, request, state=None, save=None):
        from src.services.storage import get_storage_service, parse_locator
        storage = get_storage_service()
        if not storage.s3:
            raise ConfigurationError('Tencent audio over 5 MiB requires Speakr S3 storage with a publicly reachable endpoint')
        key = self.staging_prefix + '/' + uuid4().hex + Path(request.filename).suffix
        locator = parse_locator(storage.s3.build_locator(key))
        if state is not None:
            state['staging_locator'] = storage.s3.build_locator(key)
            state['staging_endpoint'] = storage.s3.endpoint_url
            save()  # Remember the key even if the worker dies during upload.
        try:
            storage.s3.upload_local_file(path, key, content_type=request.mime_type or 'application/octet-stream', acl='private')
            url = storage.s3.presign_get_url(locator, expires_seconds=86400)
            return storage.s3, locator, url
        except Exception:
            self._cleanup(storage.s3, locator)
            raise TranscriptionError('Tencent ASR staging failed; check S3 configuration and permissions') from None

    @staticmethod
    def _cleanup(storage, locator):
        try:
            storage.delete(locator, missing_ok=True)
            return True
        except Exception:
            logger.warning('Tencent ASR staging cleanup failed; configure a tencent-asr/ bucket lifecycle rule')
            return False

    def _poll(self, client, task_id):
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            try:
                data = self._call(client, 'DescribeTaskStatus', {'TaskId': task_id})
                status = data.get('Status')
                if status in (2, 3):
                    return data
                if status not in (0, 1):
                    raise ProviderError(f'Tencent task {task_id}: unexpected status', provider=self.PROVIDER_NAME)
            except httpx.TransportError:
                pass  # Polling is safe to retry; submission is not.
            except ProviderError as exc:
                if exc.status_code != 429 and not (exc.status_code and exc.status_code >= 500):
                    raise
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise TranscriptionError(f'Tencent task {task_id}: polling timed out. The remote task may still be running; '
                                 'retry the same recording to resume polling the saved task.')

    def _state_directory(self):
        if self.state_dir:
            return self.state_dir
        # Next to the SQLite database on the instance volume, as for the
        # generated secret key; Flask's instance_path (/app/instance) is not a
        # volume and is lost when the container is recreated.
        uri = os.environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:////data/instance/transcriptions.db')
        if uri.startswith('sqlite:///'):
            base = os.path.dirname(uri.split('sqlite:///', 1)[1]) or '.'
        else:
            base = '/data/instance'
        return os.path.join(base, 'tencent-tasks')

    def _cleanup_checkpoint(self, state, save):
        if not state.get('staging_locator'):
            return
        from src.services.storage import get_storage_service, parse_locator
        storage = get_storage_service().s3
        if not storage or storage.endpoint_url != state.get('staging_endpoint'):
            logger.warning('Tencent staging cleanup deferred: original storage endpoint is unavailable')
            return
        if self._cleanup(storage, parse_locator(state['staging_locator'])):
            state.pop('staging_locator', None)
            state.pop('staging_endpoint', None)
            save()

    def transcribe(self, request):
        params = self._parameters(request)
        try:
            request.audio_file.seek(0)
        except (AttributeError, OSError, io.UnsupportedOperation):
            pass
        # Spool without transcoding; hash original bytes to identify safe retries.
        with tempfile.TemporaryDirectory(prefix='speakr_tencent_') as directory:
            path = Path(directory) / 'audio'
            digest = hashlib.sha256()
            size = 0
            with path.open('wb') as output:
                while True:
                    chunk = request.audio_file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > FILE_LIMIT:
                        raise TranscriptionError('Tencent ASR: audio exceeds 1 GiB URL limit')
                    output.write(chunk)
                    digest.update(chunk)
            if not size:
                raise TranscriptionError('Tencent ASR: empty audio file')
            identity = json.dumps([self.secret_id, digest.hexdigest(), params], sort_keys=True)
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            directory = self._state_directory()
            for locator in prune_checkpoints(directory, CHECKPOINT_MAX_AGE):
                logger.warning('Tencent ASR: expired checkpoint removed; its staging object %s was not '
                               'deleted and is left to the bucket lifecycle rule', locator)
            with locked_checkpoint(directory, fingerprint) as (state, save):
                state.update(source_sha256=digest.hexdigest(), source_bytes=size)
                save()
                if state.get('status') in ('completed', 'failed'):
                    self._cleanup_checkpoint(state, save)
                    if state['status'] == 'failed':
                        raise TranscriptionError('Saved Tencent task failed; inspect the checkpoint before explicitly starting another billable task')
                    return self._parse_result(state['result'], params['EngineModelType'], request.diarize)
                if state.get('status') == 'submitting':
                    raise TranscriptionError('Tencent submission outcome is uncertain. Check the ASR console and saved checkpoint; automatic resubmission is blocked to prevent duplicate charges')
                with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False) as client:
                    task_id = state.get('task_id')
                    if task_id:
                        if time.time() - state['submitted_at'] >= 23 * 3600:
                            raise TranscriptionError('Saved Tencent task is nearing its 24-hour expiry; automatic resubmission is blocked. Check the ASR console')
                        logger.info('Resuming Tencent ASR task: %s', task_id)
                    else:
                        # A previous interrupted upload has no ASR task yet.
                        self._cleanup_checkpoint(state, save)
                        if state.get('staging_locator'):
                            raise TranscriptionError('Previous Tencent staging cleanup is pending; check object storage before retrying')
                        state.update(status='preparing')
                        save()
                        if size <= INLINE_LIMIT:
                            params.update(SourceType=1, Data=base64.b64encode(path.read_bytes()).decode(), DataLen=size)
                        else:
                            _, _, url = self._stage(str(path), request, state, save)
                            params.update(SourceType=0, Url=url)
                        state.update(status='submitting', submitted_at=time.time())
                        save()  # Crash after this point must not silently submit twice.
                        try:
                            result = self._call(client, 'CreateRecTask', params)
                        except ProviderError as exc:
                            if exc.status_code and 400 <= exc.status_code < 500:
                                state['status'] = 'rejected'
                                save()
                                self._cleanup_checkpoint(state, save)
                            raise
                        except httpx.HTTPError:
                            raise TranscriptionError('Tencent ASR network error. Submission was not retried; its outcome is uncertain. Check the ASR console') from None
                        task_id = result.get('TaskId')
                        if not isinstance(task_id, int) or isinstance(task_id, bool):
                            raise ProviderError('Tencent submit returned no task ID; automatic resubmission is blocked', provider=self.PROVIDER_NAME)
                        state.update(status='pending', task_id=task_id)
                        save()
                        logger.info('Tencent ASR task submitted: %s', task_id)
                    result = self._poll(client, task_id)
                    state.update(status='completed' if result['Status'] == 2 else 'failed', result=result)
                    save()  # Keep result before deleting the only cloud audio copy.
                    self._cleanup_checkpoint(state, save)
                    if result['Status'] == 3:
                        raise TranscriptionError(f'Tencent task {task_id} failed; check the ASR console for details')
                    return self._parse_result(result, params['EngineModelType'], request.diarize)

    def _parse_result(self, result, model, diarize):
        segments = []
        for item in result.get('ResultDetail') or []:
            text = (item.get('FinalSentence') or '').strip()
            if not text:
                continue
            speaker_id = item.get('SpeakerId')
            speaker = f'SPEAKER_{speaker_id:02d}' if diarize and isinstance(speaker_id, int) and speaker_id >= 0 else None
            segments.append(TranscriptionSegment(text=text, speaker=speaker,
                            start_time=item['StartMs'] / 1000 if item.get('StartMs') is not None else None,
                            end_time=item['EndMs'] / 1000 if item.get('EndMs') is not None else None))
        return TranscriptionResponse(
            text='\n'.join(s.text for s in segments) if segments else (result.get('Result') or ''),
            segments=segments or None, speakers=sorted({s.speaker for s in segments if s.speaker}) or None,
            duration=result.get('AudioDuration'), provider=self.PROVIDER_NAME, model=model,
            raw_response=result)

    def list_models(self):
        return [{'id': model} for model in MODELS]

    @classmethod
    def get_config_schema(cls):
        return {'type': 'object', 'required': ['secret_id', 'secret_key'], 'properties': {
            'secret_id': {'type': 'string'}, 'secret_key': {'type': 'string', 'writeOnly': True},
            'model': {'type': 'string', 'enum': list(MODELS), 'default': MODELS[0]},
            'hotword_id': {'type': 'string'}, 'timeout': {'type': 'number', 'default': 1800}}}
