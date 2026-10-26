"""
Apify audio-transcriber connector.

Runs the pennydata/audio-transcriber Actor (Whisper) on the user's own Apify
account. The Actor takes audio by URL, so the connector stages the recording in
a temporary Apify key-value store (private to the user's account, readable by
its unguessable ID only while the run lasts), calls the synchronous run API
(`run-sync-get-dataset-items`), and deletes the store afterwards.

Config (from env via the registry):
- api_key:  APIFY_TOKEN                      (required; the user's own Apify API token)
- model:    APIFY_AUDIO_TRANSCRIBER_ACTOR    (optional; default pennydata~audio-transcriber)
- timeout:  APIFY_AUDIO_TRANSCRIBER_TIMEOUT  (optional; seconds, default 290)

Apify's run-sync endpoint answers within 300 seconds, so this connector suits
recordings that transcribe within that window; longer files fail with a clear error.
"""

import logging
import uuid
from typing import Dict, Any, Set, Optional

import httpx

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError

logger = logging.getLogger(__name__)

DEFAULT_ACTOR = 'pennydata~audio-transcriber'
APIFY_API = 'https://api.apify.com'


class ApifyAudioTranscriberConnector(BaseTranscriptionConnector):
    """Connector for the pennydata/audio-transcriber Apify Actor."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "apify_audio_transcriber"

    # The Actor decodes and splits long audio itself.
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        self.api_key = (config.get('api_key') or '').strip()
        self.base_url = (config.get('base_url') or APIFY_API).rstrip('/')
        self.model = (config.get('model') or DEFAULT_ACTOR).replace('/', '~')
        self.timeout = float(config.get('timeout') or 290)
        super().__init__(config)

    def _validate_config(self) -> None:
        if not self.api_key:
            raise ConfigurationError(
                "api_key is required for the Apify audio-transcriber connector. "
                "Set the APIFY_TOKEN environment variable to your Apify API token."
            )

    def _headers(self) -> Dict[str, str]:
        return {'Authorization': f'Bearer {self.api_key}'}

    def _stage_audio(self, client: httpx.Client, request: TranscriptionRequest) -> tuple:
        """Upload the recording to a fresh key-value store; return (store_id, url)."""
        resp = client.post('/v2/key-value-stores', headers=self._headers(),
                           params={'name': f'speakr-{uuid.uuid4().hex[:16]}'})
        if resp.status_code >= 400:
            raise TranscriptionError(f"Apify store creation failed (status {resp.status_code}): {resp.text[:300]}")
        store_id = resp.json()['data']['id']

        # Make the (unguessable) store ID sufficient to read the record, so the
        # Actor can fetch it without the user's token being placed in its input.
        client.put(f'/v2/key-value-stores/{store_id}', headers=self._headers(),
                   json={'generalAccess': 'ANYONE_WITH_ID_CAN_READ'})

        key = 'audio'
        up = client.put(f'/v2/key-value-stores/{store_id}/records/{key}', headers={
            **self._headers(),
            'Content-Type': request.mime_type or 'application/octet-stream',
        }, content=request.audio_file.read())
        if up.status_code >= 400:
            self._delete_store(client, store_id)
            raise TranscriptionError(f"Apify audio upload failed (status {up.status_code}): {up.text[:300]}")
        return store_id, f'{self.base_url}/v2/key-value-stores/{store_id}/records/{key}'

    def _delete_store(self, client: httpx.Client, store_id: Optional[str]) -> None:
        if not store_id:
            return
        try:
            client.delete(f'/v2/key-value-stores/{store_id}', headers=self._headers())
        except Exception as e:
            logger.warning(f"Could not delete temporary Apify store {store_id}: {e}")

    def _build_input(self, request: TranscriptionRequest, audio_url: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {'urls': [audio_url]}
        if request.language and request.language != 'auto':
            payload['language'] = request.language
        return payload

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        store_id = None
        try:
            with httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(60.0, read=self.timeout + 30)) as client:
                try:
                    store_id, audio_url = self._stage_audio(client, request)
                    logger.info(f"Apify audio-transcriber: actor={self.model}")
                    resp = client.post(
                        f'/v2/acts/{self.model}/run-sync-get-dataset-items',
                        headers=self._headers(),
                        params={'timeout': int(self.timeout)},
                        json=self._build_input(request, audio_url),
                    )
                finally:
                    self._delete_store(client, store_id)
        except httpx.TimeoutException as e:
            raise TranscriptionError(f"Apify audio-transcriber timed out after {self.timeout:.0f}s") from e
        except TranscriptionError:
            raise
        except Exception as e:
            raise TranscriptionError(f"Apify audio-transcriber failed: {e}") from e

        if resp.status_code == 408:
            raise TranscriptionError(
                "Apify run-sync limit reached: the recording did not finish within the synchronous window"
            )
        if resp.status_code >= 400:
            raise TranscriptionError(
                f"Apify audio-transcriber request failed (status {resp.status_code}): {resp.text[:800]}"
            )
        return self._parse_response(resp.json())

    def _parse_response(self, items: Any) -> TranscriptionResponse:
        if not isinstance(items, list) or not items:
            raise TranscriptionError("Apify audio-transcriber returned no dataset items")
        item = items[0]
        if item.get('status') != 'ok':
            raise TranscriptionError(
                f"Apify audio-transcriber could not transcribe the audio: {item.get('error') or item.get('note') or 'unknown error'}"
            )
        segments = [
            TranscriptionSegment(
                text=(seg.get('text') or '').strip(),
                start_time=seg.get('start'),
                end_time=seg.get('end'),
            )
            for seg in item.get('segments') or []
        ]
        language = item.get('language')
        return TranscriptionResponse(
            text=item.get('text', ''),
            segments=segments or None,
            language=None if language in (None, 'auto-detected') else language,
            duration=item.get('durationSeconds'),
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=item,
        )

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "Your Apify API token (APIFY_TOKEN)"
                },
                "model": {
                    "type": "string",
                    "default": DEFAULT_ACTOR,
                    "description": "Apify Actor to run"
                },
                "timeout": {
                    "type": "integer",
                    "default": 290,
                    "description": "Seconds to wait for the synchronous Apify run (max 300)"
                },
            },
        }
