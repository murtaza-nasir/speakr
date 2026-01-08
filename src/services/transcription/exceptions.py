"""
Custom exceptions for transcription services.
"""


class TranscriptionError(Exception):
    """Base exception for transcription errors."""
    pass


class ConfigurationError(TranscriptionError):
    """Configuration-related errors (missing or invalid config)."""
    pass


class ProviderError(TranscriptionError):
    """Provider/API errors."""

    def __init__(self, message: str, provider: str = None, status_code: int = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class AudioFormatError(TranscriptionError):
    """Unsupported audio format errors."""
    pass


class ChunkingError(TranscriptionError):
    """Errors during file chunking."""
    pass
