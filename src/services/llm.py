"""
LLM API integration services (OpenAI/OpenRouter).
"""

import json
import logging
import os
import re
import sys
import time

import httpx
from openai import APITimeoutError, BadRequestError

# Use standard logging instead of current_app.logger for context independence
logger = logging.getLogger(__name__)


class TokenBudgetExceeded(Exception):
    """Raised when user exceeds their token budget."""
    # The budget is as used up at the second attempt as at the first: the job
    # queue does not retry (src/services/job_queue.py reads this attribute)
    permanent = True
    def __init__(self, message, usage_percentage=100):
        self.message = message
        self.usage_percentage = usage_percentage
        super().__init__(message)

from src.services import llm_config
from src.utils import extract_json_object, safe_json_loads
from src.utils.error_text import log_failure


def extract_cache_tokens(usage):
    """Pull prompt-cache counts out of a provider's usage object.

    Returns (cached_tokens, cache_write_tokens) as ints, defaulting to 0.
    Providers expose these differently:
      - OpenAI / OpenRouter: usage.prompt_tokens_details.cached_tokens (reads),
        and some surface a cache_write/cache_creation field alongside it.
      - Anthropic: usage.cache_read_input_tokens / usage.cache_creation_input_tokens.
    We probe all known shapes defensively because usage may be a pydantic
    object or a plain dict, and missing fields must never break tracking.
    """
    if not usage:
        return 0, 0

    def _get(obj, name):
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _to_int(val):
        try:
            return int(val) if val else 0
        except (TypeError, ValueError):
            return 0

    details = _get(usage, 'prompt_tokens_details')

    cached = (
        _get(details, 'cached_tokens')
        or _get(usage, 'cache_read_input_tokens')
        or 0
    )
    cache_write = (
        _get(usage, 'cache_creation_input_tokens')
        or _get(details, 'cache_write_tokens')
        or _get(usage, 'cache_write_tokens')
        or 0
    )
    return _to_int(cached), _to_int(cache_write)

