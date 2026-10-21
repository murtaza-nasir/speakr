"""Tencent Cloud recording-file ASR (TC3 signed submit-and-poll API)."""

import base64
import hashlib
import hmac
import io
import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from ..base import (BaseTranscriptionConnector, ConnectorSpecifications,
                    TranscriptionCapability, TranscriptionResponse, TranscriptionSegment)
from ..exceptions import ConfigurationError, ProviderError, TranscriptionError

logger = logging.getLogger(__name__)
HOST = 'asr.tencentcloudapi.com'
INLINE_LIMIT = 5 * 1024 * 1024
FILE_LIMIT = 1024 * 1024 * 1024
MODELS = ('16k_zh_en_meeting', '16k_zh_en_2.0')


class TencentASRConnector(BaseTranscriptionConnector):
    PROVIDER_NAME = 'tencent_asr'
    CAPABILITIES = {TranscriptionCapability.DIARIZATION,
                    TranscriptionCapability.TIMESTAMPS, TranscriptionCapability.HOTWORDS}
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=FILE_LIMIT, max_duration_seconds=18000,
        handles_chunking_internally=True)

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
                                    status_code=429 if code.startswith('RequestLimitExceeded') else 400)
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
        if request.hotwords:
            words = list(dict.fromkeys(w.strip() for w in request.hotwords.split(',') if w.strip()))
            if len(words) > 128 or any(len(w) > 30 or len(w.encode('utf-8')) > 30 or '|' in w for w in words):
                raise ConfigurationError('Tencent hotwords: at most 128 words, 30 UTF-8 bytes per word; no | characters')
            if words:
                params['HotwordList'] = ','.join(w + '|5' for w in words)
        elif self.hotword_id:
            params['HotwordId'] = self.hotword_id
        return params

    def _stage(self, path, request):
        from src.services.storage import get_storage_service, parse_locator
        storage = get_storage_service()
        if not storage.s3:
            raise ConfigurationError('Tencent audio over 5 MiB requires Speakr S3 storage with a publicly reachable endpoint')
        key = 'tencent-asr/' + uuid4().hex + Path(request.filename).suffix
        locator = parse_locator(storage.s3.build_locator(key))
        try:
            storage.s3.upload_local_file(path, key, content_type=request.mime_type or 'application/octet-stream')
            url = storage.s3.presign_get_url(locator, expires_seconds=86400)
            return storage.s3, locator, url
        except Exception:
            self._cleanup(storage.s3, locator)
            raise TranscriptionError('Tencent ASR staging failed; check S3 configuration and permissions') from None

    @staticmethod
    def _cleanup(storage, locator):
        try:
            storage.delete(locator, missing_ok=True)
        except Exception:
            logger.warning('Tencent ASR staging cleanup failed; configure a tencent-asr/ bucket lifecycle rule')

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
                                 'a new transcription submits a new billable task.')

    def transcribe(self, request):
        params = self._parameters(request)
        storage = locator = None
        submitted = terminal = False
        try:
            try:
                request.audio_file.seek(0)
            except (AttributeError, OSError, io.UnsupportedOperation):
                pass
            data = request.audio_file.read(INLINE_LIMIT + 1)
            if not data:
                raise TranscriptionError('Tencent ASR: empty audio file')
            if len(data) <= INLINE_LIMIT:
                params.update(SourceType=1, Data=base64.b64encode(data).decode(), DataLen=len(data))
            else:
                with tempfile.TemporaryDirectory(prefix='speakr_tencent_') as directory:
                    path = str(Path(directory) / 'audio')
                    size = len(data)
                    with open(path, 'wb') as output:
                        output.write(data)
                        while True:
                            chunk = request.audio_file.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > FILE_LIMIT:
                                raise TranscriptionError('Tencent ASR: audio exceeds 1 GiB URL limit')
                            output.write(chunk)
                    storage, locator, url = self._stage(path, request)
                params.update(SourceType=0, Url=url)
            with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False) as client:
                # Mark before POST: a transport failure can occur after acceptance.
                submitted = True
                try:
                    result = self._call(client, 'CreateRecTask', params)
                except ProviderError as exc:
                    # A definite rejection cannot be downloading the staged object.
                    terminal = bool(exc.status_code and 400 <= exc.status_code < 500)
                    raise
                task_id = result.get('TaskId')
                if not isinstance(task_id, int) or isinstance(task_id, bool):
                    raise ProviderError('Tencent submit returned no task ID', provider=self.PROVIDER_NAME)
                logger.info('Tencent ASR task submitted: %s', task_id)
                result = self._poll(client, task_id)
                terminal = True
                if result['Status'] == 3:
                    raise TranscriptionError(f'Tencent task {task_id} failed; check the ASR console for details')
                return self._parse_result(result, params['EngineModelType'], request.diarize)
        except httpx.HTTPError:
            raise TranscriptionError('Tencent ASR network error. Submission was not retried: check the ASR console '
                                     'before retrying to avoid duplicate charges.') from None
        finally:
            # On timeout or ambiguous POST failure, Tencent may still need the URL.
            if storage and (terminal or not submitted):
                self._cleanup(storage, locator)

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
