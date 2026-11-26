"""
audio.cpp server connector (#700).

audio.cpp (https://github.com/0xShug0/audio.cpp) serves speech models such as
VibeVoice ASR, Parakeet-TDT, Nemotron ASR and Qwen3-ASR from one C++ process,
``audiocpp_server``. Its OpenAI-style ``POST /v1/audio/transcriptions`` returns
text only; ``POST /v1/audio/transcriptions/details`` takes the same multipart
request (``file``, ``model``, optional ``language``) and adds what the model
produced: ``segments``, ``speaker_turns`` (when the model diarizes), ``words``
and ``language``. Offsets are sample counts; ``sample_rate`` converts them to
seconds. The server documents no authentication, so the connector sends no key.

The core routes accept WAV (MP3 and FLAC only with an optional frontend
module), and some models require mono 16 kHz, so every upload is sent as mono
16 kHz 16-bit PCM WAV: a file already in that form as it is, anything else
converted with FFmpeg first.

Speakers come from the response, not from a request option: audio.cpp has no
documented speaker-count parameter, and whether a model diarizes is a
property of the model the server runs. ``speaker_turns`` become diarized
segments (labels normalized to SPEAKER_NN); without them the result is a
plain transcript, as from any connector that does not diarize. The
``model_diarizes`` setting only tells Speakr, before a recording is sent,
that speaker labels can be expected (the speaker options of the upload form).
"""

import io
import logging
import os
import subprocess
import tempfile
import wave
from typing import Any, Dict, List, Optional, Set

import httpx

from ..base import (
    BaseTranscriptionConnector,
    ConnectorSpecifications,
    PROBE_TIMEOUT_SECONDS,
    ProbeResult,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    probe_failure,
    probe_result_from_response,
)
from ..exceptions import AudioFormatError, ConfigurationError, ProviderError, TranscriptionError
from src.utils.error_body import summarize_error_body
from src.utils.error_text import describe_body, log_failure

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000
DETAILS_PATH = '/v1/audio/transcriptions/details'
FFMPEG_TIMEOUT_SECONDS = 1800


class AudioCppResponseError(TranscriptionError):
    """The server answered with a body the connector cannot read."""


def _root_url(base_url: str) -> str:
    """The server root: the base URL without a trailing ``/v1``."""
    root = (base_url or '').strip().rstrip('/')
    if root.endswith('/v1'):
        root = root[:-3]
    return root


def _is_target_wav(data: bytes) -> bool:
    """Whether the bytes are a mono 16 kHz 16-bit PCM WAV file."""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return False
    try:
        with wave.open(io.BytesIO(data), 'rb') as handle:
            return (handle.getnchannels() == 1
                    and handle.getsampwidth() == 2
                    and handle.getframerate() == TARGET_SAMPLE_RATE
                    and handle.getcomptype() == 'NONE')
    except (wave.Error, EOFError):
        return False


def _join_words(words: List[str]) -> str:
    """Join word tokens; tokens that carry their own spacing keep it."""
    tokens = [word for word in words if isinstance(word, str) and word]
    if not tokens:
        return ''
    if any(token[:1].isspace() for token in tokens):
        return ''.join(tokens).strip()
    return ' '.join(token.strip() for token in tokens if token.strip())


