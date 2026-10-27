"""
ASR Endpoint connector for custom self-hosted ASR services.

Supports whisper-asr-webservice, WhisperX, and other compatible ASR services
that expose a /asr endpoint. A WhisperX ASR service that advertises the
asynchronous jobs API in /health (0.5.0 and later) is used through POST /jobs.
"""

import email.utils
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx
from typing import Dict, Any, Set, Optional

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError, ProviderError
from src.config.app_config import ASR_ENABLE_CHUNKING, ASR_MAX_DURATION_SECONDS
from src.utils.error_body import summarize_error_body
from src.utils.log_filters import loggable_params
from src.services.job_control import JobCanceled

from src.utils.error_text import describe_body, log_failure
logger = logging.getLogger(__name__)


class ASREndpointConnector(BaseTranscriptionConnector):
    """Connector for custom ASR webservice (whisper-asr-webservice, WhisperX, etc.)."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.LANGUAGE_SELECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,  # Supports min/max speakers
        TranscriptionCapability.HOTWORDS,
        TranscriptionCapability.INITIAL_PROMPT,
    }
    PROVIDER_NAME = "asr_endpoint"
    # A job of the WhisperX jobs API is saved on the processing job, so a
    # retry or a restart polls it instead of submitting the audio again.
    SUPPORTS_TASK_RESUME = True

    # SPECIFICATIONS is set dynamically in __init__ based on ASR_ENABLE_CHUNKING config
    # Default values here for class-level reference (overridden per-instance)
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the ASR Endpoint connector.

        Args:
            config: Configuration dict with keys:
                - base_url: ASR service base URL (required)
                - timeout: Request timeout in seconds (default: 1800)
                - return_speaker_embeddings: Whether to request embeddings (default: False)
                - diarize: Whether to enable diarization by default (default: True)
        """
        super().__init__(config)

        self.base_url = config['base_url'].rstrip('/')
        self._config_timeout = config.get('timeout', 1800)  # 30 minutes default
        self.return_embeddings = config.get('return_speaker_embeddings', False)
        self.default_diarize = config.get('diarize', True)

        # Configure chunking behavior based on environment variables
        # ASR_ENABLE_CHUNKING=true enables app-level chunking for self-hosted ASR services
        # that may crash on long files due to GPU memory exhaustion
        if ASR_ENABLE_CHUNKING:
            # Calculate recommended chunk size (80% of max for safety margin)
            recommended_chunk = int(ASR_MAX_DURATION_SECONDS * 0.8)
            self.SPECIFICATIONS = ConnectorSpecifications(
                max_file_size_bytes=None,  # No file size limit
                max_duration_seconds=ASR_MAX_DURATION_SECONDS,
                handles_chunking_internally=False,  # App handles chunking
                recommended_chunk_seconds=recommended_chunk,
            )
            logger.info(
                f"ASR chunking enabled: max_duration={ASR_MAX_DURATION_SECONDS}s, "
                f"recommended_chunk={recommended_chunk}s"
            )
        else:
            # Default behavior: ASR service handles everything internally
            self.SPECIFICATIONS = ConnectorSpecifications(
                max_file_size_bytes=None,
                max_duration_seconds=None,
                handles_chunking_internally=True,
            )

        # Add speaker embeddings capability if enabled
        if self.return_embeddings:
            self.CAPABILITIES = self.CAPABILITIES | {TranscriptionCapability.SPEAKER_EMBEDDINGS}

    @property
    def timeout(self):
        """Get ASR timeout, reading fresh from env/DB each time to respect runtime changes."""
        # Environment variables take priority
        env_timeout = os.environ.get('ASR_TIMEOUT') or os.environ.get('asr_timeout_seconds')
        if env_timeout:
            try:
                return int(env_timeout)
            except (ValueError, TypeError):
                pass

        # Try database setting (Admin UI)
        try:
            from src.models import SystemSetting
            db_timeout = SystemSetting.get_setting('asr_timeout_seconds', None)
            if db_timeout is not None:
                return int(db_timeout)
        except Exception:
            pass

        # Fall back to config value from initialization
        return self._config_timeout

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('base_url'):
            raise ConfigurationError("base_url is required for ASR endpoint connector")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using ASR webservice.

        The asynchronous jobs API of the WhisperX ASR service (POST /jobs) is
        used when the service advertises it in /health and the asr_async_jobs
        setting is on; every other service gets the synchronous /asr request.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with segments and speaker information
        """
        params = self._build_params(request)
        if self._async_jobs_enabled():
            capabilities = self._job_capabilities()
            if capabilities is not None:
                try:
                    return self._transcribe_async(request, params, capabilities)
                except _JobsUnavailable as e:
                    logger.info(f"ASR asynchronous jobs are unavailable ({e}); using the synchronous endpoint")
        if request.provider_task and request.save_provider_task:
            # A job saved while the jobs API was in use is not resumed by the
            # synchronous path, so it is removed from the service. It stays
            # saved while the service cannot confirm that, and is removed
            # when the processing job ends.
            if self._remove_saved_job(request.provider_task):
                request.save_provider_task(None)
        _rewind(request)
        return self._transcribe_sync(request, params)

    def _build_params(self, request: TranscriptionRequest) -> Dict[str, Any]:
        """Query parameters shared by /asr and POST /jobs."""
        params = {
            'encode': True,
            'task': 'transcribe',
            'output': 'json'
        }

        if request.language:
            params['language'] = request.language
            logger.info(f"Using transcription language: {request.language}")

        # Determine if we should diarize
        should_diarize = request.diarize if request.diarize is not None else self.default_diarize

        # Send both parameter names for compatibility:
        # - 'diarize' is used by whisper-asr-webservice
        # - 'enable_diarization' is used by WhisperX
        params['diarize'] = should_diarize
        params['enable_diarization'] = should_diarize

        if should_diarize and self.return_embeddings:
            params['return_speaker_embeddings'] = True

        if request.min_speakers:
            params['min_speakers'] = request.min_speakers
        if request.max_speakers:
            params['max_speakers'] = request.max_speakers

        if request.prompt:
            params['initial_prompt'] = request.prompt
        if request.hotwords:
            params['hotwords'] = request.hotwords

        # Per-request model override (issue #266). The whisperx-asr-service
        # fork accepts a `model` query parameter (e.g. large-v3,
        # distil-medium.en) and switches the loaded Whisper model
        # on demand. The upstream onerahmet/whisper-asr-webservice
        # ignores unknown params, so this is safe in both cases.
        # Never an empty or whitespace name: the service would take it
        # literally instead of using its own default model.
        model = self._effective_model(request).strip()
        if model:
            params['model'] = model
            logger.info(f"Using per-request model override: {model}")
        return params

    def _transcribe_sync(self, request: TranscriptionRequest, params: Dict[str, Any]) -> TranscriptionResponse:
        """One synchronous POST /asr request."""
        try:
            url = f"{self.base_url}/asr"

            content_type = request.mime_type or 'application/octet-stream'
            files = {
                'audio_file': (request.filename, request.audio_file, content_type)
            }

            # Configure timeout: generous values for large file uploads
            # Write timeout needs to be high too - large files take time to upload
            timeout = httpx.Timeout(
                None,
                connect=60.0,
                read=float(self.timeout),
                write=float(self.timeout),
                pool=None
            )

            logger.info(f"Sending ASR request to {url} with params: {loggable_params(params)} (timeout: {self.timeout}s)")
            logger.debug(f"ASR request params in full: {params}")

            with self._http_client() as client:
                response = client.post(url, params=params, files=files, timeout=timeout)
                logger.info(f"ASR request completed with status: {response.status_code}")
                response.raise_for_status()

                # Parse the JSON response
                response_text = response.text
                try:
                    data = response.json()
                except Exception as json_err:
                    if response_text.strip().startswith('<'):
                        logger.error(f"ASR returned HTML error page (status {response.status_code})")
                        raise ProviderError(
                            f"ASR service returned HTML error page",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )
                    else:
                        raise ProviderError(
                            f"ASR service returned invalid response: {json_err}",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )

            return self._parse_response(data)

        except httpx.HTTPStatusError as e:
            # Capture the upstream response body so the actual error message
            # (e.g. faster-whisper's "Invalid model size 'whisper-tiny'") is
            # preserved instead of being collapsed to just a status code.
            try:
                body = e.response.text
            except Exception:
                body = ''
            # Keep both ends: a worker traceback names the exception on its
            # last line, and a cut from the start threw exactly that away.
            # A gateway's HTML page is reduced to its text and anything that
            # looks like a credential is redacted: this text reaches the log,
            # the failure webhook and email, and the technical details (#593).
            body_excerpt = summarize_error_body(body, 800)
            # The body can quote the prompt or the filename: its status and
            # length at ERROR, its text at DEBUG.
            _ext = os.path.splitext(request.filename or '')[1].lower() or 'none'
            logger.error(f"ASR request failed (file extension {_ext}): "
                         f"{describe_body(e.response.status_code, body)}")
            logger.debug(
                f"ASR request for {request.filename!r} failed with status "
                f"{e.response.status_code}: {body_excerpt}"
            )
            detail = (
                f"ASR request failed with status {e.response.status_code}: {body_excerpt}"
                if body_excerpt
                else f"ASR request failed with status {e.response.status_code}"
            )
            raise ProviderError(
                detail,
                provider=self.PROVIDER_NAME,
                status_code=e.response.status_code
            ) from e

        except httpx.TimeoutException as e:
            logger.error(f"ASR request timed out after {self.timeout}s")
            raise TranscriptionError(f"ASR request timed out after {self.timeout}s") from e

        except Exception as e:
            error_msg = str(e)
            log_failure(logger, logging.ERROR, "ASR transcription failed", e)
            raise TranscriptionError(f"ASR transcription failed: {error_msg}") from e

    # -- Asynchronous jobs (WhisperX ASR service 0.5.0 and later) -----------

    def _http_client(self, **kwargs) -> httpx.Client:
        return httpx.Client(**kwargs)

    def _async_jobs_enabled(self) -> bool:
        """The asr_async_jobs setting (ASR_ASYNC_JOBS in .env, default on)."""
        from src.utils.system_settings import env_fallback
        fallback = env_fallback('ASR_ASYNC_JOBS', True)
        try:
            from flask import has_app_context
            if has_app_context():
                from src.models import SystemSetting
                return bool(SystemSetting.get_setting('asr_async_jobs', fallback))
        except Exception:
            pass
        return fallback

    def _job_capabilities(self) -> Optional[Dict[str, Any]]:
        """The service's job capabilities, or None when it has no jobs API.

        Read from the `capabilities` object of GET /health, or of GET / when
        /health gives no JSON. Services before 0.5.0 report no such object.
        """
        try:
            with self._http_client(timeout=HEALTH_TIMEOUT_SECONDS) as client:
                for path in ('/health', '/'):
                    try:
                        response = client.get(f"{self.base_url}{path}")
                        data = response.json()
                    except (httpx.HTTPError, ValueError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    capabilities = data.get('capabilities')
                    version = capabilities.get('jobs_api_version') if isinstance(capabilities, dict) else None
                    if (isinstance(capabilities, dict) and capabilities.get('async_jobs') is True
                            and type(version) is int and version == JOBS_API_VERSION):
                        return capabilities
                    return None
        except Exception as e:
            logger.debug(f"ASR capability check failed: {e}")
        return None

    @staticmethod
    def _idempotency_key(request: TranscriptionRequest, params: Dict[str, Any]) -> Optional[str]:
        """Idempotency-Key for POST /jobs: the processing job, the audio and the parameters.

        The task key names what is transcribed (a processing job and its
        audio bytes, or a chunk, an incognito upload or the voice check and
        its audio bytes) and is the same on every retry of it; the digest of
        the parameters changes the key when the parameters change.
        """
        if not request.task_key:
            return None
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]
        return f"{request.task_key}-{digest}"

    def _transcribe_async(self, request: TranscriptionRequest, params: Dict[str, Any],
                          capabilities: Dict[str, Any]) -> TranscriptionResponse:
        """Submit a job (or resume the saved one), poll it and fetch the result.

        A request without save_provider_task (a chunk, incognito mode, the
        voice check) has nothing that could resume its job or remove it
        later, so the job is deleted on the service on every failure.
        """
        resumable = request.save_provider_task is not None
        save = request.save_provider_task or (lambda task: None)
        job_params = {k: v for k, v in params.items() if k not in ('output', 'encode')}
        headers = {}
        key = self._idempotency_key(request, job_params)
        if key and capabilities.get('idempotency_key') is not False:
            headers['Idempotency-Key'] = key
        deadline = _now() + float(self.timeout)

        saved = request.provider_task if isinstance(request.provider_task, dict) else {}
        job_id = None
        if _valid_job_id(saved.get('job_id')) and saved.get('base_url') == self.base_url:
            job_id = saved['job_id']
            logger.info(f"ASR job {job_id}: resuming the saved job")
        resubmits_left = 1

        try:
            with self._http_client() as client:
                try:
                    while True:
                        if job_id is None:
                            job_id = self._submit_job(client, request, job_params, headers, deadline)
                            if save({'job_id': job_id, 'base_url': self.base_url}) is False:
                                # Nothing would remove this job later.
                                gone_id, job_id = job_id, None
                                self._delete_job(client, gone_id)
                                raise TranscriptionError(_PROCESSING_JOB_GONE.format(job_id=gone_id))
                        try:
                            data = self._wait_for_result(client, job_id, request, deadline)
                            break
                        except _RemoteJobGone as gone:
                            # From here the job is either unknown to the
                            # service or deleted below.
                            gone_id, job_id = job_id, None
                            if gone.still_stored:
                                self._delete_job(client, gone_id)
                            if save(None) is False:
                                # The processing job was deleted (with its
                                # recording, or on its own): nothing is submitted.
                                raise TranscriptionError(_PROCESSING_JOB_GONE.format(job_id=gone_id)) from None
                            if resubmits_left <= 0:
                                raise ProviderError(
                                    f"ASR job {gone_id} was lost on the service ({gone}) after a new submit",
                                    provider=self.PROVIDER_NAME) from None
                            resubmits_left -= 1
                            logger.warning(f"ASR job {gone_id}: {gone}; the audio is submitted again")
                        except RemoteJobFailed:
                            # The service does not reuse a failed or cancelled
                            # job, so a retry submits again and this one is
                            # removed from the service.
                            failed_id, job_id = job_id, None
                            self._delete_job(client, failed_id)
                            save(None)
                            raise

                    response = self._parse_response(data)
                except BaseException as stop:
                    # A timeout, failed polls, an unreadable result or a
                    # shutdown: a saved job is left for the retry that
                    # resumes it. A job nothing saved is removed now, and so
                    # is the job of a canceled processing job: no retry
                    # follows a cancel.
                    canceled = isinstance(stop, JobCanceled)
                    if job_id is not None and (canceled or not resumable):
                        reason = "the processing job was canceled" if canceled else "nothing can resume it"
                        logger.info(f"ASR job {job_id}: deleted on the service, {reason}")
                        if self._delete_job(client, job_id) and canceled:
                            # Confirmed gone: nothing is left for the queue
                            # to remove. Unconfirmed, the saved task stays
                            # and the queue tries once more.
                            save(None)
                    raise
                self._delete_job(client, job_id)
                save(None)
                return response

        except (_JobsUnavailable, TranscriptionError):
            raise
        except httpx.TimeoutException as e:
            logger.error(f"ASR job request timed out: {e}")
            raise TranscriptionError(f"ASR job request timed out after {self.timeout}s") from e
        except Exception as e:
            log_failure(logger, logging.ERROR, "ASR transcription failed", e)
            raise TranscriptionError(f"ASR transcription failed: {e}") from e

    def _upload_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(None, connect=60.0, read=float(self.timeout), write=float(self.timeout), pool=None)

    def _submit_job(self, client, request: TranscriptionRequest, params: Dict[str, Any],
                    headers: Dict[str, str], deadline: float) -> str:
        """POST /jobs; waits out a busy answer (413 or 503 with Retry-After)."""
        content_type = request.mime_type or 'application/octet-stream'
        while True:
            if request.should_cancel and request.should_cancel():
                raise JobCanceled(None)
            _rewind(request)
            files = {'audio_file': (request.filename, request.audio_file, content_type)}
            logger.info(f"Submitting ASR job to {self.base_url}/jobs with params: {loggable_params(params)}")
            logger.debug(f"ASR job params in full: {params}")
            response = client.post(f"{self.base_url}/jobs", params=params, files=files,
                                   headers=headers, timeout=self._upload_timeout())
            status = response.status_code
            if status in (200, 202):
                data = _json_body(response)
                job_id = data.get('job_id') if isinstance(data, dict) else None
                if not _valid_job_id(job_id):
                    raise ProviderError("ASR job submit returned no valid job id",
                                        provider=self.PROVIDER_NAME, status_code=status)
                logger.info(f"ASR job {job_id} accepted (status {data.get('status')}, "
                            f"deduplicated={bool(data.get('deduplicated'))})")
                return job_id
            if status in (404, 405):
                raise _JobsUnavailable(f"POST /jobs answered {status}")
            retry_after = _retry_after(response)
            if status in (413, 503) and retry_after is not None:
                wait = min(retry_after, MAX_BUSY_WAIT_SECONDS)
                if _now() + wait > deadline:
                    raise ProviderError(
                        f"ASR service busy: the job submit was refused (HTTP {status}, "
                        f"Retry-After {retry_after:.0f}s) until the timeout of {self.timeout}s",
                        provider=self.PROVIDER_NAME, status_code=status)
                logger.info(f"ASR service busy (HTTP {status}); the job is submitted again in {wait:.0f}s")
                _sleep(wait)
                continue
            self._raise_for_status(response, "ASR job submit")

    def _wait_for_result(self, client, job_id: str, request: TranscriptionRequest,
                         deadline: float) -> Dict[str, Any]:
        """Poll GET /jobs/{id} at the pace the service asks for, then fetch the result."""
        state: Dict[str, Any] = {}
        failures = 0
        while True:
            # A safe point: the owner canceled the processing job. The caller
            # removes the job from the service.
            if request.should_cancel and request.should_cancel():
                raise JobCanceled(job_id)
            response = None
            try:
                response = client.get(f"{self.base_url}/jobs/{job_id}", timeout=POLL_REQUEST_TIMEOUT_SECONDS)
            except httpx.TransportError as e:
                failure = str(e) or type(e).__name__
            else:
                failure = None
                if response.status_code == 404:
                    raise _RemoteJobGone("the job is not known to the service")
                if response.status_code >= 500:
                    failure = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    self._raise_for_status(response, f"ASR job {job_id} poll")

            if failure is not None:
                # A dropped connection or a restarting service: the job is
                # kept, and a retry of the Speakr job resumes it.
                failures += 1
                if failures > MAX_POLL_FAILURES:
                    raise TranscriptionError(f"ASR job {job_id} could not be polled: {failure}")
                wait = POLL_DEFAULT_SECONDS
                status = 'unreachable'
            else:
                failures = 0
                data = _json_body(response)
                if not isinstance(data, dict):
                    raise ProviderError(f"ASR job {job_id} poll returned no job state",
                                        provider=self.PROVIDER_NAME, status_code=response.status_code)
                status = data.get('status')
                self._note_progress(job_id, data, request, state)
                if status == 'done':
                    result = self._fetch_result(client, job_id)
                    if result is not None:
                        return result
                elif status == 'error':
                    raise _job_error(job_id, data.get('error'))
                elif status == 'cancelled':
                    raise RemoteJobFailed(f"ASR job {job_id} was cancelled on the service", permanent=True,
                                          provider=self.PROVIDER_NAME)
                elif status == 'interrupted':
                    raise _RemoteJobGone("the service restarted while the job ran", still_stored=True)
                elif status not in ('queued', 'running'):
                    raise ProviderError(f"ASR job {job_id} reported an unknown status {status!r}",
                                        provider=self.PROVIDER_NAME)
                wait = _poll_wait(data, response)

            if _now() + wait > deadline:
                outlook = ("a retry resumes the same job" if request.save_provider_task is not None
                           else "the job is deleted on the service")
                raise TranscriptionError(
                    f"ASR job {job_id} timed out after {self.timeout}s (status {status}); {outlook}")
            _sleep(wait)

    def _fetch_result(self, client, job_id: str) -> Optional[Dict[str, Any]]:
        """GET /jobs/{id}/result as JSON; None while the job is unfinished."""
        timeout = httpx.Timeout(None, connect=60.0, read=float(self.timeout), write=60.0, pool=None)
        response = client.get(f"{self.base_url}/jobs/{job_id}/result",
                              params={'output_format': 'json'}, timeout=timeout)
        if response.status_code == 202:
            return None
        if response.status_code == 404:
            raise _RemoteJobGone("the result has expired or is not known to the service")
        if response.status_code == 409:
            raise RemoteJobFailed(f"ASR job {job_id} ended without a result", permanent=False,
                                  provider=self.PROVIDER_NAME)
        if response.status_code != 200:
            self._raise_for_status(response, f"ASR job {job_id} result")
        data = _json_body(response)
        if not isinstance(data, dict):
            raise ProviderError(f"ASR job {job_id} returned an invalid result",
                                provider=self.PROVIDER_NAME, status_code=response.status_code)
        return data

    def _delete_job(self, client, job_id: str) -> bool:
        """DELETE /jobs/{id}: the job and its transcript leave the service.

        Sent once the result is fetched and when the job has ended for good
        without one. Best effort: a failure is logged and never raised.
        Returns whether the service confirmed it (200, or 404 for a job it no
        longer knows).
        """
        try:
            response = client.delete(f"{self.base_url}/jobs/{job_id}", timeout=CANCEL_TIMEOUT_SECONDS)
            if response.status_code in (200, 404):
                return True
            logger.warning(f"ASR job {job_id}: DELETE answered {response.status_code}")
        except Exception as e:
            logger.warning(f"ASR job {job_id} could not be deleted on the service: {e}")
        return False

    def _remove_saved_job(self, saved: Any) -> bool:
        """Remove a saved job that will not be polled again; True when nothing is left to remove."""
        if not isinstance(saved, dict) or saved.get('base_url') != self.base_url \
                or not _valid_job_id(saved.get('job_id')):
            return True
        try:
            with self._http_client(timeout=CANCEL_TIMEOUT_SECONDS) as client:
                return self._delete_job(client, saved['job_id'])
        except Exception as e:
            logger.warning(f"ASR job {saved['job_id']} could not be deleted on the service: {e}")
            return False

    @staticmethod
    def _note_progress(job_id: str, data: Dict[str, Any], request: TranscriptionRequest,
                       state: Dict[str, Any]) -> None:
        """Log stage changes; store the stage and a measured fraction on the processing job.

        A fraction the service marks as estimated (fraction_estimated) is
        logged and not stored: Speakr shows a number only where one was
        measured, and a moving bar with the elapsed time otherwise.
        """
        progress = data.get('progress') if isinstance(data.get('progress'), dict) else {}
        stage = progress.get('stage')
        fraction = progress.get('fraction')
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            fraction = None
        estimated = bool(progress.get('fraction_estimated'))
        status = data.get('status')
        if state.get('stage') != (status, stage):
            state['stage'] = (status, stage)
            detail = f", stage {stage}" if stage else ""
            if fraction is not None:
                detail += f", {fraction:.0%}{' (estimated)' if estimated else ''}"
            logger.info(f"ASR job {job_id}: {status}{detail}")
            if request.report_stage:
                request.report_stage(stage_word(status, stage))
        if request.report_progress:
            done = None if fraction is None or estimated else max(0, min(100, int(round(fraction * 100))))
            if state.get('done') != done:
                state['done'] = done
                if done is None:
                    request.report_progress(None, None)
                else:
                    request.report_progress(done, 100)

    def _raise_for_status(self, response: httpx.Response, what: str) -> None:
        body_excerpt = summarize_error_body(response.text or '', 800)
        logger.error(f"{what} failed: {describe_body(response.status_code, response.text)}")
        logger.debug(f"{what} failed with status {response.status_code}: {body_excerpt}")
        detail = (f"{what} failed with status {response.status_code}: {body_excerpt}"
                  if body_excerpt else f"{what} failed with status {response.status_code}")
        raise ProviderError(detail, provider=self.PROVIDER_NAME, status_code=response.status_code)

    def _parse_response(self, data: Dict[str, Any]) -> TranscriptionResponse:
        """
        Parse ASR webservice response into standardized format.

        The ASR response contains:
        - text: Full transcription text
        - language: Detected language
        - segments: Array of segments with speaker, text, start, end
        - speaker_embeddings: Optional speaker embeddings (WhisperX only)
        """
        segments = []
        speakers = set()
        full_text_parts = []
        last_speaker = None

        logger.info(f"ASR response keys: {list(data.keys())}")

        if 'segments' in data and isinstance(data['segments'], list):
            logger.info(f"Number of segments: {len(data['segments'])}")

            for seg in data['segments']:
                speaker = seg.get('speaker')

                # Handle missing speakers by carrying forward from previous segment
                if speaker is None:
                    if last_speaker is not None:
                        speaker = last_speaker
                    else:
                        speaker = 'UNKNOWN_SPEAKER'
                else:
                    last_speaker = speaker

                text = seg.get('text', '').strip()
                speakers.add(speaker)
                full_text_parts.append(f"[{speaker}]: {text}")

                segments.append(TranscriptionSegment(
                    text=text,
                    speaker=speaker,
                    start_time=seg.get('start'),
                    end_time=seg.get('end')
                ))

        # Get the full text
        if 'text' in data and isinstance(data['text'], str):
            full_text = data['text']
        elif full_text_parts:
            full_text = '\n'.join(full_text_parts)
        else:
            full_text = ''

        # Extract speaker embeddings if present
        speaker_embeddings = data.get('speaker_embeddings')
        if speaker_embeddings:
            logger.info(f"Received speaker embeddings for speakers: {list(speaker_embeddings.keys())}")

        logger.info(f"Parsed {len(segments)} segments with {len(speakers)} unique speakers: {sorted(speakers)}")

        return TranscriptionResponse(
            text=full_text,
            segments=segments,
            speakers=sorted(list(speakers)),
            speaker_embeddings=speaker_embeddings,
            language=data.get('language'),
            provider=self.PROVIDER_NAME,
            model="asr-endpoint",
            raw_response=data
        )

    def list_models(self):
        """Probe the ASR service's /v1/models endpoint and return the list.

        The whisperx-asr-service fork (and any OpenAI-compatible variant)
        exposes /v1/models. The upstream onerahmet/whisper-asr-webservice does
        not, in which case we return an empty list.
        """
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{self.base_url}/v1/models")
                if resp.status_code != 200:
                    return []
                data = resp.json().get('data', [])
                return [
                    {
                        'id': m.get('id'),
                        'label': m.get('id'),
                        'owned_by': m.get('owned_by', 'unknown'),
                    }
                    for m in data if m.get('id')
                ]
        except Exception as e:
            logger.warning(f"asr_endpoint /v1/models probe failed: {e}")
            return []

    def health_check(self) -> bool:
        """Check if ASR endpoint is reachable."""
        try:
            with httpx.Client(timeout=10.0) as client:
                # Try common health check endpoints
                for endpoint in ['/health', '/']:
                    try:
                        response = client.get(f"{self.base_url}{endpoint}")
                        if response.status_code < 500:
                            return True
                    except Exception:
                        continue
                return False
        except Exception:
            return False

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "ASR service base URL (e.g., http://whisper-asr:9000)"
                },
                "timeout": {
                    "type": "integer",
                    "default": 1800,
                    "description": "Request timeout in seconds"
                },
                "diarize": {
                    "type": "boolean",
                    "default": True,
                    "description": "Enable speaker diarization by default"
                },
                "return_speaker_embeddings": {
                    "type": "boolean",
                    "default": False,
                    "description": "Request speaker embeddings (WhisperX only)"
                }
            }
        }


# Asynchronous jobs of the WhisperX ASR service (POST /jobs, 0.5.0 and later).
JOBS_API_VERSION = 1
HEALTH_TIMEOUT_SECONDS = 10.0
POLL_REQUEST_TIMEOUT_SECONDS = 30.0
CANCEL_TIMEOUT_SECONDS = 5.0
# poll_after_seconds from the service is followed within these bounds.
POLL_MIN_SECONDS = 2.0
POLL_MAX_SECONDS = 60.0
POLL_DEFAULT_SECONDS = 10.0
# Longest single wait on a busy answer (Retry-After) before the next submit.
MAX_BUSY_WAIT_SECONDS = 600.0
# Polls in a row that may fail (connection, 5xx) before the attempt ends.
MAX_POLL_FAILURES = 5
# Error codes the service marks as not retryable, for an error object
# without the retryable field.
NON_RETRYABLE_CODES = frozenset({'audio_too_long', 'file_too_large', 'decode_failed', 'invalid_request'})

_JOB_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

# Replaced in tests.
_sleep = time.sleep
_now = time.monotonic


class RemoteJobFailed(ProviderError):
    """An ASR service job ended in error or was cancelled.

    ``permanent`` tells the job queue whether a retry can help: a job refused
    for its audio (audio_too_long, file_too_large) fails at once, and one that
    ran out of memory or found the service unavailable is retried.
    """

    def __init__(self, message: str, permanent: bool, code: Optional[str] = None, provider: Optional[str] = None):
        super().__init__(message, provider=provider)
        self.permanent = permanent
        self.code = code


class _JobsUnavailable(Exception):
    """The service has no jobs API after all; the synchronous endpoint is used."""


class _RemoteJobGone(Exception):
    """The job is unknown to the service, its result has expired, or the service restarted.

    ``still_stored``: the service still lists the job (an interrupted one),
    so it is deleted there before the audio is submitted again.
    """

    def __init__(self, message: str, still_stored: bool = False):
        super().__init__(message)
        self.still_stored = still_stored


# The service's stage names in Speakr's words. Names are matched by what they
# contain, so "whisper", "transcribe" and "transcription" are one stage; a
# name that matches nothing is not shown.
_STAGE_WORDS = (
    (('diar', 'speaker', 'embed'), 'diarizing'),
    (('align',), 'aligning'),
    (('whisper', 'transcri', 'asr', 'decod'), 'transcribing'),
    (('load', 'prepar', 'convert', 'preprocess', 'vad', 'download'), 'preparing'),
)


def stage_word(status: Any, stage: Any) -> Optional[str]:
    """Speakr's word for what the service reports: waiting, preparing,
    transcribing, aligning or diarizing; None for a stage it does not name."""
    if status == 'queued':
        return 'waiting'
    if not isinstance(stage, str) or not stage:
        return None
    name = stage.lower()
    for needles, word in _STAGE_WORDS:
        if any(needle in name for needle in needles):
            return word
    return None


_PROCESSING_JOB_GONE = "ASR job {job_id} was removed from the service: its processing job no longer exists"


def _valid_job_id(job_id: Any) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.match(job_id))


def _rewind(request: TranscriptionRequest) -> None:
    try:
        request.audio_file.seek(0)
    except (AttributeError, OSError, ValueError):
        pass


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _retry_after(response: httpx.Response) -> Optional[float]:
    """Seconds from a Retry-After header (seconds or HTTP date) or a retry_after body field."""
    raw = response.headers.get('Retry-After')
    seconds = _number(raw)
    if seconds is None and raw:
        try:
            when = email.utils.parsedate_to_datetime(raw)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            seconds = None
    if seconds is None:
        body = _json_body(response)
        if isinstance(body, dict):
            seconds = _number(body.get('retry_after'))
    if seconds is None:
        return None
    return max(1.0, seconds)


def _poll_wait(data: Dict[str, Any], response: httpx.Response) -> float:
    """poll_after_seconds (or Retry-After), kept within the polling bounds."""
    seconds = _number(data.get('poll_after_seconds'))
    if seconds is None:
        seconds = _number(response.headers.get('Retry-After'))
    if seconds is None:
        seconds = POLL_DEFAULT_SECONDS
    return min(POLL_MAX_SECONDS, max(POLL_MIN_SECONDS, seconds))


def _job_error(job_id: str, error: Any) -> RemoteJobFailed:
    """The failure of a job in status error, retryable or not as the service says."""
    error = error if isinstance(error, dict) else {}
    code = str(error.get('code') or 'internal')
    message = summarize_error_body(str(error.get('message') or ''), 500)
    retryable = error.get('retryable')
    if not isinstance(retryable, bool):
        retryable = code not in NON_RETRYABLE_CODES
    text = f"ASR job {job_id} failed ({code})"
    if message:
        text += f": {message}"
    return RemoteJobFailed(text, permanent=not retryable, code=code, provider=ASREndpointConnector.PROVIDER_NAME)


def cancel_remote_job(base_url: str, job_id: str, timeout: float = CANCEL_TIMEOUT_SECONDS) -> bool:
    """DELETE /jobs/{id} on the service: cancels a running job or drops its result.

    Best effort; returns whether the service confirmed it (200, or 404 for a
    job it no longer knows).
    """
    if not _valid_job_id(job_id) or not base_url:
        return False
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.delete(f"{base_url.rstrip('/')}/jobs/{job_id}")
        logger.info(f"ASR job {job_id}: cancel answered {response.status_code}")
        return response.status_code in (200, 404)
    except Exception as e:
        logger.warning(f"ASR job {job_id} could not be cancelled: {e}")
        return False
