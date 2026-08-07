"""
Transcription connector implementations.
"""

from .openai_whisper import OpenAIWhisperConnector
from .openai_transcribe import OpenAITranscribeConnector
from .asr_endpoint import ASREndpointConnector
from .azure_openai_transcribe import AzureOpenAITranscribeConnector
from .mistral import MistralTranscriptionConnector
from .vibevoice import VibeVoiceTranscriptionConnector
from .mossland import MosslandTranscriptionConnector
from .openasr import OpenASRTranscriptionConnector

__all__ = [
    'OpenAIWhisperConnector',
    'OpenAITranscribeConnector',
    'ASREndpointConnector',
    'AzureOpenAITranscribeConnector',
    'MistralTranscriptionConnector',
    'VibeVoiceTranscriptionConnector',
    'MosslandTranscriptionConnector',
    'OpenASRTranscriptionConnector',
]