class AudioCppTranscriptionConnector(BaseTranscriptionConnector):
    """Connector for an audio.cpp server (audiocpp_server)."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_SELECTION,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "audiocpp"
    DISPLAY_NAME = "audio.cpp server"
    DESCRIPTION = ("A self-hosted audio.cpp server (VibeVoice ASR, Parakeet, Nemotron, Qwen3-ASR) "
                   "with timestamps and, for models that diarize, speaker labels.")

    # The server takes a request of up to 2 GiB (max_request_body_bytes), far
    # beyond a recording as 16 kHz WAV, so the app does not chunk unless
    # max_duration_seconds is set for a model with a duration limit.
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
        recommended_chunk_seconds=0,
    )

    def __init__(self, config: Dict[str, Any]):
        self.base_url = _root_url(config.get('base_url', ''))
        self.model = (config.get('model') or '').strip()
        self.model_diarizes = bool(config.get('model_diarizes', False))
        # Speakr asks the connector for this attribute when it decides whether
        # to request diarization; audio.cpp takes no such request option.
        self.default_diarize = self.model_diarizes
        try:
            self._config_timeout = int(config.get('timeout') or 1800)
        except (TypeError, ValueError):
            self._config_timeout = 1800
        try:
            max_seconds = int(config.get('max_duration_seconds') or 0)
        except (TypeError, ValueError):
            max_seconds = 0
        super().__init__(config)
        if self.model_diarizes:
            self.CAPABILITIES = self.CAPABILITIES | {TranscriptionCapability.DIARIZATION}
        if max_seconds > 0:
            # A model with a duration limit (VibeVoice ASR takes about an
            # hour): the app cuts longer recordings into chunks, as for the
            # Mistral connector, and merges them with the existing chunker.
            self.SPECIFICATIONS = ConnectorSpecifications(
                max_file_size_bytes=None,
                max_duration_seconds=max_seconds,
                handles_chunking_internally=False,
                recommended_chunk_seconds=int(max_seconds * 0.8),
            )

    def _validate_config(self) -> None:
        # The model is checked when a recording is sent, not here: Test and
        # Discover models run before a model is chosen.
        if not self.config.get('base_url'):
            raise ConfigurationError(
                "base_url is required for the audio.cpp connector. "
                "Set TRANSCRIPTION_BASE_URL to the audiocpp_server address.")

    # ------------------------------------------------------------ audio

    def _wav_bytes(self, request: TranscriptionRequest) -> bytes:
        """The recording as mono 16 kHz 16-bit PCM WAV bytes."""
        audio = request.audio_file
        if hasattr(audio, 'seek'):
            try:
                audio.seek(0)
            except (OSError, ValueError):
                pass
        data = audio.read() if hasattr(audio, 'read') else bytes(audio or b'')
        if not data:
            raise AudioFormatError('The recording is empty.')
        if _is_target_wav(data):
            return data
        # The copies of an incognito file go into the folder of its request
        # and are removed with it, as for the VibeVoice and Tencent connectors.
        from src.services.incognito_temp import dir_for_stream
        return self._convert_to_wav(data, request.filename,
                                    temp_dir=dir_for_stream(request.audio_file))

    def _convert_to_wav(self, data: bytes, filename: Optional[str], temp_dir: Optional[str] = None) -> bytes:
        suffix = os.path.splitext(filename or '')[1] or '.bin'
        source = target = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=temp_dir) as handle:
                handle.write(data)
                source = handle.name
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=temp_dir) as handle:
                target = handle.name
            command = ['ffmpeg', '-y', '-nostdin', '-loglevel', 'error', '-i', source,
                       '-vn', '-ac', '1', '-ar', str(TARGET_SAMPLE_RATE),
                       '-c:a', 'pcm_s16le', '-f', 'wav', target]
            try:
                subprocess.run(command, check=True, capture_output=True,
                               timeout=FFMPEG_TIMEOUT_SECONDS)
            except FileNotFoundError as error:
                raise AudioFormatError(
                    'FFmpeg is needed to send audio to audio.cpp and was not found.') from error
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or b'').decode('utf-8', errors='replace').strip()[-300:]
                raise AudioFormatError(
                    f'Could not convert the recording to WAV for audio.cpp: {detail}') from error
            with open(target, 'rb') as handle:
                return handle.read()
        finally:
            for path in (source, target):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # ------------------------------------------------------------ transcribe

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        model = self._effective_model(request) or self.model
        if not model:
            raise ConfigurationError(
                "The audio.cpp connector needs a model: the id of a transcription model "
                "in the server's config (TRANSCRIPTION_MODEL, or Model name in the admin settings).")
        url = self.base_url + DETAILS_PATH
        wav = self._wav_bytes(request)
        stem = os.path.splitext(os.path.basename(request.filename or 'audio'))[0] or 'audio'
        files = {
            'file': (f'{stem}.wav', wav, 'audio/wav'),
            'model': (None, model),
        }
        if request.language:
            files['language'] = (None, request.language)
        timeout = httpx.Timeout(None, connect=30.0, read=float(self._config_timeout),
                                write=float(self._config_timeout))
        logger.info(f"audio.cpp transcription: model={model}, {len(wav)} bytes of WAV")
        try:
            with httpx.Client() as client:
                response = client.post(url, files=files, timeout=timeout)
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as error:
                    raise AudioCppResponseError(
                        'audio.cpp answered with a body that is not JSON.') from error
        except httpx.HTTPStatusError as error:
            try:
                body = error.response.text
            except Exception:  # noqa: BLE001
                body = ''
            excerpt = summarize_error_body(body, 800)
            status = error.response.status_code
            logger.error(f"audio.cpp request failed: {describe_body(status, body)}")
            raise ProviderError(
                f"audio.cpp request failed (status {status}): {excerpt}",
                provider=self.PROVIDER_NAME, status_code=status) from error
        except httpx.TimeoutException as error:
            logger.error(f"audio.cpp request timed out after {self._config_timeout}s")
            raise TranscriptionError(
                f"audio.cpp request timed out after {self._config_timeout}s") from error
        except httpx.HTTPError as error:
            log_failure(logger, logging.ERROR, "audio.cpp request failed", error)
            raise TranscriptionError(f"audio.cpp request failed: {error}") from error
        if not isinstance(data, dict):
            raise AudioCppResponseError('audio.cpp answered with JSON that is not an object.')
        return self.parse_details(data, model)

    # ------------------------------------------------------------ response

    def parse_details(self, data: Dict[str, Any], model: str) -> TranscriptionResponse:
        """Turn a /v1/audio/transcriptions/details body into a response."""
        text = data.get('text') if isinstance(data.get('text'), str) else ''
        turns = [turn for turn in data.get('speaker_turns') or [] if isinstance(turn, dict)]
        segments_in = [seg for seg in data.get('segments') or [] if isinstance(seg, dict)]
        words = [word for word in data.get('words') or [] if isinstance(word, dict)]

        rate = None
        if turns or segments_in or words:
            rate = data.get('sample_rate')
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
                raise AudioCppResponseError(
                    'audio.cpp returned sample offsets without a sample_rate, so their '
                    'times cannot be read.')

        def seconds(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return round(float(value) / float(rate), 3)

        def words_between(start, end):
            if start is None or end is None:
                return ''
            inside = []
            for word in words:
                w_start, w_end = word.get('start_sample'), word.get('end_sample')
                if not isinstance(w_start, (int, float)) or not isinstance(w_end, (int, float)):
                    continue
                middle = (w_start + w_end) / 2.0
                if start <= middle < end:
                    inside.append(word.get('word'))
            return _join_words(inside)

        segments: List[TranscriptionSegment] = []
        speakers = None
        if turns:
            from src.utils.speaker_labels import diarization_label_map

            raw_labels = [str(turn.get('speaker_id')) for turn in turns
                          if turn.get('speaker_id') not in (None, '')]
            mapping = diarization_label_map(raw_labels)
            order = []
            for turn in sorted(turns, key=lambda t: (t.get('start_sample') or 0)):
                start, end = turn.get('start_sample'), turn.get('end_sample')
                turn_text = turn.get('text') if isinstance(turn.get('text'), str) else ''
                turn_text = turn_text.strip() or words_between(start, end)
                if not turn_text:
                    # A diarization-only model (Sortformer) labels time spans
                    # without text; a span with no words is not a sentence.
                    continue
                raw = turn.get('speaker_id')
                label = None
                if raw not in (None, ''):
                    label = mapping.get(str(raw), str(raw))
                    if label not in order:
                        order.append(label)
                segments.append(TranscriptionSegment(
                    text=turn_text, speaker=label,
                    start_time=seconds(start), end_time=seconds(end)))
            speakers = order or None
            if not segments:
                logger.warning('audio.cpp returned speaker turns without any text; '
                               'the transcript has no speaker labels')
        if not segments and segments_in:
            for seg in sorted(segments_in, key=lambda s: (s.get('start_sample') or 0)):
                start, end = seg.get('start_sample'), seg.get('end_sample')
                seg_text = seg.get('text') if isinstance(seg.get('text'), str) else ''
                seg_text = seg_text.strip() or words_between(start, end)
                if seg_text:
                    segments.append(TranscriptionSegment(
                        text=seg_text, speaker=None,
                        start_time=seconds(start), end_time=seconds(end)))
            speakers = None

        if not text.strip() and segments:
            text = ' '.join(seg.text for seg in segments)
        if not text.strip() and words:
            text = _join_words([word.get('word') for word in words])

        language = data.get('language') if isinstance(data.get('language'), str) else None
        return TranscriptionResponse(
            text=text,
            segments=segments or None,
            speakers=speakers,
            language=language,
            provider=self.PROVIDER_NAME,
            model=model,
            raw_response=data,
        )

    # ------------------------------------------------------------ probe

    def probe(self) -> ProbeResult:
        """GET /v1/models (model discovery); /health when that route is missing."""
        try:
            response = httpx.get(f'{self.base_url}/v1/models', timeout=PROBE_TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            return probe_failure(error)
        if response.status_code == 200:
            return probe_result_from_response(response)
        try:
            health = httpx.get(f'{self.base_url}/health', timeout=PROBE_TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            return probe_failure(error)
        if health.status_code < 400:
            return ProbeResult(ok=True, models=[], status=health.status_code, message='Connected.')
        return ProbeResult(ok=False, status=health.status_code, message=f'HTTP {health.status_code}')

    def health_check(self) -> bool:
        try:
            return httpx.get(f'{self.base_url}/health', timeout=PROBE_TIMEOUT_SECONDS).status_code < 400
        except httpx.HTTPError:
            return False

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["base_url", "model"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "audiocpp_server address (e.g. http://audiocpp:8080)",
                },
                "model": {
                    "type": "string",
                    "examples": ["vibevoice-asr", "parakeet-tdt", "qwen3-asr", "nemotron-3-diar"],
                    "description": "The id of a transcription model in the server's config",
                },
                "model_diarizes": {
                    "type": "boolean",
                    "default": False,
                    "description": ("Turn on when the model assigns speaker labels (VibeVoice ASR, "
                                    "Nemotron 3 Diarization). Speaker labels in a response are stored either way."),
                },
                "timeout": {
                    "type": "integer",
                    "default": 1800,
                    "description": "Request timeout in seconds",
                },
                "max_duration_seconds": {
                    "type": "integer",
                    "default": 0,
                    "description": ("Longest audio the model takes in one request; longer recordings "
                                    "are split. 0 sends every recording whole."),
                },
            },
        }

