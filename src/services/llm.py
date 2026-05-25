"""
LLM API integration services (OpenAI/OpenRouter).
"""

import os
import re
import json
import time
import logging
import httpx
from openai import OpenAI, APITimeoutError

# Use standard logging instead of current_app.logger for context independence
logger = logging.getLogger(__name__)


class TokenBudgetExceeded(Exception):
    """Raised when user exceeds their token budget."""
    def __init__(self, message, usage_percentage=100):
        self.message = message
        self.usage_percentage = usage_percentage
        super().__init__(message)

from src.utils import safe_json_loads, extract_json_object

# Configuration - use TEXT_MODEL_* variables for LLM
TEXT_MODEL_API_KEY = os.environ.get("TEXT_MODEL_API_KEY")
TEXT_MODEL_BASE_URL = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
if TEXT_MODEL_BASE_URL:
    TEXT_MODEL_BASE_URL = TEXT_MODEL_BASE_URL.split('#')[0].strip()
TEXT_MODEL_NAME = os.environ.get("TEXT_MODEL_NAME", "openai/gpt-3.5-turbo")


def _resolve_db_setting(key, default=None):
    """Resolve a setting from the database SystemSetting table.

    Returns (value, source) where *source* is ``'database'`` or ``None`` when
    no DB entry exists (meaning we should fall back to the env-var value).
    """
    try:
        # Avoid circular import at module level – import lazily
        from src.models import SystemSetting
        raw = SystemSetting.get_setting(key, None)
        if raw is not None and str(raw).strip():
            return str(raw).strip(), 'database'
    except Exception as exc:
        logger.debug(f"Could not read SystemSetting {key}: {exc}")
    return default, None


def resolve_llm_model_name():
    """Return the effective LLM model name.

    Resolution order:
      1. SystemSetting ``llm_model_name`` (admin UI override)
      2. TEXT_MODEL_NAME env var (module-level constant)
      3. Hard-coded default ``openai/gpt-3.5-turbo``
    """
    val, source = _resolve_db_setting('llm_model_name')
    if val:
        return val
    return TEXT_MODEL_NAME


def resolve_llm_base_url():
    """Return the effective LLM base URL.

    Resolution order:
      1. SystemSetting ``llm_base_url`` (admin UI override)
      2. TEXT_MODEL_BASE_URL env var (module-level constant)
      3. Hard-coded default ``https://openrouter.ai/api/v1``
    """
    val, source = _resolve_db_setting('llm_base_url')
    if val:
        return val
    base = TEXT_MODEL_BASE_URL or "https://openrouter.ai/api/v1"
    if base:
        base = base.split('#')[0].strip()
    return base


def resolve_llm_config():
    """Return a dict with the effective LLM configuration.

    Keys: ``model_name``, ``base_url``, ``source`` (one of 'database', 'env', 'default').
    Also returns ``requires_restart`` when DB and env values differ for base_url.
    """
    db_model, _ = _resolve_db_setting('llm_model_name')
    db_base, _ = _resolve_db_setting('llm_base_url')

    model_source = 'database' if db_model else ('env' if os.environ.get('TEXT_MODEL_NAME') else 'default')
    base_source = 'database' if db_base else ('env' if os.environ.get('TEXT_MODEL_BASE_URL') else 'default')

    effective_model = db_model or TEXT_MODEL_NAME
    effective_base = (db_base or TEXT_MODEL_BASE_URL or "https://openrouter.ai/api/v1")
    if effective_base:
        effective_base = effective_base.split('#')[0].strip()

    requires_restart = False
    if db_base and os.environ.get('TEXT_MODEL_BASE_URL'):
        db_clean = db_base.split('#')[0].strip()
        env_clean = (os.environ.get('TEXT_MODEL_BASE_URL') or '').split('#')[0].strip()
        requires_restart = db_clean != env_clean

    return {
        'model_name': effective_model,
        'base_url': effective_base,
        'model_source': model_source,
        'base_source': base_source,
        'requires_restart': requires_restart,
    }


