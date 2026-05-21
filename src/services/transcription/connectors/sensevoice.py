"""
SenseVoice ASR connector using FunAudioLLM/SenseVoiceSmall.

Provides local, offline speech-to-text transcription with multilingual support
(Chinese, English, Japanese, Korean) via the FunASR library.

Model loading order:
1. Local directory (SENSEVOICE_MODEL_DIR env var or model_dir config)
2. ModelScope (default, works in China)
"""

import logging
import os
from pathlib import Path
from typing import Dict, Any, Set

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


class SenseVoiceConnector(BaseTranscriptionConnector):
    """Connector for local SenseVoice ASR (FunAudioLLM/SenseVoiceSmall)."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.CHUNKING,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "sensevoice"

    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=False,
        recommended_chunk_seconds=1800,
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        self.model_name = config.get(
            "model_name", "FunAudioLLM/SenseVoiceSmall"
        )
        self.device = config.get("device", "cuda")
        self.ncpu_thread_config = config.get("ncpu_thread", 4)
        self.disable_update = config.get("disable_update", True)

        self._model = None
        self._model_dir = config.get("model_dir", None)
        self._cache_dir = config.get("model_cache_dir", None)
        self._load_model()

    def _load_model(self):
        """Load SenseVoice model, preferring local directory if available."""
        try:
            from funasr import AutoModel

            model_path = self._resolve_model_path()

            kwargs = {
                "model": model_path,
                "device": self.device,
                "ncpu_thread": self.ncpu_thread_config,
                "disable_update": self.disable_update,
            }

            if self._cache_dir:
                kwargs["cache_dir"] = self._cache_dir

            logger.info(
                f"Loading SenseVoice model from: {model_path} "
                f"(device={self.device})"
            )
            self._model = AutoModel(**kwargs)
            logger.info("SenseVoice model loaded successfully")

        except ImportError:
            raise ConfigurationError(
                "funasr package is required for SenseVoice connector. "
                "Install it with: pip install funasr"
            )
        except Exception as e:
            raise ConfigurationError(
                f"Failed to load SenseVoice model '{self.model_name}': {e}"
            ) from e

    def _resolve_model_path(self) -> str:
        """Resolve model path, preferring local directory if it exists."""
        if self._model_dir and Path(self._model_dir).exists():
            logger.info(f"Using local SenseVoice model from: {self._model_dir}")
            return self._model_dir
        return self.model_name

    def _validate_config(self) -> None:
        pass

    def transcribe(
        self, request: TranscriptionRequest
    ) -> TranscriptionResponse:
        import tempfile

        audio_path = None
        is_temp = False

        if isinstance(request.audio_file, (str, bytes, Path)):
            audio_path = str(request.audio_file)
        else:
            suffix = os.path.splitext(request.filename)[1] if request.filename else ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                while True:
                    chunk = request.audio_file.read(8192)
                    if not chunk:
                        break
                    tmp.write(chunk)
                audio_path = tmp.name
            is_temp = True

        try:
            result = self._model.generate(
                input=audio_path,
                language=request.language or "auto",
                use_itn=False,
            )

            return self._parse_result(result)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"SenseVoice transcription failed: {error_msg}")
            raise TranscriptionError(
                f"SenseVoice transcription failed: {error_msg}"
            ) from e
        finally:
            if is_temp and audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    def _parse_result(
        self, result: Dict[str, Any]
    ) -> TranscriptionResponse:
        import re

        segments = []
        full_text_parts = []

        if isinstance(result, (list, tuple)) and len(result) >= 1:
            first = result[0]
        else:
            first = result

        raw_text = first.get("text", "")

        # Extract language from SenseVoice special token <|xx|>
        detected_lang = first.get("language") or first.get("lang")
        if not detected_lang:
            lang_match = re.search(r'<\|([a-z]{2}(?:-[a-z]{2})?)\|>', raw_text)
            if lang_match:
                detected_lang = lang_match.group(1)

        # Strip all SenseVoice special tokens: <|zh|>, <|NEUTRAL|>, <|Speech|>, <|woitn|>, etc.
        clean_text = re.sub(r'<\|[^|]*\|>', '', raw_text).strip()

        # Try to parse timestamp segments if available
        raw_segments = first.get("timestamp", [])
        if raw_segments:
            for ts in raw_segments:
                seg_text = ts.get("text", "").strip()
                # Strip tokens from segment text too
                seg_text = re.sub(r'<\|[^|]*\|>', '', seg_text).strip()
                start_ms = ts.get("start", 0)
                end_ms = ts.get("end", 0)

                start_s = start_ms / 1000.0
                end_s = end_ms / 1000.0

                if seg_text:
                    full_text_parts.append(seg_text)

                segments.append(
                    TranscriptionSegment(
                        text=seg_text,
                        start_time=start_s,
                        end_time=end_s,
                    )
                )

        if not segments and clean_text:
            segments.append(
                TranscriptionSegment(text=clean_text)
            )
            full_text_parts.append(clean_text)

        full_text = "".join(full_text_parts) if full_text_parts else clean_text

        return TranscriptionResponse(
            text=full_text,
            segments=segments if segments else None,
            language=detected_lang,
            provider=self.PROVIDER_NAME,
            model=self.model_name,
            raw_response=first,
        )

    def health_check(self) -> bool:
        try:
            return self._model is not None
        except Exception:
            return False

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": [],
            "properties": {
                "model_name": {
                    "type": "string",
                    "default": "FunAudioLLM/SenseVoiceSmall",
                    "description": "HuggingFace model identifier",
                },
                "device": {
                    "type": "string",
                    "default": "cuda",
                    "description": "Device for inference (cuda, cpu)",
                },
                "ncpu_thread": {
                    "type": "integer",
                    "default": 4,
                    "description": "Number of CPU threads",
                },
                "disable_update": {
                    "type": "boolean",
                    "default": True,
                    "description": "Skip model update check on load",
                },
                "model_cache_dir": {
                    "type": "string",
                    "description": "Directory to cache downloaded models",
                },
            },
        }
