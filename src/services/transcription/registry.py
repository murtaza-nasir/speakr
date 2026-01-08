"""
Connector registry for managing transcription connectors.

Provides factory pattern for creating and managing transcription connectors,
with auto-detection from environment variables for backwards compatibility.
"""

import os
import logging
from typing import Dict, Any, Optional, Type, List

from .base import BaseTranscriptionConnector, TranscriptionCapability, TranscriptionRequest, TranscriptionResponse
from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """
    Registry for managing transcription connectors.

    Singleton pattern - use get_registry() to get the shared instance.
    """

    _instance = None
    _connectors: Dict[str, Type[BaseTranscriptionConnector]] = {}
    _active_connector: Optional[BaseTranscriptionConnector] = None
    _connector_name: str = ""
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._register_builtin_connectors()
        self._initialized = True

    def _register_builtin_connectors(self):
        """Register all built-in connectors."""
        from .connectors.openai_whisper import OpenAIWhisperConnector
        from .connectors.openai_transcribe import OpenAITranscribeConnector
        from .connectors.asr_endpoint import ASREndpointConnector

        self.register('openai_whisper', OpenAIWhisperConnector)
        self.register('openai_transcribe', OpenAITranscribeConnector)
        self.register('asr_endpoint', ASREndpointConnector)

    def register(self, name: str, connector_class: Type[BaseTranscriptionConnector]):
        """
        Register a connector class.

        Args:
            name: Unique name for the connector
            connector_class: The connector class to register
        """
        self._connectors[name] = connector_class
        logger.debug(f"Registered transcription connector: {name}")

    def get_connector_class(self, name: str) -> Type[BaseTranscriptionConnector]:
        """
        Get a connector class by name.

        Args:
            name: The connector name

        Returns:
            The connector class

        Raises:
            ConfigurationError: If connector not found
        """
        if name not in self._connectors:
            raise ConfigurationError(
                f"Unknown connector: {name}. Available: {list(self._connectors.keys())}"
            )
        return self._connectors[name]

    def create_connector(self, name: str, config: Dict[str, Any]) -> BaseTranscriptionConnector:
        """
        Create a connector instance.

        Args:
            name: The connector name
            config: Configuration dict for the connector

        Returns:
            Configured connector instance
        """
        connector_class = self.get_connector_class(name)
        return connector_class(config)

    def list_connectors(self) -> List[Dict[str, Any]]:
        """
        List all registered connectors with their capabilities.

        Returns:
            List of connector info dicts
        """
        result = []
        for name, cls in self._connectors.items():
            result.append({
                'name': name,
                'provider_name': cls.PROVIDER_NAME,
                'capabilities': [c.name for c in cls.CAPABILITIES],
                'config_schema': cls.get_config_schema()
            })
        return result

    def initialize_from_env(self) -> BaseTranscriptionConnector:
        """
        Initialize the active connector from environment variables.

        Auto-detection priority:
        1. TRANSCRIPTION_CONNECTOR - explicit connector name
        2. ASR_BASE_URL is set - use ASR endpoint (smarter detection)
           - USE_ASR_ENDPOINT=true also works (backwards compat, with deprecation warning)
        3. TRANSCRIPTION_MODEL contains 'gpt-4o' - use OpenAI Transcribe
        4. TRANSCRIPTION_MODEL is set - use OpenAI Whisper with that model
        5. Default to OpenAI Whisper (whisper-1)

        Returns:
            The initialized connector
        """
        connector_name = os.environ.get('TRANSCRIPTION_CONNECTOR', '').lower().strip()

        if not connector_name:
            # Auto-detect based on existing config for backwards compatibility
            asr_base_url = os.environ.get('ASR_BASE_URL', '').strip()
            use_asr_flag = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
            transcription_model = os.environ.get('TRANSCRIPTION_MODEL', '').lower()
            whisper_model = os.environ.get('WHISPER_MODEL', '').lower()

            # Priority 2: ASR endpoint - check ASR_BASE_URL directly (smarter than flag)
            if asr_base_url:
                connector_name = 'asr_endpoint'
                logger.info("Auto-detected ASR endpoint from ASR_BASE_URL")
            elif use_asr_flag:
                # Backwards compat: USE_ASR_ENDPOINT flag still works
                connector_name = 'asr_endpoint'
                logger.warning(
                    "USE_ASR_ENDPOINT=true is deprecated. "
                    "Set ASR_BASE_URL instead for auto-detection, or use TRANSCRIPTION_CONNECTOR=asr_endpoint"
                )
            # Priority 3: Model-based detection
            elif transcription_model and 'gpt-4o' in transcription_model:
                connector_name = 'openai_transcribe'
                logger.info(f"Auto-detected OpenAI Transcribe from TRANSCRIPTION_MODEL={transcription_model}")
            # Priority 4 & 5: OpenAI Whisper (with custom or default model)
            else:
                connector_name = 'openai_whisper'
                model = transcription_model or whisper_model or 'whisper-1'
                logger.info(f"Using OpenAI Whisper connector with model: {model}")

        config = self._build_config_from_env(connector_name)

        try:
            self._active_connector = self.create_connector(connector_name, config)
            self._connector_name = connector_name

            logger.info(f"Initialized transcription connector: {connector_name}")
            logger.info(f"Capabilities: {[c.name for c in self._active_connector.get_capabilities()]}")

            return self._active_connector

        except Exception as e:
            logger.error(f"Failed to initialize connector '{connector_name}': {e}")
            raise ConfigurationError(f"Failed to initialize connector '{connector_name}': {e}") from e

    def _build_config_from_env(self, connector_name: str) -> Dict[str, Any]:
        """
        Build connector config from environment variables.

        Args:
            connector_name: The connector to build config for

        Returns:
            Configuration dict
        """
        if connector_name == 'asr_endpoint':
            base_url = os.environ.get('ASR_BASE_URL', '')
            if base_url:
                base_url = base_url.split('#')[0].strip()

            return {
                'base_url': base_url,
                'timeout': int(os.environ.get('ASR_TIMEOUT', os.environ.get('asr_timeout_seconds', '1800'))),
                'diarize': os.environ.get('ASR_DIARIZE', 'true').lower() == 'true',
                'return_speaker_embeddings': os.environ.get('ASR_RETURN_SPEAKER_EMBEDDINGS', 'false').lower() == 'true'
            }

        elif connector_name == 'openai_transcribe':
            base_url = os.environ.get('TRANSCRIPTION_BASE_URL', 'https://api.openai.com/v1')
            if base_url:
                base_url = base_url.split('#')[0].strip()

            return {
                'api_key': os.environ.get('TRANSCRIPTION_API_KEY', ''),
                'base_url': base_url,
                'model': os.environ.get('TRANSCRIPTION_MODEL', 'gpt-4o-transcribe')
            }

        else:  # openai_whisper (default)
            base_url = os.environ.get('TRANSCRIPTION_BASE_URL', '')
            if base_url:
                base_url = base_url.split('#')[0].strip()

            # Support both TRANSCRIPTION_MODEL and legacy WHISPER_MODEL
            # TRANSCRIPTION_MODEL takes priority for custom Whisper variants
            model = os.environ.get('TRANSCRIPTION_MODEL', '') or os.environ.get('WHISPER_MODEL', 'whisper-1')

            return {
                'api_key': os.environ.get('TRANSCRIPTION_API_KEY', ''),
                'base_url': base_url or None,
                'model': model
            }

    def get_active_connector(self) -> BaseTranscriptionConnector:
        """
        Get the currently active connector.

        Initializes from environment if not already initialized.

        Returns:
            The active connector
        """
        if not self._active_connector:
            self.initialize_from_env()
        return self._active_connector

    def get_active_connector_name(self) -> str:
        """Get the name of the currently active connector."""
        if not self._active_connector:
            self.initialize_from_env()
        return self._connector_name

    def reinitialize(self) -> BaseTranscriptionConnector:
        """
        Force re-initialization of the connector.

        Useful when environment variables have changed.

        Returns:
            The newly initialized connector
        """
        self._active_connector = None
        self._connector_name = ""
        return self.initialize_from_env()


# Global registry instance
_registry: Optional[ConnectorRegistry] = None


def get_registry() -> ConnectorRegistry:
    """Get the global connector registry."""
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry()
    return _registry


# Convenience aliases
connector_registry = get_registry()


def transcribe(request: TranscriptionRequest) -> TranscriptionResponse:
    """
    Transcribe audio using the active connector.

    This is a convenience function that uses the global registry.

    Args:
        request: The transcription request

    Returns:
        Transcription response
    """
    connector = get_registry().get_active_connector()
    return connector.transcribe(request)


def get_connector() -> BaseTranscriptionConnector:
    """Get the active transcription connector."""
    return get_registry().get_active_connector()


def supports_diarization() -> bool:
    """Check if the active connector supports diarization."""
    return get_registry().get_active_connector().supports_diarization