def discover_llm_models():
    """Probe the active LLM provider's /v1/models endpoint.

    Returns a dict with ``connector``, ``supported``, and ``models`` keys.
    Empty model list if the provider does not expose discovery or is unreachable.
    """
    base_url = resolve_llm_base_url()
    api_key = TEXT_MODEL_API_KEY or CHAT_MODEL_API_KEY

    connector_name = 'openrouter'
    if 'api.openai.com' in (base_url or ''):
        connector_name = 'openai'
    elif 'generativelanguage.googleapis.com' in (base_url or ''):
        connector_name = 'google-gemini'

    try:
        import httpx as _httpx
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        base = (base_url or '').rstrip('/')
        models_path = f"{base}/v1/models" if not base.endswith('/v1') else f"{base}/models"
        resp = _httpx.get(models_path, headers=headers, timeout=15)
        if resp.status_code != 200:
            body_preview = resp.text[:300]
            return {
                'connector': connector_name,
                'supported': False,
                'models': [],
                'message': f'HTTP {resp.status_code} from /v1/models (body: {body_preview})',
            }

        data = resp.json()

        # Try multiple response formats (OpenAI uses 'data', some providers use 'models')
        raw_list = None
        if isinstance(data, dict):
            raw_list = data.get('data') or data.get('models') or []
        elif isinstance(data, list):
            raw_list = data

        models = []
        for item in (raw_list or []):
            mid = ''
            if isinstance(item, dict):
                mid = item.get('id', '') or item.get('model_id', '') or item.get('name', '')
                if mid:
                    models.append({
                        'id': str(mid),
                        'label': item.get('label') or item.get('name') or str(mid),
                        'owned_by': item.get('owned_by') or item.get('provider') or '',
                    })

        if not models:
            body_preview = resp.text[:300]
            return {
                'connector': connector_name,
                'supported': False,
                'models': [],
                'message': f'/v1/models returned no models (response preview: {body_preview})',
            }

        return {
            'connector': connector_name,
            'supported': True,
            'models': models,
        }
    except httpx.ConnectError as exc:
        logger.debug(f"LLM model discovery connection error for {base_url}: {exc}")
        return {
            'connector': connector_name,
            'supported': False,
            'models': [],
            'message': f'Connection refused — is the upstream service running at {base_url}?',
        }
    except httpx.TimeoutException as exc:
        logger.debug(f"LLM model discovery timeout for {base_url}: {exc}")
        return {
            'connector': connector_name,
            'supported': False,
            'models': [],
            'message': f'Request timed out after 15s (provider at {base_url} not responding)',
        }
    except Exception as exc:
        logger.debug(f"LLM model discovery failed for {base_url}: {exc}")
        return {
            'connector': connector_name,
            'supported': False,
            'models': [],
            'message': f'Discovery error: {exc}',
        }

# Chat model configuration (optional - falls back to TEXT_MODEL_* if not set)
CHAT_MODEL_API_KEY = os.environ.get("CHAT_MODEL_API_KEY")
CHAT_MODEL_BASE_URL = os.environ.get("CHAT_MODEL_BASE_URL")
if CHAT_MODEL_BASE_URL:
    CHAT_MODEL_BASE_URL = CHAT_MODEL_BASE_URL.split('#')[0].strip()
CHAT_MODEL_NAME = os.environ.get("CHAT_MODEL_NAME")

# Chat-specific GPT-5 settings (optional - falls back to main GPT5_* settings)
CHAT_GPT5_REASONING_EFFORT = os.environ.get("CHAT_GPT5_REASONING_EFFORT")
CHAT_GPT5_VERBOSITY = os.environ.get("CHAT_GPT5_VERBOSITY")

# Streaming options - disable for LLM servers that don't support OpenAI's stream_options
ENABLE_STREAM_OPTIONS = os.environ.get("ENABLE_STREAM_OPTIONS", "true").lower() == "true"