# Provider-specific request parameters, as a JSON object merged into the body
# of every request to the model (OpenAI SDK extra_body). Reasoning controls
# differ by provider and model (reasoning_effort, OpenRouter's reasoning
# object, chat_template_kwargs on vLLM), so they are set here, not mapped.
def _parse_extra_body(env_var):
    """Return (parameters, error) for a JSON object in env_var."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return {}, None
    try:
        value = json.loads(raw)
    except ValueError as e:
        return {}, f"{env_var} is not valid JSON ({e})"
    if not isinstance(value, dict):
        return {}, f"{env_var} must be a JSON object, such as {{\"reasoning_effort\": \"low\"}}"
    return value, None


def _cfg(name):
    """The current value of a former module constant.

    A test or a caller that sets the attribute (monkeypatch) wins. Otherwise
    the module ``__getattr__`` resolves it from ``llm_config`` at call time, so
    an assigned model and a hot setting apply to the next request without a
    restart.
    """
    return getattr(sys.modules[__name__], name)


# The OpenRouter app identification headers and the HTTP client with them,
# shared with the LLM clients of llm_config (one connection pool).
app_headers = llm_config.APP_HEADERS
http_client_no_proxy = llm_config.HTTP_CLIENT

_ROLE_ATTRIBUTES = {
    'TEXT_MODEL_NAME': ('text', 'model_name'),
    'TEXT_MODEL_BASE_URL': ('text', 'base_url'),
    'TEXT_MODEL_API_KEY': ('text', 'api_key'),
    'TEXT_MODEL_EXTRA_BODY': ('text', 'extra_body'),
    'TEXT_MODEL_EXTRA_BODY_ERROR': ('text', 'extra_body_error'),
    'TEXT_MODEL_GPT5_FORCE': ('text', 'gpt5_force'),
    'GPT5_REASONING_EFFORT': ('text', 'gpt5_reasoning_effort'),
    'GPT5_VERBOSITY': ('text', 'gpt5_verbosity'),
    'CHAT_MODEL_NAME': ('chat', 'model_name'),
    'CHAT_MODEL_BASE_URL': ('chat', 'base_url'),
    'CHAT_MODEL_API_KEY': ('chat', 'api_key'),
    'CHAT_MODEL_EXTRA_BODY': ('chat', 'extra_body'),
    'CHAT_MODEL_EXTRA_BODY_ERROR': ('chat', 'extra_body_error'),
    'CHAT_MODEL_GPT5_FORCE': ('chat', 'gpt5_force'),
    'CHAT_GPT5_REASONING_EFFORT': ('chat', 'gpt5_reasoning_effort'),
    'CHAT_GPT5_VERBOSITY': ('chat', 'gpt5_verbosity'),
    'TRANSLATION_MODEL_NAME': ('translation', 'model_name'),
    'TRANSLATION_MODEL_BASE_URL': ('translation', 'base_url'),
    'TRANSLATION_MODEL_API_KEY': ('translation', 'api_key'),
    'TRANSLATION_MODEL_EXTRA_BODY': ('translation', 'extra_body'),
    'TRANSLATION_MODEL_EXTRA_BODY_ERROR': ('translation', 'extra_body_error'),
    'TRANSLATION_MODEL_GPT5_FORCE': ('translation', 'gpt5_force'),
    'TRANSLATION_GPT5_REASONING_EFFORT': ('translation', 'gpt5_reasoning_effort'),
    'TRANSLATION_GPT5_VERBOSITY': ('translation', 'gpt5_verbosity'),
}
_RUNTIME_ATTRIBUTES = {
    'LLM_REQUEST_TIMEOUT': 'request_timeout',
    'LLM_MAX_RETRIES': 'max_retries',
    'LLM_CONNECT_TIMEOUT': 'connect_timeout',
    'LLM_WRITE_TIMEOUT': 'write_timeout',
    'ENABLE_STREAM_OPTIONS': 'enable_stream_options',
}
_CLIENT_ATTRIBUTES = {'client': 'text', 'chat_client': 'chat', 'translation_client': 'translation'}
# Every name __getattr__ computes. An attribute of that name set on the module
# shadows the live value until it is deleted again: pytest's monkeypatch
# restores such a name by setting the value it read, so the test suite drops
# them after each test (conftest.py).
DYNAMIC_ATTRIBUTES = frozenset(
    set(_ROLE_ATTRIBUTES) | set(_RUNTIME_ATTRIBUTES) | set(_CLIENT_ATTRIBUTES)
    | {'TEXT_MODEL_CONFIGURED', 'TRANSLATION_MODEL_CONFIGURED', 'llm_timeout'})


def __getattr__(name):
    """Resolve the former import-time constants from the live configuration."""
    if name in _CLIENT_ATTRIBUTES:
        return llm_config.get_client(_CLIENT_ATTRIBUTES[name])
    if name == 'TEXT_MODEL_CONFIGURED':
        return llm_config.is_configured('text')
    if name == 'TRANSLATION_MODEL_CONFIGURED':
        return llm_config.is_configured('translation')
    if name == 'llm_timeout':
        settings = llm_config.runtime_settings()
        return httpx.Timeout(
            connect=settings.connect_timeout,
            read=float(settings.request_timeout),
            write=settings.write_timeout,
            pool=30.0,
        )
    if name in _RUNTIME_ATTRIBUTES:
        return getattr(llm_config.runtime_settings(), _RUNTIME_ATTRIBUTES[name])
    if name in _ROLE_ATTRIBUTES:
        role, field = _ROLE_ATTRIBUTES[name]
        return llm_config.resolve_config(role, reveal_key=True).get(field)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _inherits_text_model(role):
    """Whether the role has no configuration of its own and reuses the text model."""
    return bool(llm_config.resolve_config(role).get('uses_text_model'))


def get_chat_config():
    """
    Get chat model configuration, falling back to TEXT_MODEL if not set.

    Returns a dict with api_key, base_url, model_name, and GPT-5 settings.
    A dedicated chat model needs CHAT_MODEL_NAME and either its own key or
    its own base URL; a local server often needs no key at all (#403).
    """
    chat_name = _cfg('CHAT_MODEL_NAME')
    chat_key = _cfg('CHAT_MODEL_API_KEY')
    chat_url = _cfg('CHAT_MODEL_BASE_URL')
    text_key = _cfg('TEXT_MODEL_API_KEY')
    text_url = _cfg('TEXT_MODEL_BASE_URL')

    if chat_name and (chat_key or chat_url):
        return {
            # The text model's key goes only to the text model's server.
            'api_key': chat_key or (text_key if (chat_url or text_url) == text_url else None),
            'base_url': chat_url or text_url,
            'model_name': chat_name,
            'gpt5_reasoning_effort': (
                _cfg('CHAT_GPT5_REASONING_EFFORT') or _cfg('GPT5_REASONING_EFFORT')),
            'gpt5_verbosity': _cfg('CHAT_GPT5_VERBOSITY') or _cfg('GPT5_VERBOSITY'),
            # A separate chat model does not inherit GPT5_FORCE_MODE: it may
            # be a different (non-GPT-5) model than the main text model.
            'gpt5_force': bool(_cfg('CHAT_MODEL_GPT5_FORCE')),
            # Like GPT5_FORCE_MODE, the text model's parameters are not
            # inherited: they may not suit a different model or server.
            'extra_body': _cfg('CHAT_MODEL_EXTRA_BODY'),
            'extra_body_variable': (
                'TEXT_MODEL_EXTRA_BODY' if _inherits_text_model('chat')
                else 'CHAT_MODEL_EXTRA_BODY'),
            'extra_body_error': _cfg('CHAT_MODEL_EXTRA_BODY_ERROR'),
        }
    return {
        'api_key': text_key,
        'base_url': text_url,
        'model_name': _cfg('TEXT_MODEL_NAME'),
        'gpt5_reasoning_effort': _cfg('GPT5_REASONING_EFFORT'),
        'gpt5_verbosity': _cfg('GPT5_VERBOSITY'),
        'gpt5_force': bool(_cfg('TEXT_MODEL_GPT5_FORCE')),
        'extra_body': _cfg('TEXT_MODEL_EXTRA_BODY'),
        'extra_body_variable': 'TEXT_MODEL_EXTRA_BODY',
        'extra_body_error': _cfg('TEXT_MODEL_EXTRA_BODY_ERROR'),
    }


def get_translation_config():
    """
    Get translation model configuration, falling back to TEXT_MODEL if not set.

    Mirrors get_chat_config().
    """
    name = _cfg('TRANSLATION_MODEL_NAME')
    key = _cfg('TRANSLATION_MODEL_API_KEY')
    url = _cfg('TRANSLATION_MODEL_BASE_URL')
    text_key = _cfg('TEXT_MODEL_API_KEY')
    text_url = _cfg('TEXT_MODEL_BASE_URL')

    if name and (key or url):
        return {
            'api_key': key or (text_key if (url or text_url) == text_url else None),
            'base_url': url or text_url,
            'model_name': name,
            'gpt5_reasoning_effort': (
                _cfg('TRANSLATION_GPT5_REASONING_EFFORT') or _cfg('GPT5_REASONING_EFFORT')),
            'gpt5_verbosity': (
                _cfg('TRANSLATION_GPT5_VERBOSITY') or _cfg('GPT5_VERBOSITY')),
            'gpt5_force': bool(_cfg('TRANSLATION_MODEL_GPT5_FORCE')),
            'extra_body': _cfg('TRANSLATION_MODEL_EXTRA_BODY'),
            'extra_body_variable': (
                'TEXT_MODEL_EXTRA_BODY' if _inherits_text_model('translation')
                else 'TRANSLATION_MODEL_EXTRA_BODY'),
            'extra_body_error': _cfg('TRANSLATION_MODEL_EXTRA_BODY_ERROR'),
        }
    return {
        'api_key': text_key,
        'base_url': text_url,
        'model_name': _cfg('TEXT_MODEL_NAME'),
        'gpt5_reasoning_effort': _cfg('GPT5_REASONING_EFFORT'),
        'gpt5_verbosity': _cfg('GPT5_VERBOSITY'),
        'gpt5_force': bool(_cfg('TEXT_MODEL_GPT5_FORCE')),
        'extra_body': _cfg('TEXT_MODEL_EXTRA_BODY'),
        'extra_body_variable': 'TEXT_MODEL_EXTRA_BODY',
        'extra_body_error': _cfg('TEXT_MODEL_EXTRA_BODY_ERROR'),
    }


def _add_extra_body(completion_args, extra_body, error):
    """Merge the configured request parameters into completion_args.

    They are applied last, so they take precedence over the parameters set
    above (temperature, max_tokens, the GPT-5 settings)."""
    if error:
        raise ValueError(error)
    if extra_body:
        completion_args["extra_body"] = dict(extra_body)


def _log_rejected_parameters(error, variable, extra_body):
    """Name the request parameters setting when the provider rejects a request."""
    if extra_body and isinstance(error, BadRequestError):
        logger.error(
            f"The model server rejected the request. {variable} adds "
            f"{', '.join(sorted(extra_body))}; check that the server accepts these parameters."
        )


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

    Returns:
        Boolean indicating if this is the OpenAI API
    """
    base_url = _cfg('TEXT_MODEL_BASE_URL')
    return base_url and 'api.openai.com' in base_url



class LLMNotConfiguredError(ValueError):
    """No text model is configured; the API answers with text_model_not_configured."""


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
    effective_client = _cfg('client')
    if not effective_client:
        raise LLMNotConfiguredError("No text model is configured. Set TEXT_MODEL_BASE_URL, and TEXT_MODEL_API_KEY for a hosted service.")

    model_name = _cfg('TEXT_MODEL_NAME')
    base_url = _cfg('TEXT_MODEL_BASE_URL') or ''
    runtime = llm_config.runtime_settings()

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
        # Check if we're using GPT-5 with OpenAI API (or forced for Azure AI
        # Foundry and other gateways the URL check can't detect).
        using_gpt5 = (
            bool(_cfg('TEXT_MODEL_GPT5_FORCE'))
            or (is_gpt5_model(model_name) and 'api.openai.com' in base_url)
        )

        completion_args = {
            "model": model_name,
            "messages": messages,
            "stream": stream
        }

        # Add stream_options to get usage in final chunk for streaming
        # Some LLM servers don't support this OpenAI-specific option
        if stream and _cfg('ENABLE_STREAM_OPTIONS'):
            completion_args["stream_options"] = {"include_usage": True}

        if using_gpt5:
            # GPT-5 models don't support temperature, top_p, or logprobs
            # They use reasoning_effort and verbosity instead
            logger.debug(f"Using GPT-5 model: {model_name} - applying GPT-5 specific parameters")

            completion_args["reasoning_effort"] = _cfg('GPT5_REASONING_EFFORT')
            completion_args["verbosity"] = _cfg('GPT5_VERBOSITY')

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

        _add_extra_body(completion_args, _cfg('TEXT_MODEL_EXTRA_BODY'),
                        _cfg('TEXT_MODEL_EXTRA_BODY_ERROR'))

        # Make the resolved max_tokens visible in logs so users can confirm
        # their SUMMARY_MAX_TOKENS / CHAT_MAX_TOKENS / etc. settings actually
        # took effect for a given operation. The "key" used here is
        # max_completion_tokens for GPT-5 and max_tokens otherwise.
        budget_key = 'max_completion_tokens' if using_gpt5 else 'max_tokens'
        logger.info(
            f"LLM call: operation={operation_type or 'unspecified'}, model={model_name}, "
            f"{budget_key}={completion_args.get(budget_key, 'provider default')}"
        )

        request_started_at = time.monotonic()
        response = effective_client.chat.completions.create(**completion_args)

        # Track usage for non-streaming calls
        if user_id and operation_type and not stream and response.usage:
            try:
                from src.services.token_tracking import token_tracker
                cached_tokens, cache_write_tokens = extract_cache_tokens(response.usage)
                token_tracker.record_usage(
                    user_id=user_id,
                    operation_type=operation_type,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    model_name=model_name,
                    cost=getattr(response.usage, 'cost', None),
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens
                )
            except Exception as e:
                logger.warning(f"Failed to record token usage: {e}")

        # Debug log for empty responses
        if not stream and response.choices:
            content = response.choices[0].message.content
            if not content:
                logger.warning(f"LLM returned empty content. Model: {model_name}, finish_reason: {response.choices[0].finish_reason}")
                # Log more details if available
                # The refusal text and the tool-call arguments are model output
                # about user content: DEBUG only (discussion #603).
                if getattr(response.choices[0].message, 'refusal', None):
                    logger.warning(f"Refusal present ({len(str(response.choices[0].message.refusal))} characters)")
                    logger.debug(f"Refusal: {response.choices[0].message.refusal}")
                if hasattr(response.choices[0].message, 'tool_calls') and response.choices[0].message.tool_calls:
                    logger.warning(f"Tool calls present: {len(response.choices[0].message.tool_calls)}")
                    logger.debug(f"Tool calls: {response.choices[0].message.tool_calls}")

        return response

    except TokenBudgetExceeded:
        raise
    except APITimeoutError as e:
        elapsed = time.monotonic() - request_started_at if 'request_started_at' in locals() else None
        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "unknown"
        logger.error(
            f"LLM request timed out after {elapsed_str} (configured read_timeout={runtime.request_timeout}s, "
            f"max_retries={runtime.max_retries}, model={model_name}). "
            f"If the elapsed time is much shorter than the configured timeout, an upstream proxy or the "
            f"provider is closing the connection early. For reasoning models that take longer to think, "
            f"increase LLM_REQUEST_TIMEOUT."
        )
        raise
    except Exception as e:
        # The provider's error text can quote the prompt: metadata at ERROR,
        # the text at DEBUG (src/utils/error_text.py).
        log_failure(logger, logging.ERROR, f"LLM API call failed (operation={operation_type or 'unspecified'})", e)
        _log_rejected_parameters(e, 'TEXT_MODEL_EXTRA_BODY', _cfg('TEXT_MODEL_EXTRA_BODY'))
        raise


def call_chat_completion(messages, temperature=0.7, response_format=None, stream=False, max_tokens=None,
                         user_id=None, operation_type=None, tools=None, tool_choice=None):
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
        tools: Optional list of tool schemas (OpenAI function-calling format).
            Only sent when provided, so non-agent call sites are unaffected.
        tool_choice: Optional tool_choice value, passed through when tools are set.

    Returns:
        OpenAI completion object or generator (if streaming)
    """
    effective_client = _cfg('chat_client') or _cfg('client')
    chat_config = get_chat_config()
    runtime = llm_config.runtime_settings()

    if not effective_client:
        raise ValueError("Chat LLM client not initialized")

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

        # Check if we're using GPT-5 with OpenAI API (or forced via env for
        # Azure AI Foundry and other gateways the URL check can't detect)
        using_gpt5 = chat_config['gpt5_force'] or (is_gpt5_model(model_name) and 'api.openai.com' in base_url)

        completion_args = {
            "model": model_name,
            "messages": messages,
            "stream": stream
        }

        # Add stream_options to get usage in final chunk for streaming
        # Some LLM servers don't support this OpenAI-specific option
        if stream and _cfg('ENABLE_STREAM_OPTIONS'):
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

        if tools:
            completion_args["tools"] = tools
            if tool_choice:
                completion_args["tool_choice"] = tool_choice

        _add_extra_body(completion_args, chat_config['extra_body'], chat_config['extra_body_error'])

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
                cached_tokens, cache_write_tokens = extract_cache_tokens(response.usage)
                token_tracker.record_usage(
                    user_id=user_id,
                    operation_type=operation_type,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    model_name=model_name,
                    cost=getattr(response.usage, 'cost', None),
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens
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
            f"Chat LLM request timed out after {elapsed_str} (configured read_timeout={runtime.request_timeout}s, "
            f"max_retries={runtime.max_retries}, model={model_name}). "
            f"If the elapsed time is much shorter than the configured timeout, an upstream proxy or the "
            f"provider is closing the connection early. For reasoning models that take longer to think, "
            f"increase LLM_REQUEST_TIMEOUT."
        )
        raise
    except Exception as e:
        log_failure(logger, logging.ERROR, f"Chat LLM API call failed (operation={operation_type or 'unspecified'})", e)
        _log_rejected_parameters(e, chat_config['extra_body_variable'], chat_config['extra_body'])
        raise


def call_translation_completion(messages, temperature=0.7, response_format=None, stream=False, max_tokens=None,
                            user_id=None, operation_type=None):
    """
    Translation-specific LLM completion function. Uses dedicated translation
    model if configured, otherwise falls back to standard TEXT_MODEL
    configuration.

    Args:
        messages: List of message dicts with 'role' and 'content'
        temperature: Sampling temperature (0-1) - ignored for GPT-5 models
        response_format: Optional response format dict (e.g., {"type": "json_object"})
        stream: Whether to stream the response
        max_tokens: Optional maximum tokens to generate
        user_id: Optional user ID for token tracking and budget enforcement
        operation_type: Optional operation type for token tracking (e.g., 'translation')

    Returns:
        OpenAI completion object or generator (if streaming)
    """
    effective_client = _cfg('translation_client') or _cfg('client')
    translation_config = get_translation_config()
    runtime = llm_config.runtime_settings()

    if not effective_client:
        raise ValueError("Translation LLM client not initialized")

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
        model_name = translation_config['model_name']
        base_url = translation_config['base_url'] or ''

        # Check if we're using GPT-5 with OpenAI API (or forced via env for
        # Azure AI Foundry and other gateways the URL check can't detect)
        using_gpt5 = translation_config['gpt5_force'] or (is_gpt5_model(model_name) and 'api.openai.com' in base_url)

        completion_args = {
            "model": model_name,
            "messages": messages,
            "stream": stream
        }

        # Add stream_options to get usage in final chunk for streaming
        # Some LLM servers don't support this OpenAI-specific option
        if stream and _cfg('ENABLE_STREAM_OPTIONS'):
            completion_args["stream_options"] = {"include_usage": True}

        if using_gpt5:
            logger.debug(f"Using GPT-5 translation model: {model_name}")
            completion_args["reasoning_effort"] = translation_config['gpt5_reasoning_effort']
            completion_args["verbosity"] = translation_config['gpt5_verbosity']

            if max_tokens:
                completion_args["max_completion_tokens"] = max_tokens
        else:
            completion_args["temperature"] = temperature
            if max_tokens:
                completion_args["max_tokens"] = max_tokens

        if response_format:
            completion_args["response_format"] = response_format

        _add_extra_body(completion_args, translation_config['extra_body'], translation_config['extra_body_error'])

        budget_key = 'max_completion_tokens' if using_gpt5 else 'max_tokens'
        logger.info(
            f"Translation LLM call: operation={operation_type or 'unspecified'}, model={model_name}, "
            f"{budget_key}={completion_args.get(budget_key, 'provider default')}"
        )

        request_started_at = time.monotonic()
        response = effective_client.chat.completions.create(**completion_args)

        # Track usage for non-streaming calls
        if user_id and operation_type and not stream and response.usage:
            try:
                from src.services.token_tracking import token_tracker
                cached_tokens, cache_write_tokens = extract_cache_tokens(response.usage)
                token_tracker.record_usage(
                    user_id=user_id,
                    operation_type=operation_type,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    model_name=model_name,
                    cost=getattr(response.usage, 'cost', None),
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens
                )
            except Exception as e:
                logger.warning(f"Failed to record token usage: {e}")

        # Debug log for empty responses
        if not stream and response.choices:
            content = response.choices[0].message.content
            if not content:
                logger.warning(f"Translation LLM returned empty content. Model: {model_name}, finish_reason: {response.choices[0].finish_reason}")

        return response

    except TokenBudgetExceeded:
        raise
    except APITimeoutError as e:
        elapsed = time.monotonic() - request_started_at if 'request_started_at' in locals() else None
        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "unknown"
        logger.error(
            f"Translation LLM request timed out after {elapsed_str} (configured read_timeout={runtime.request_timeout}s, "
            f"max_retries={runtime.max_retries}, model={model_name}). "
            f"If the elapsed time is much shorter than the configured timeout, an upstream proxy or the "
            f"provider is closing the connection early. For reasoning models that take longer to think, "
            f"increase LLM_REQUEST_TIMEOUT."
        )
        raise
    except Exception as e:
        log_failure(logger, logging.ERROR, f"Translation LLM API call failed (operation={operation_type or 'unspecified'})", e)
        _log_rejected_parameters(e, translation_config['extra_body_variable'], translation_config['extra_body'])
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
    finish_reason = None

    for chunk in stream:
        # Capture the provider's final finish_reason so truncation
        # (finish_reason == 'length') survives to the terminal SSE event
        # instead of being silently dropped (issue #349).
        if chunk.choices:
            chunk_finish = getattr(chunk.choices[0], 'finish_reason', None)
            if chunk_finish:
                finish_reason = chunk_finish

        # Check for usage in final chunk (from stream_options={'include_usage': True})
        if hasattr(chunk, 'usage') and chunk.usage and user_id and operation_type:
            try:
                from src.services.token_tracking import token_tracker
                cached_tokens, cache_write_tokens = extract_cache_tokens(chunk.usage)
                # Use app context if provided (needed for generators where context may be lost)
                if app:
                    with app.app_context():
                        token_tracker.record_usage(
                            user_id=user_id,
                            operation_type=operation_type,
                            prompt_tokens=chunk.usage.prompt_tokens,
                            completion_tokens=chunk.usage.completion_tokens,
                            total_tokens=chunk.usage.total_tokens,
                            model_name=model_name or _cfg('TEXT_MODEL_NAME'),
                            cost=getattr(chunk.usage, 'cost', None),
                            cached_tokens=cached_tokens,
                            cache_write_tokens=cache_write_tokens
                        )
                else:
                    token_tracker.record_usage(
                        user_id=user_id,
                        operation_type=operation_type,
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.total_tokens,
                        model_name=model_name or _cfg('TEXT_MODEL_NAME'),
                        cost=getattr(chunk.usage, 'cost', None),
                        cached_tokens=cached_tokens,
                        cache_write_tokens=cache_write_tokens
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

    # Signal the end of the stream, propagating the provider's finish_reason
    # so the frontend can flag truncated (finish_reason == 'length') responses.
    end_event = {'end_of_stream': True}
    if finish_reason:
        end_event['finish_reason'] = finish_reason
        if finish_reason == 'length':
            end_event['truncated'] = True
    yield f"data: {json.dumps(end_event)}\n\n"



