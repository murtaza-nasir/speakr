"""
VibeVoice ASR connector for audio transcription.

Supports Microsoft's VibeVoice model served via vLLM, which provides
structured transcription with speaker diarization, timestamps, and
automatic language detection for 50+ languages. Handles up to 60 minutes
per request via the OpenAI-compatible chat completions API.
"""

import base64
import json
import logging
import mimetypes
import re
import httpx
from typing import Dict, Any, Set, List, Optional

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError, ProviderError

logger = logging.getLogger(__name__)


class VibeVoiceTranscriptionConnector(BaseTranscriptionConnector):
    """Connector for VibeVoice ASR via vLLM's OpenAI-compatible API."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "vibevoice"

    # VibeVoice handles up to ~60 minutes per request; use app-level chunking beyond that
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=3500,  # ~58 min safety margin under the 60 min cap
        handles_chunking_internally=False,
        recommended_chunk_seconds=3000,  # ~50 min chunks to stay well under limit
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the VibeVoice connector.

        Args:
            config: Configuration dict with keys:
                - base_url: vLLM server URL (required, e.g. http://localhost:8000)
                - model: Model name (default: microsoft/VibeVoice-ASR)
                - api_key: API key if the vLLM server requires auth (optional)
        """
        super().__init__(config)

        self.base_url = config['base_url'].rstrip('/')
        self.model = config.get('model', 'microsoft/VibeVoice-ASR')
        self.api_key = config.get('api_key', '')

        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Speakr/1.0 (https://github.com/murtaza-nasir/speakr)',
        }
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, read=3600.0, write=300.0),
            verify=True,
        )

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('base_url'):
            raise ConfigurationError("base_url is required for VibeVoice connector")

    def _get_audio_duration(self, audio_bytes: bytes, filename: str = None) -> Optional[float]:
        """Get audio duration in seconds using ffprobe."""
        try:
            import subprocess, tempfile, os
            suffix = ''
            if filename:
                _, suffix = os.path.splitext(filename)
            with tempfile.NamedTemporaryFile(suffix=suffix or '.wav', delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', tmp_path],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    return float(result.stdout.strip())
            finally:
                os.unlink(tmp_path)
        except Exception as e:
            logger.debug(f"Could not determine audio duration: {e}")
        return None

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using VibeVoice via vLLM chat completions API.

        Audio is sent as a base64 data URL in a multimodal chat message.
        The model returns a JSON array of segments with speaker, timestamps, and text.
        """
        try:
            # Read and base64-encode the audio
            audio_bytes = request.audio_file.read()
            audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

            # Determine MIME type
            mime = request.mime_type
            if not mime and request.filename:
                mime, _ = mimetypes.guess_type(request.filename)
            if not mime:
                mime = 'audio/wav'

            # Get audio duration for the prompt
            duration = self._get_audio_duration(audio_bytes, request.filename)

            # Build the transcription prompt (per VibeVoice docs)
            if request.hotwords and duration:
                text_prompt = (
                    f"This is a {duration:.2f} seconds audio, "
                    f"with extra info: {request.hotwords}\n\n"
                    f"Please transcribe it with these keys: Start time, End time, Speaker ID, Content"
                )
            elif duration:
                text_prompt = (
                    f"This is a {duration:.2f} seconds audio, "
                    f"please transcribe it with these keys: Start time, End time, Speaker ID, Content"
                )
            elif request.hotwords:
                text_prompt = (
                    f"Extra info: {request.hotwords}\n\n"
                    f"Please transcribe it with these keys: Start time, End time, Speaker ID, Content"
                )
            else:
                text_prompt = "Please transcribe it with these keys: Start time, End time, Speaker ID, Content"

            # Build the data URL (VibeVoice docs format)
            data_url = f"data:{mime};base64,{audio_b64}"

            # Build the chat completion request per VibeVoice docs
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that transcribes audio input into text output in JSON format."
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "audio_url", "audio_url": {"url": data_url}},
                            {"type": "text", "text": text_prompt},
                        ],
                    }
                ],
                "max_tokens": 32768,
                "temperature": 0.0,
                "top_p": 1.0,
            }

            logger.info(f"Sending request to VibeVoice at {self.base_url} (duration={duration}s, model={self.model})")
            response = self.client.post('/v1/chat/completions', json=payload)

            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    error_detail = error_json.get('error', {}).get('message', response.text)
                except Exception:
                    pass
                raise ProviderError(
                    f"VibeVoice API error: {error_detail}",
                    provider=self.PROVIDER_NAME,
                    status_code=response.status_code,
                )

            result = response.json()

            # Extract the content from the chat completion response
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            if not content:
                raise TranscriptionError("VibeVoice returned empty content")

            # Parse the JSON array of segments from the response
            raw_segments = self._parse_json_segments(content)

            segments = self._parse_segments(raw_segments)

            # Build full text from segments
            full_text = ' '.join(seg.text for seg in segments if seg.text)

            # Extract unique speakers
            speakers = list({s.speaker for s in segments if s.speaker})

            logger.info(f"VibeVoice transcription complete: {len(segments)} segments, {len(speakers)} speakers")

            return TranscriptionResponse(
                text=full_text,
                segments=segments,
                speakers=speakers if speakers else None,
                provider=self.PROVIDER_NAME,
                model=self.model,
                raw_response=result,
            )

        except (ProviderError, TranscriptionError):
            raise
        except httpx.TimeoutException as e:
            logger.error(f"VibeVoice API request timed out: {e}")
            raise TranscriptionError(f"VibeVoice API request timed out: {e}") from e
        except Exception as e:
            error_msg = str(e)
            logger.error(f"VibeVoice transcription failed: {error_msg}")
            raise TranscriptionError(f"VibeVoice transcription failed: {error_msg}") from e

    def _parse_json_segments(self, content: str) -> List[Dict[str, Any]]:
        """
        Parse the JSON array from VibeVoice, with fallback for truncated/malformed output.

        VibeVoice can sometimes produce degenerate JSON towards the end of longer
        transcriptions. When strict parsing fails, we recover by finding the last
        complete segment object and truncating there.
        """
        # Dump raw response to temp file for debugging
        try:
            import tempfile, os
            debug_path = os.path.join(tempfile.gettempdir(), 'vibevoice_last_response.json')
            with open(debug_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.debug(f"VibeVoice raw response saved to {debug_path}")
        except Exception:
            pass

        # Try strict parse first
        try:
            result = json.loads(content)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Try lenient parse (allows control characters)
        try:
            result = json.loads(content, strict=False)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Fallback: find all },{ boundaries and try progressively shorter
        # truncations until we get valid JSON. The corruption can start mid-segment
        # so we may need to cut more than just the last one.
        boundaries = [m.start() for m in re.finditer(r'\}\s*,\s*\{', content)]

        for cut_pos in reversed(boundaries):
            truncated = content[:cut_pos + 1] + ']'
            try:
                result = json.loads(truncated, strict=False)
                if isinstance(result, list) and len(result) > 0:
                    logger.warning(
                        f"VibeVoice JSON was truncated: recovered {len(result)} segments "
                        f"from {len(content)} chars (cut at position {cut_pos})"
                    )
                    return result
            except json.JSONDecodeError:
                continue

        logger.error(f"Failed to parse VibeVoice response as JSON: {content[:500]}")
        raise TranscriptionError("VibeVoice returned unparseable JSON response")

    def _parse_segments(self, raw_segments: List[Dict[str, Any]]) -> List[TranscriptionSegment]:
        """
        Convert VibeVoice segment format to TranscriptionSegment objects.

        VibeVoice returns speaker IDs as integers (0, 1, 2...) or sometimes
        omits the Speaker key entirely for non-speech segments like [Silence],
        [Human Sounds]. For those, we assign the previous speaker's ID so they
        flow naturally. If there's no previous speaker yet, we drop the segment.
        """
        segments = []
        last_speaker = None
        for seg in raw_segments:
            raw_speaker = seg.get('Speaker')
            if raw_speaker is not None:
                # Convert integer speaker IDs to SPEAKER_XX format
                if isinstance(raw_speaker, int):
                    speaker = f"SPEAKER_{raw_speaker:02d}"
                else:
                    speaker = str(raw_speaker)
                last_speaker = speaker
            else:
                # Non-speech segment — inherit previous speaker or skip
                if last_speaker is None:
                    continue
                speaker = last_speaker

            segment = TranscriptionSegment(
                text=seg.get('Content', ''),
                speaker=speaker,
                start_time=seg.get('Start', None),
                end_time=seg.get('End', None),
            )
            segments.append(segment)
        return segments

    def health_check(self) -> bool:
        """Check if the vLLM server is reachable."""
        try:
            response = self.client.get('/v1/models')
            return response.status_code == 200
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
                    "description": "vLLM server URL (e.g. http://localhost:8000)"
                },
                "model": {
                    "type": "string",
                    "default": "microsoft/VibeVoice-ASR",
                    "description": "VibeVoice model name"
                },
                "api_key": {
                    "type": "string",
                    "default": "",
                    "description": "API key for vLLM server (if required)"
                }
            }
        }