# LLM timeout and retry configuration
# Read timeout controls how long to wait for model inference (the bottleneck for local models)
# Connect/write stay short so misconfigured URLs fail fast instead of hanging
LLM_REQUEST_TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "600"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))
LLM_CONNECT_TIMEOUT = float(os.environ.get("LLM_CONNECT_TIMEOUT", "30"))
LLM_WRITE_TIMEOUT = float(os.environ.get("LLM_WRITE_TIMEOUT", "30"))
llm_timeout = httpx.Timeout(
    connect=LLM_CONNECT_TIMEOUT,
    read=float(LLM_REQUEST_TIMEOUT),
    write=LLM_WRITE_TIMEOUT,
    pool=30.0,
)


def get_chat_config():
    """
    Get chat model configuration, falling back to resolved TEXT_MODEL if not set.

    Resolution order for fallback:
      1. SystemSetting ``chat_model_name`` / ``chat_base_url`` (admin UI override)
      2. CHAT_MODEL_* env vars
      3. SystemSetting ``llm_model_name`` / ``llm_base_url`` (admin UI override)
      4. TEXT_MODEL_* env vars

    Returns a dict with api_key, base_url, model_name, and GPT-5 settings.
    """
    if CHAT_MODEL_API_KEY and CHAT_MODEL_NAME:
        return {
            'api_key': CHAT_MODEL_API_KEY,
            'base_url': CHAT_MODEL_BASE_URL or resolve_llm_base_url(),
            'model_name': CHAT_MODEL_NAME,
            'gpt5_reasoning_effort': CHAT_GPT5_REASONING_EFFORT or os.environ.get("GPT5_REASONING_EFFORT", "medium"),
            'gpt5_verbosity': CHAT_GPT5_VERBOSITY or os.environ.get("GPT5_VERBOSITY", "medium")
        }

    # Check for chat-specific DB overrides before falling back to main LLM config
    db_chat_model, _ = _resolve_db_setting('chat_model_name')
    db_chat_base, _ = _resolve_db_setting('chat_base_url')

    if db_chat_model:
        return {
            'api_key': TEXT_MODEL_API_KEY or CHAT_MODEL_API_KEY,
            'base_url': db_chat_base or resolve_llm_base_url(),
            'model_name': db_chat_model,
            'gpt5_reasoning_effort': os.environ.get("GPT5_REASONING_EFFORT", "medium"),
            'gpt5_verbosity': os.environ.get("GPT5_VERBOSITY", "medium")
        }

    return {
        'api_key': TEXT_MODEL_API_KEY,
        'base_url': resolve_llm_base_url(),
        'model_name': resolve_llm_model_name(),
        'gpt5_reasoning_effort': os.environ.get("GPT5_REASONING_EFFORT", "medium"),
        'gpt5_verbosity': os.environ.get("GPT5_VERBOSITY", "medium")
    }


# Set up HTTP client with custom headers for OpenRouter app identification
app_headers = {
    "HTTP-Referer": "https://github.com/murtaza-nasir/speakr",
    "X-Title": "Speakr - AI Audio Transcription",
    "User-Agent": "Speakr/1.0 (https://github.com/murtaza-nasir/speakr)"
}

http_client_no_proxy = httpx.Client(
    verify=True,
    headers=app_headers
)

# Create client with placeholder key if not provided (allows app to start)
try:
    api_key = TEXT_MODEL_API_KEY or "not-needed"
    client = OpenAI(
        api_key=api_key,
        base_url=TEXT_MODEL_BASE_URL,
        http_client=http_client_no_proxy,
        timeout=llm_timeout,
        max_retries=LLM_MAX_RETRIES,
    )
    # Always log the resolved timeout/retry values at startup so users can confirm
    # their LLM_REQUEST_TIMEOUT / LLM_MAX_RETRIES env vars actually took effect.
    logger.info(
        f"LLM client configured: read_timeout={LLM_REQUEST_TIMEOUT}s, "
        f"connect_timeout={LLM_CONNECT_TIMEOUT}s, write_timeout={LLM_WRITE_TIMEOUT}s, "
        f"max_retries={LLM_MAX_RETRIES}"
    )
except Exception as client_init_e:
    client = None

