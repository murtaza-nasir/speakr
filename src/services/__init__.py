"""
Service layer for business logic.
"""

from .embeddings import *
from .llm import *
from .document import *
from .retention import *

__all__ = [
    # Embedding services
    'get_embedding_model',
    'chunk_transcription',
    'generate_embeddings',
    'serialize_embedding',
    'deserialize_embedding',
    'get_accessible_recording_ids',
    'process_recording_chunks',
    'basic_text_search_chunks',
    'semantic_search_chunks',
    # LLM services
    'is_gpt5_model',
    'is_using_openai_api',
    'call_llm_completion',
    'call_chat_completion',
    'chat_client',
    'format_api_error_message',
    # Document services
    'process_markdown_to_docx',
    # Retention services
    'is_recording_exempt_from_deletion',
    'get_retention_days_for_recording',
    'process_auto_deletion',
]
