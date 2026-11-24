"""
Base classes and data types for transcription connectors.
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Dict, Any, BinaryIO, Callable, Set, FrozenSet


class TranscriptionCapability(Enum):
    """Capabilities that connectors can declare support for."""
    DIARIZATION = auto()           # Speaker diarization
    CHUNKING = auto()              # Automatic file chunking for large files
    TIMESTAMPS = auto()            # Word/segment timestamps
    LANGUAGE_DETECTION = auto()    # Auto language detection
    KNOWN_SPEAKERS = auto()        # Support for known speaker references (future)
    SPEAKER_EMBEDDINGS = auto()    # Return speaker embeddings
    SPEAKER_COUNT_CONTROL = auto() # Support for min/max speaker count parameters
    EXACT_SPEAKER_COUNT = auto()   # Accepts exactly-N speakers only (no range); UI shows a single count field
    HOTWORDS = auto()              # Hotword/keyword biasing (or prompt-based equivalent)
    INITIAL_PROMPT = auto()        # Free-text initial prompt / context hint
    STREAMING = auto()             # Real-time streaming transcription
    LANGUAGE_SELECTION = auto()    # Accepts an explicit language parameter (request.language)


@dataclass
class ConnectorSpecifications:
    """
    Provider-specific constraints and requirements.

    Each connector declares its constraints so the application can automatically
    handle chunking, format conversion, and other preprocessing as needed.
    """
    # Size constraints
    max_file_size_bytes: Optional[int] = None  # None = unlimited

    # Duration constraints
    max_duration_seconds: Optional[int] = None  # None = unlimited
    min_duration_for_chunking: Optional[int] = None  # Provider's internal chunking threshold

    # Chunking behavior
    handles_chunking_internally: bool = False  # Provider handles large files
    requires_chunking_param: bool = False  # Must send chunking_strategy param
    recommended_chunk_seconds: int = 600  # 10 minutes default

    # Audio format support - connector-specific codec restrictions
    # None = use system defaults from get_supported_codecs()
    # Set = only allow these codecs (overrides defaults)
    supported_codecs: Optional[FrozenSet[str]] = None
    # Codecs this connector doesn't support (removed from defaults)
    # Merged with AUDIO_UNSUPPORTED_CODECS env var
    unsupported_codecs: Optional[FrozenSet[str]] = None
    # Provider accepts original audio; do not transcode or repair its container.
    preserve_original_audio: bool = False


# Default specifications for connectors that don't define their own
DEFAULT_SPECIFICATIONS = ConnectorSpecifications()


@dataclass
class TranscriptionRequest:
    """Standardized transcription request."""
    audio_file: BinaryIO
    filename: str
    mime_type: Optional[str] = None
    language: Optional[str] = None

    # Diarization options
    diarize: bool = False
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    known_speaker_names: Optional[List[str]] = None
    # known_speaker_references: Dict mapping speaker label to either BinaryIO or data URL string
    known_speaker_references: Optional[Dict[str, Any]] = None

    # Advanced options
    prompt: Optional[str] = None
    hotwords: Optional[str] = None  # Comma-separated words to bias recognition
    temperature: Optional[float] = None

    # Per-request model override. When set, the connector uses this model name
    # instead of its configured default. Only honoured if the connector supports
    # multiple models (asr_endpoint, openai_*, mistral). Issue #266.
    model: Optional[str] = None

    # Provider-specific options (passthrough)
    extra_options: Dict[str, Any] = field(default_factory=dict)

    # Saved provider task, for connectors with SUPPORTS_TASK_RESUME. A
    # connector calls save_provider_task(task) as soon as the service accepts
    # a job (and with None to forget it); a retry of the same processing job
    # receives that task back in provider_task and polls it instead of
    # submitting the audio again. Both are None outside a processing job.
    provider_task: Optional[Dict[str, Any]] = None
    save_provider_task: Optional[Callable[[Optional[Dict[str, Any]]], None]] = None
    # A stable name for what is transcribed, the same on every retry of it,
    # for services that accept an idempotency key. In a processing job it
    # names the job and the audio bytes (provider_task_context); a request
    # without a saved task (a chunk, incognito mode, the voice check) gets
    # one from provider_tasks.audio_task_key.
    task_key: Optional[str] = None
    # Stores (done, total) as the progress of the running processing job.
    # None outside a processing job.
    report_progress: Optional[Callable[[int, int], None]] = None
    # Stores what the service says it is doing (a Speakr stage word, or None)
    # on the running processing job. None outside a processing job.
    report_stage: Optional[Callable[[Optional[str]], None]] = None
    # True once the owner canceled the processing job. A connector that polls
    # asks at each poll and raises JobCanceled (src/services/job_control.py)
    # after removing its remote job. None outside a processing job.
    should_cancel: Optional[Callable[[], bool]] = None


@dataclass
class TranscriptionSegment:
    """Single segment of transcription with optional metadata."""
    text: str
    speaker: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    confidence: Optional[float] = None
    words: Optional[List[Dict[str, Any]]] = None


@dataclass
class TranscriptionResponse:
    """Standardized transcription response."""
    # Core content
    text: str                                    # Plain text transcription
    segments: Optional[List[TranscriptionSegment]] = None  # Detailed segments

    # Metadata
    language: Optional[str] = None               # Detected language
    duration: Optional[float] = None             # Audio duration in seconds

    # Speaker information
    speakers: Optional[List[str]] = None         # List of speakers found
    speaker_embeddings: Optional[Dict[str, List[float]]] = None

    # Provider info
    provider: str = ""
    model: str = ""

    # Raw response for debugging
    raw_response: Optional[Dict[str, Any]] = None

    def normalize_speaker_labels(self) -> Dict[Any, str]:
        """
        Bring the speaker labels of this result to the form SPEAKER_NN.

        Services number voices in different ways ("A", "S01", "speaker_0").
        The mapping of src/utils/speaker_labels.diarization_label_map() is
        applied to everything in this result that is keyed by a label: the
        speaker of each segment, `speakers`, the keys of
        `speaker_embeddings` and the "[label]:" line prefixes of `text`.
        `raw_response` keeps what the service sent.

        Labels are read in the order segments, `speakers`, embedding keys.
        A result that is already normalized is not changed, so a second call
        does nothing. Returns the mapping that was applied ({} for none).
        """
        from src.utils.speaker_labels import diarization_label_map

        def usable(label):
            return isinstance(label, (str, int)) and not isinstance(label, bool)

        labels = [seg.speaker for seg in self.segments or []]
        labels += list(self.speakers or [])
        labels += list(self.speaker_embeddings or {})
        mapping = diarization_label_map([label for label in labels if usable(label)])
        if not mapping:
            return {}

        def new(label):
            return mapping.get(label, label) if usable(label) else label

        for seg in self.segments or []:
            seg.speaker = new(seg.speaker)
        if self.speakers:
            self.speakers = list(dict.fromkeys(new(label) for label in self.speakers))
        if self.speaker_embeddings:
            self.speaker_embeddings = {new(label): vector for label, vector in self.speaker_embeddings.items()}
        if isinstance(self.text, str) and self.text:
            by_text = {str(label): target for label, target in mapping.items()}
            prefix = re.compile(r'^\[(' + '|'.join(re.escape(label) for label in by_text) + r')\](?=:)',
                                re.MULTILINE)
            self.text = prefix.sub(lambda match: f'[{by_text[match.group(1)]}]', self.text)
        return mapping

    def to_storage_format(self) -> str:
        """
        Convert to the JSON format used for storage in database.

        The speaker labels are normalized first (normalize_speaker_labels),
        in place: every stored transcript passes through this method, so the
        segments, `speakers` and `speaker_embeddings` of this result carry
        the stored labels afterwards.

        Returns a JSON string in the format expected by the existing codebase:
        [
            {
                "speaker": "SPEAKER_00",
                "sentence": "Text here",
                "start_time": 0.0,
                "end_time": 5.5
            },
            ...
        ]
        """
        self.normalize_speaker_labels()
        if self.segments:
            return json.dumps([
                {
                    'speaker': seg.speaker or 'Unknown Speaker',
                    'sentence': seg.text,
                    'start_time': seg.start_time,
                    'end_time': seg.end_time
                }
                for seg in self.segments
            ], ensure_ascii=False)
        # If no segments, return plain text (for non-diarized transcriptions)
        return self.text

    def has_diarization(self) -> bool:
        """Check if this response contains diarization data."""
        if not self.segments:
            return False
        return any(seg.speaker for seg in self.segments)


class BaseTranscriptionConnector(ABC):
    """Abstract base class for transcription connectors."""

    # Class-level capability declarations - subclasses should override
    CAPABILITIES: Set[TranscriptionCapability] = set()
    PROVIDER_NAME: str = "unknown"

    # Provider-specific constraints - subclasses should override
    SPECIFICATIONS: ConnectorSpecifications = DEFAULT_SPECIFICATIONS

    # Submit-and-poll services that charge per job set this and use
    # TranscriptionRequest.provider_task / save_provider_task.
    SUPPORTS_TASK_RESUME: bool = False

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize connector with configuration.

        Args:
            config: Provider-specific configuration dict
        """
        self.config = config
        self._validate_config()

    @abstractmethod
    def _validate_config(self) -> None:
        """
        Validate required configuration is present.

        Raises:
            ConfigurationError: If required config is missing or invalid
        """
        pass

    @abstractmethod
    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Perform transcription.

        Args:
            request: Standardized transcription request

        Returns:
            Standardized transcription response

        Raises:
            TranscriptionError: On transcription failure
            ConfigurationError: On configuration issues
        """
        pass

    def supports(self, capability: TranscriptionCapability) -> bool:
        """Check if connector supports a capability."""
        return capability in self.CAPABILITIES

    def _effective_model(self, request: 'TranscriptionRequest') -> str:
        """Resolve which model to use for a given request.

        Honours the per-request override (issue #266) if present, otherwise
        falls back to the connector's configured default model.
        """
        override = (request.model or '').strip() if request and request.model else ''
        return override or getattr(self, 'model', '') or ''

    def get_capabilities(self) -> Set[TranscriptionCapability]:
        """Get all supported capabilities."""
        return self.CAPABILITIES.copy()

    @property
    def supports_diarization(self) -> bool:
        """Check if connector supports speaker diarization."""
        return TranscriptionCapability.DIARIZATION in self.CAPABILITIES

    @property
    def supports_chunking(self) -> bool:
        """Check if connector supports automatic file chunking."""
        return TranscriptionCapability.CHUNKING in self.CAPABILITIES

    @property
    def supports_speaker_count_control(self) -> bool:
        """Check if connector supports min/max speaker count parameters."""
        return TranscriptionCapability.SPEAKER_COUNT_CONTROL in self.CAPABILITIES

    @property
    def supports_exact_speaker_count(self) -> bool:
        """Check if connector accepts an exact speaker count (no range)."""
        return TranscriptionCapability.EXACT_SPEAKER_COUNT in self.CAPABILITIES

    @property
    def supports_hotwords(self) -> bool:
        """Check if connector accepts hotword/keyword biasing input."""
        return TranscriptionCapability.HOTWORDS in self.CAPABILITIES

    @property
    def supports_initial_prompt(self) -> bool:
        """Check if connector accepts an initial prompt / context hint."""
        return TranscriptionCapability.INITIAL_PROMPT in self.CAPABILITIES

    @property
    def supports_language_selection(self) -> bool:
        """Check if connector accepts an explicit transcription language."""
        return TranscriptionCapability.LANGUAGE_SELECTION in self.CAPABILITIES

    @property
    def specifications(self) -> ConnectorSpecifications:
        """Get connector specifications."""
        return self.SPECIFICATIONS

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """
        Return JSON schema for this connector's configuration.
        Useful for admin UI and validation.

        Returns:
            JSON schema dict describing required and optional config
        """
        return {}

    def list_models(self) -> List[Dict[str, Any]]:
        """Discover models the upstream provider exposes.

        Connectors that talk to an OpenAI-compatible /v1/models endpoint
        should override this. Each item is a dict with at minimum 'id' and
        an optional 'label' or 'owned_by'. The default raises
        NotImplementedError so callers can detect unsupported providers.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement list_models()"
        )

    def health_check(self) -> bool:
        """
        Check if the connector is properly configured and reachable.

        Returns:
            True if the connector is healthy, False otherwise
        """
        return True