# Create chat client (may be same as main client if no separate config)
chat_client = None
try:
    chat_config = get_chat_config()
    if chat_config['api_key']:
        if CHAT_MODEL_API_KEY and CHAT_MODEL_API_KEY != TEXT_MODEL_API_KEY:
            # Separate chat configuration - create dedicated client
            chat_client = OpenAI(
                api_key=chat_config['api_key'],
                base_url=chat_config['base_url'],
                http_client=http_client_no_proxy,
                timeout=llm_timeout,
                max_retries=LLM_MAX_RETRIES,
            )
            logger.info(f"Separate chat client initialized: {chat_config['base_url']} / {chat_config['model_name']}")
        else:
            # Use same client as main LLM
            chat_client = client
except Exception as chat_client_init_e:
    logger.warning(f"Failed to initialize chat client, falling back to main client: {chat_client_init_e}")
    chat_client = client


def is_gpt5_model(model_name):
    """
    Check if the model is a GPT-5 series model that requires special API parameters.

    Args:
        model_name: The model name string

    Returns:
        Boolean indicating if this is a GPT-5 model
    """
    if not model_name:
        return False
    model_lower = model_name.lower()
    return model_lower.startswith('gpt-5') or model_lower in ['gpt-5', 'gpt-5-mini', 'gpt-5-nano', 'gpt-5-chat-latest']



def is_using_openai_api():
    """
    Check if we're using the official OpenAI API (not OpenRouter or other providers).

    Uses the resolved base URL (DB > env var) for accurate detection.

    Returns:
        Boolean indicating if this is the OpenAI API
    """
    return resolve_llm_base_url() and 'api.openai.com' in resolve_llm_base_url()



