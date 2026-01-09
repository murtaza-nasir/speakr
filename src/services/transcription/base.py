"""
Base classes and data types for transcription connectors.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Dict, Any, BinaryIO, Set, Type, FrozenSet


class TranscriptionCapability(Enum):
    """Capabilities that connectors can declare support for."""
    DIARIZATION = auto()           # Speaker diarization
    CHUNKING = auto()              # Automatic file chunking for large files
    TIMESTAMPS = auto()            # Word/segment timestamps
    LANGUAGE_DETECTION = auto()    # Auto language detection
    KNOWN_SPEAKERS = auto()        # Support for known speaker references (future)
    SPEAKER_EMBEDDINGS = auto()    # Return speaker embeddings
    SPEAKER_COUNT_CONTROL = auto() # Support for min/max speaker count parameters
    STREAMING = auto()             # Real-time streaming transcription


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
    temperature: Optional[float] = None

    # Provider-specific options (passthrough)
    extra_options: Dict[str, Any] = field(default_factory=dict)


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

    def to_storage_format(self) -> str:
        """
        Convert to the JSON format used for storage in database.

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
        if self.segments:
            return json.dumps([
                {
                    'speaker': seg.speaker or 'Unknown Speaker',
                    'sentence': seg.text,
                    'start_time': seg.start_time,
                    'end_time': seg.end_time
                }
                for seg in self.segments
            ])
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

    def health_check(self) -> bool:
        """
        Check if the connector is properly configured and reachable.

        Returns:
            True if the connector is healthy, False otherwise
        """
        return True
