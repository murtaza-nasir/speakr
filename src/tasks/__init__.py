"""
Background task functions for asynchronous processing.

Note: Legacy functions (transcribe_audio_asr, transcribe_single_file, transcribe_with_chunking)
were removed. All transcription now uses the connector architecture via transcribe_audio_task.
"""

from .processing import (
    generate_title_task,
    generate_summary_only_task,
    extract_events_from_transcript,
    extract_audio_from_video,
    compress_lossless_audio,
    transcribe_audio_task,
    transcribe_with_connector,
    transcribe_chunks_with_connector,
    transcribe_incognito,
)

__all__ = [
    'generate_title_task',
    'generate_summary_only_task',
    'extract_events_from_transcript',
    'extract_audio_from_video',
    'compress_lossless_audio',
    'transcribe_audio_task',
    'transcribe_with_connector',
    'transcribe_chunks_with_connector',
    'transcribe_incognito',
]