def call_llm_completion(messages, temperature=0.7, response_format=None, stream=False, max_tokens=None,
                        user_id=None, operation_type=None):
    """
    Centralized function for LLM API calls with proper error handling and logging.

    Args:
        messages: List of message dicts with 'role' and 'content'
        temperature: Sampling temperature (0-1) - ignored for GPT-5 models
        response_format: Optional response format dict (e.g., {"type": "json_object"})
        stream: Whether to stream the response
        max_tokens: Optional maximum tokens to generate
        user_id: Optional user ID for token tracking and budget enforcement
        operation_type: Optional operation type for token tracking (e.g., 'summarization', 'chat')

    Returns:
        OpenAI completion object or generator (if streaming)
    """
    if not client:
        raise ValueError("LLM client not initialized")

    if not TEXT_MODEL_API_KEY:
        raise ValueError("TEXT_MODEL_API_KEY not configured")

    # Check budget before making the call
    if user_id and operation_type:
        try:
            from src.services.token_tracking import token_tracker
            can_proceed, usage_pct, msg = token_tracker.check_budget(user_id)
            if not can_proceed:
                raise TokenBudgetExceeded(msg, usage_pct)
            if usage_pct >= 80:
                logger.warning(f"User {user_id} at {usage_pct:.1f}% of token budget")
        except TokenBudgetExceeded:
            raise
        except Exception as e:
            # Log but don't block on budget check errors
            logger.warning(f"Budget check failed for user {user_id}: {e}")

    try:
        # Resolve effective model name at runtime (DB > env var)
        effective_model = resolve_llm_model_name()

        # Check if we're using GPT-5 with OpenAI API
        using_gpt5 = is_gpt5_model(effective_model) and is_using_openai_api()

        completion_args = {
            "model": effective_model,
            "messages": messages,
            "stream": stream
        }

        # Add stream_options to get usage in final chunk for streaming
        # Some LLM servers don't support this OpenAI-specific option
        if stream and ENABLE_STREAM_OPTIONS:
            completion_args["stream_options"] = {"include_usage": True}

        if using_gpt5:
            # GPT-5 models don't support temperature, top_p, or logprobs
            # They use reasoning_effort and verbosity instead
            logger.debug(f"Using GPT-5 model: {effective_model} - applying GPT-5 specific parameters")

            # Get GPT-5 specific parameters from environment variables
            reasoning_effort = os.environ.get("GPT5_REASONING_EFFORT", "medium")  # minimal, low, medium, high
            verbosity = os.environ.get("GPT5_VERBOSITY", "medium")  # low, medium, high

            # Add GPT-5 specific parameters
            completion_args["reasoning_effort"] = reasoning_effort
            completion_args["verbosity"] = verbosity

            # Use max_completion_tokens instead of max_tokens for GPT-5
            if max_tokens:
                completion_args["max_completion_tokens"] = max_tokens
        else:
            # Non-GPT-5 models use standard parameters
            completion_args["temperature"] = temperature

            if max_tokens:
                completion_args["max_tokens"] = max_tokens

        if response_format:
            completion_args["response_format"] = response_format

        # Make the resolved max_tokens visible in logs so users can confirm
        # their SUMMARY_MAX_TOKENS / CHAT_MAX_TOKENS / etc. settings actually
        # took effect for a given operation. The "key" used here is
        # max_completion_tokens for GPT-5 and max_tokens otherwise.
        budget_key = 'max_completion_tokens' if using_gpt5 else 'max_tokens'
        logger.info(
            f"LLM call: operation={operation_type or 'unspecified'}, model={effective_model}, "
            f"{budget_key}={completion_args.get(budget_key, 'provider default')}"
        )

        request_started_at = time.monotonic()
        response = client.chat.completions.create(**completion_args)

        # Track usage for non-streaming calls
        if user_id and operation_type and not stream and response.usage:
            try:
                from src.services.token_tracking import token_tracker
                token_tracker.record_usage(
                    user_id=user_id,
                    operation_type=operation_type,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    model_name=effective_model,
                    cost=getattr(response.usage, 'cost', None)
                )
            except Exception as e:
                logger.warning(f"Failed to record token usage: {e}")

        # Debug log for empty responses
        if not stream and response.choices:
            content = response.choices[0].message.content
            if not content:
                logger.warning(f"LLM returned empty content. Model: {effective_model}, finish_reason: {response.choices[0].finish_reason}")
                # Log more details if available
                if hasattr(response.choices[0].message, 'refusal'):
                    logger.warning(f"Refusal: {response.choices[0].message.refusal}")
                if hasattr(response.choices[0].message, 'tool_calls') and response.choices[0].message.tool_calls:
                    logger.warning(f"Tool calls present: {response.choices[0].message.tool_calls}")

        return response

    except TokenBudgetExceeded:
        raise
    except APITimeoutError as e:
        elapsed = time.monotonic() - request_started_at if 'request_started_at' in locals() else None
        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "unknown"
        logger.error(
            f"LLM request timed out after {elapsed_str} (configured read_timeout={LLM_REQUEST_TIMEOUT}s, "
            f"max_retries={LLM_MAX_RETRIES}, model={effective_model}). "
            f"If the elapsed time is much shorter than the configured timeout, an upstream proxy or the "
            f"provider is closing the connection early. For reasoning models that take longer to think, "
            f"increase LLM_REQUEST_TIMEOUT."
        )
        raise
    except Exception as e:
        logger.error(f"LLM API call failed: {e}")
        raise


