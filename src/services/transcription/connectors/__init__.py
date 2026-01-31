"""
Transcription connector implementations.
"""

from .openai_whisper import OpenAIWhisperConnector
from .openai_transcribe import OpenAITranscribeConnector
from .asr_endpoint import ASREndpointConnector
from .azure_openai_transcribe import AzureOpenAITranscribeConnector

__all__ = [
    'OpenAIWhisperConnector',
    'OpenAITranscribeConnector',
    'ASREndpointConnector',
    'AzureOpenAITranscribeConnector',
]
