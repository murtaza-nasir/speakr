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
import re
from pathlib import Path
from typing import Dict, Any, Set, Optional, List

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

        # Punctuation restoration model (CT-Transformer, lazy loaded)
        self.use_punctuation_model = config.get("use_punctuation_model", True)
        self._punc_model = None
        self._punc_model_loaded = False

        # Auto-detect GPU memory and fallback to CPU if insufficient
        self._check_and_adjust_device()
        self._load_model()

    def _check_and_adjust_device(self):
        """Check GPU memory and fallback to CPU if insufficient for SenseVoice."""
        if self.device != "cuda":
            return

        try:
            import torch
            if not torch.cuda.is_available():
                logger.info("CUDA not available, falling back to CPU")
                self.device = "cpu"
                return

            free_gb = torch.cuda.mem_get_info()[0] / (1024**3)
            total_gb = torch.cuda.mem_get_info()[1] / (1024**3)
            logger.info(f"GPU memory: {free_gb:.2f}GB free / {total_gb:.2f}GB total")

            # SenseVoiceSmall needs ~2GB for model + ~1.2GB per chunk = ~3.5GB minimum
            min_required_gb = 3.5
            if free_gb < min_required_gb:
                logger.warning(
                    f"GPU free memory ({free_gb:.2f}GB) is below minimum required "
                    f"({min_required_gb}GB) for SenseVoice - falling back to CPU. "
                    f"This usually means another GPU process (e.g., CosyVoice) is using the GPU."
                )
                self.device = "cpu"
            else:
                logger.info(f"GPU has sufficient memory ({free_gb:.2f}GB free), using CUDA")
        except Exception as e:
            logger.warning(f"GPU memory check failed ({e}), keeping device={self.device}")

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
            logger.info(f"SenseVoice transcribing: {audio_path} (filename={request.filename})")
            result = self._model.generate(
                input=audio_path,
                language=request.language or "auto",
                use_itn=False,
            )

            # Log raw result for debugging
            if isinstance(result, (list, tuple)) and len(result) >= 1:
                raw_text = result[0].get("text", "")
            else:
                raw_text = result.get("text", "")
            logger.info(f"SenseVoice raw output ({len(raw_text)} chars): {raw_text[:200]}")

            response = self._parse_result(result)
            logger.info(f"SenseVoice parsed: {len(response.text)} chars, {len(response.segments)} segments: {response.text[:200]}")

            # Free GPU memory after each transcription to prevent OOM on subsequent chunks
            self._free_gpu_memory()

            return response
        except Exception as e:
            error_msg = str(e)
            logger.error(f"SenseVoice transcription failed: {error_msg}")
            self._free_gpu_memory()
            raise TranscriptionError(
                f"SenseVoice transcription failed: {error_msg}"
            ) from e
        finally:
            if is_temp and audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    def _get_punc_model(self):
        """Lazy load CT-Transformer punctuation restoration model."""
        if not self.use_punctuation_model:
            return None
        if self._punc_model_loaded:
            return self._punc_model
        self._punc_model_loaded = True
        try:
            from funasr import AutoModel
            self._punc_model = AutoModel(
                model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                device=self.device,
                disable_update=True,
            )
            logger.info("CT-Transformer punctuation model loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load CT-Transformer punctuation model: {e}")
            self._punc_model = None
        return self._punc_model

    def _restore_punctuation(self, text: str) -> str:
        """Restore punctuation in text using CT-Transformer model."""
        if not text or not self._get_punc_model():
            return text
        try:
            result = self._get_punc_model().generate(input=text)
            if isinstance(result, list) and len(result) > 0:
                if isinstance(result[0], dict):
                    return result[0].get("text", text)
            elif isinstance(result, dict):
                return result.get("text", text)
            return text
        except Exception as e:
            logger.warning(f"Punctuation restoration failed: {e}")
            return text

    def _split_sentences(self, text: str) -> List[str]:
        """Split text into sentences based on Chinese/English punctuation."""
        sentences = re.split(
            r'(?<=[。！？.!?\n])\s*',
            text.replace('\n', '。')
        )
        return [s.strip() for s in sentences if s.strip()]

    def _strip_punc(self, text: str) -> str:
        """Remove punctuation to enable matching between raw and punctuated text."""
        return re.sub(r'[，。！？、；：""''（）【】《》—\s,\.!\?;:\'\"\(\)\[\]{}-]', '', text)

    def _map_stripped_to_original(self, stripped: str, original: str, pos: int) -> int:
        """Map a position in stripped text back to original text position."""
        si, oi = 0, 0
        while si < pos and oi < len(original):
            if self._strip_punc(original[oi]):
                oi += 1
            else:
                si += 1
                oi += 1
        return oi

    def _assign_sentence_timestamps(
        self, sentences: List[str], segments: List[TranscriptionSegment]
    ) -> List[TranscriptionSegment]:
        """
        Assign timestamps to sentences by matching stripped (no-punctuation)
        sentences against original segment texts.
        """
        if not segments or not sentences:
            return segments or []

        seg_texts = [s.text for s in segments if s.text]
        full_seg_text = "".join(seg_texts)
        full_seg_stripped = self._strip_punc(full_seg_text)

        result = []
        stripped_pos = 0

        for sentence in sentences:
            if not sentence.strip():
                continue

            sentence_stripped = self._strip_punc(sentence)
            if not sentence_stripped:
                continue

            start_stripped = full_seg_stripped.find(sentence_stripped, stripped_pos)
            if start_stripped == -1:
                start_stripped = stripped_pos
            end_stripped = start_stripped + len(sentence_stripped)
            stripped_pos = end_stripped

            start_char = self._map_stripped_to_original(full_seg_stripped, full_seg_text, start_stripped)
            end_char = self._map_stripped_to_original(full_seg_stripped, full_seg_text, end_stripped)

            start_time = None
            end_time = None

            cum_chars = 0
            for ts in segments:
                seg_len = len(ts.text or "")
                seg_start_char = cum_chars
                seg_end_char = cum_chars + seg_len

                if start_char >= seg_start_char and start_char < seg_end_char:
                    ratio = (start_char - seg_start_char) / max(seg_len, 1)
                    if ts.start_time is not None and ts.end_time is not None:
                        start_time = ts.start_time + ratio * (ts.end_time - ts.start_time)

                if end_char >= seg_start_char and end_char <= seg_end_char:
                    ratio = (end_char - seg_start_char) / max(seg_len, 1)
                    if ts.start_time is not None and ts.end_time is not None:
                        end_time = ts.start_time + ratio * (ts.end_time - ts.start_time)
                    break
                elif end_char > seg_end_char:
                    if ts.end_time is not None:
                        end_time = ts.end_time

                cum_chars += seg_len

            result.append(TranscriptionSegment(
                text=sentence,
                start_time=start_time,
                end_time=end_time,
            ))

        return result

    def _parse_result(
        self, result: Dict[str, Any]
    ) -> TranscriptionResponse:
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
                seg_text = re.sub(r'<\|[^|]*\|>', '', seg_text).strip()
                start_ms = ts.get("start", 0)
                end_ms = ts.get("end", 0)

                start_s = start_ms / 1000.0
                end_s = end_ms / 1000.0

                # Apply CT-Transformer to each segment individually (clean raw text → punctuation)
                seg_text = self._restore_punctuation(seg_text)

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
            clean_text = self._restore_punctuation(clean_text)
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

    def _free_gpu_memory(self):
        """Free GPU memory after transcription to prevent OOM on subsequent chunks."""
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                # Also run garbage collection to free Python-level references
                import gc
                gc.collect()
                free_gb = torch.cuda.mem_get_info()[0] / (1024**3)
                logger.info(f"GPU memory freed - free: {free_gb:.2f}GB")
        except Exception as e:
            logger.warning(f"Failed to free GPU memory: {e}")

    def get_gpu_free_memory_gb(self) -> float:
        """Return available GPU memory in GB, or -1 if GPU not available."""
        try:
            import torch
            if torch.cuda.is_available():
                free_bytes = torch.cuda.mem_get_info()[0]
                return free_bytes / (1024**3)
        except Exception:
            pass
        return -1.0

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
                "model_dir": {
                    "type": "string",
                    "description": "Local directory containing pre-downloaded model",
                },
                "model_cache_dir": {
                    "type": "string",
                    "description": "Directory to cache downloaded models",
                },
                "use_punctuation_model": {
                    "type": "boolean",
                    "default": True,
                    "description": "Enable CT-Transformer punctuation restoration",
                },
            },
        }