def call_chat_completion(messages, temperature=0.7, response_format=None, stream=False, max_tokens=None,
                         user_id=None, operation_type=None):
    """
    Chat-specific LLM completion function. Uses dedicated chat model if configured,
    otherwise falls back to standard TEXT_MODEL configuration.

    Args:
        messages: List of message dicts with 'role' and 'content'
        temperature: Sampling temperature (0-1) - ignored for GPT-5 models
        response_format: Optional response format dict (e.g., {"type": "json_object"})
        stream: Whether to stream the response
        max_tokens: Optional maximum tokens to generate
        user_id: Optional user ID for token tracking and budget enforcement
        operation_type: Optional operation type for token tracking (e.g., 'chat')

    Returns:
        OpenAI completion object or generator (if streaming)
    """
    effective_client = chat_client if chat_client else client
    chat_config = get_chat_config()

    if not effective_client:
        raise ValueError("Chat LLM client not initialized")

    if not chat_config['api_key']:
        raise ValueError("Chat model API key not configured")

    # Check budget before making the call
    if user_id and operation_type:
        try:
            from src.services.token_tracking import token_tracker
            can_proceed, usage_pct, msg = token_tracker.check_budget(user_id)
            if not can_proceed:
                raise TokenBudgetExceeded(msg, usage_pct)
            if usage_pct >= 80:
                logger.warning(f"User {user_id} at {usage_pct:.1f}% of token budget")
        except TokenBudgetExceeded:
            raise
        except Exception as e:
            # Log but don't block on budget check errors
            logger.warning(f"Budget check failed for user {user_id}: {e}")

    try:
        model_name = chat_config['model_name']
        base_url = chat_config['base_url'] or ''

        # Check if we're using GPT-5 with OpenAI API
        using_gpt5 = is_gpt5_model(model_name) and 'api.openai.com' in base_url

        completion_args = {
            "model": model_name,
            "messages": messages,
            "stream": stream
        }

        # Add stream_options to get usage in final chunk for streaming
        # Some LLM servers don't support this OpenAI-specific option
        if stream and ENABLE_STREAM_OPTIONS:
            completion_args["stream_options"] = {"include_usage": True}

        if using_gpt5:
            logger.debug(f"Using GPT-5 chat model: {model_name}")
            # Use chat-specific GPT-5 settings from config
            completion_args["reasoning_effort"] = chat_config['gpt5_reasoning_effort']
            completion_args["verbosity"] = chat_config['gpt5_verbosity']

            if max_tokens:
                completion_args["max_completion_tokens"] = max_tokens
        else:
            completion_args["temperature"] = temperature
            if max_tokens:
                completion_args["max_tokens"] = max_tokens

        if response_format:
            completion_args["response_format"] = response_format

        # Visibility: surface the resolved budget per call so admins can
        # confirm CHAT_MAX_TOKENS or per-call overrides took effect.
        budget_key = 'max_completion_tokens' if using_gpt5 else 'max_tokens'
        logger.info(
            f"Chat LLM call: operation={operation_type or 'unspecified'}, model={model_name}, "
            f"{budget_key}={completion_args.get(budget_key, 'provider default')}"
        )

        request_started_at = time.monotonic()
        response = effective_client.chat.completions.create(**completion_args)

        # Track usage for non-streaming calls
        if user_id and operation_type and not stream and response.usage:
            try:
                from src.services.token_tracking import token_tracker
                token_tracker.record_usage(
                    user_id=user_id,
                    operation_type=operation_type,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    model_name=model_name,
                    cost=getattr(response.usage, 'cost', None)
                )
            except Exception as e:
                logger.warning(f"Failed to record token usage: {e}")

        # Debug log for empty responses
        if not stream and response.choices:
            content = response.choices[0].message.content
            if not content:
                logger.warning(f"Chat LLM returned empty content. Model: {model_name}, finish_reason: {response.choices[0].finish_reason}")

        return response

    except TokenBudgetExceeded:
        raise
    except APITimeoutError as e:
        elapsed = time.monotonic() - request_started_at if 'request_started_at' in locals() else None
        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "unknown"
        logger.error(
            f"Chat LLM request timed out after {elapsed_str} (configured read_timeout={LLM_REQUEST_TIMEOUT}s, "
            f"max_retries={LLM_MAX_RETRIES}, model={model_name}). "
            f"If the elapsed time is much shorter than the configured timeout, an upstream proxy or the "
            f"provider is closing the connection early. For reasoning models that take longer to think, "
            f"increase LLM_REQUEST_TIMEOUT."
        )
        raise
    except Exception as e:
        logger.error(f"Chat LLM API call failed: {e}")
        raise


def format_api_error_message(error_str):
    """
    Formats API error messages to be more user-friendly.
    Specifically handles token limit errors with helpful suggestions.
    """
    error_lower = error_str.lower()
    
    # Check for token limit errors
    if 'maximum context length' in error_lower and 'tokens' in error_lower:
        return "[Summary generation failed: The transcription is too long for AI processing. Request your admin to try using a different LLM with a larger context size, or set a limit for the transcript_length_limit in the system settings.]"
    
    # Check for other common API errors
    if 'rate limit' in error_lower:
        return "[Summary generation failed: API rate limit exceeded. Please try again in a few minutes.]"
    
    if 'insufficient funds' in error_lower or 'quota exceeded' in error_lower:
        return "[Summary generation failed: API quota exceeded. Please contact support.]"
    
    if 'timeout' in error_lower:
        return "[Summary generation failed: Request timed out. Please try again.]"
    
    # For other errors, show a generic message
    return f"[Summary generation failed: {error_str}]"


def process_streaming_with_thinking(stream, user_id=None, operation_type=None, model_name=None, app=None):
    """
    Generator that processes a streaming response and separates thinking content.
    Yields SSE-formatted data with 'delta' for regular content and 'thinking' for thinking content.

    Args:
        stream: The streaming response from the LLM API
        user_id: Optional user ID for token tracking
        operation_type: Optional operation type for token tracking
        model_name: Optional model name for token tracking
        app: Optional Flask app instance for database context in generators
    """
    content_buffer = ""
    in_thinking = False
    thinking_buffer = ""

    for chunk in stream:
        # Check for usage in final chunk (from stream_options={'include_usage': True})
        if hasattr(chunk, 'usage') and chunk.usage and user_id and operation_type:
            try:
                from src.services.token_tracking import token_tracker
                effective_model = model_name or resolve_llm_model_name()
                # Use app context if provided (needed for generators where context may be lost)
                if app:
                    with app.app_context():
                        token_tracker.record_usage(
                            user_id=user_id,
                            operation_type=operation_type,
                            prompt_tokens=chunk.usage.prompt_tokens,
                            completion_tokens=chunk.usage.completion_tokens,
                            total_tokens=chunk.usage.total_tokens,
                            model_name=effective_model,
                            cost=getattr(chunk.usage, 'cost', None)
                        )
                else:
                    token_tracker.record_usage(
                        user_id=user_id,
                        operation_type=operation_type,
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.total_tokens,
                        model_name=effective_model,
                        cost=getattr(chunk.usage, 'cost', None)
                    )
            except Exception as e:
                logger.warning(f"Failed to record streaming token usage: {e}")

        # Process content delta
        if chunk.choices and chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            content_buffer += content

            # Process the buffer to detect and handle thinking tags
            while True:
                if not in_thinking:
                    # Look for opening thinking tag
                    think_start = re.search(r'<think(?:ing)?>', content_buffer, re.IGNORECASE)
                    if think_start:
                        # Send any content before the thinking tag
                        before_thinking = content_buffer[:think_start.start()]
                        if before_thinking:
                            yield f"data: {json.dumps({'delta': before_thinking})}\n\n"

                        # Start capturing thinking content
                        in_thinking = True
                        content_buffer = content_buffer[think_start.end():]
                        thinking_buffer = ""
                    else:
                        # No thinking tag found, send accumulated content
                        if content_buffer:
                            yield f"data: {json.dumps({'delta': content_buffer})}\n\n"
                        content_buffer = ""
                        break
                else:
                    # We're inside a thinking tag, look for closing tag
                    think_end = re.search(r'</think(?:ing)?>', content_buffer, re.IGNORECASE)
                    if think_end:
                        # Capture thinking content up to the closing tag
                        thinking_buffer += content_buffer[:think_end.start()]

                        # Send the thinking content as a special type
                        if thinking_buffer.strip():
                            yield f"data: {json.dumps({'thinking': thinking_buffer.strip()})}\n\n"

                        # Continue processing after the closing tag
                        in_thinking = False
                        content_buffer = content_buffer[think_end.end():]
                        thinking_buffer = ""
                    else:
                        # Still inside thinking tag, accumulate content
                        thinking_buffer += content_buffer
                        content_buffer = ""
                        break

    # Handle any remaining content
    if in_thinking and thinking_buffer:
        # Unclosed thinking tag - send as thinking content
        yield f"data: {json.dumps({'thinking': thinking_buffer.strip()})}\n\n"
    elif content_buffer:
        # Regular content
        yield f"data: {json.dumps({'delta': content_buffer})}\n\n"

    # Signal the end of the stream
    yield f"data: {json.dumps({'end_of_stream': True})}\n\n"



