"""
LLM API integration services (OpenAI/OpenRouter).
"""

import os
import re
import json
import logging
import httpx
from openai import OpenAI

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


def get_chat_config():
    """
    Get chat model configuration, falling back to TEXT_MODEL if not set.

    Returns a dict with api_key, base_url, model_name, and GPT-5 settings.
    """
    if CHAT_MODEL_API_KEY and CHAT_MODEL_NAME:
        return {
            'api_key': CHAT_MODEL_API_KEY,
            'base_url': CHAT_MODEL_BASE_URL or TEXT_MODEL_BASE_URL,
            'model_name': CHAT_MODEL_NAME,
            'gpt5_reasoning_effort': CHAT_GPT5_REASONING_EFFORT or os.environ.get("GPT5_REASONING_EFFORT", "medium"),
            'gpt5_verbosity': CHAT_GPT5_VERBOSITY or os.environ.get("GPT5_VERBOSITY", "medium")
        }
    return {
        'api_key': TEXT_MODEL_API_KEY,
        'base_url': TEXT_MODEL_BASE_URL,
        'model_name': TEXT_MODEL_NAME,
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
        http_client=http_client_no_proxy
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
                http_client=http_client_no_proxy
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

    Returns:
        Boolean indicating if this is the OpenAI API
    """
    return TEXT_MODEL_BASE_URL and 'api.openai.com' in TEXT_MODEL_BASE_URL



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
        # Check if we're using GPT-5 with OpenAI API
        using_gpt5 = is_gpt5_model(TEXT_MODEL_NAME) and is_using_openai_api()

        completion_args = {
            "model": TEXT_MODEL_NAME,
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
            logger.debug(f"Using GPT-5 model: {TEXT_MODEL_NAME} - applying GPT-5 specific parameters")

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
                    model_name=TEXT_MODEL_NAME,
                    cost=getattr(response.usage, 'cost', None)
                )
            except Exception as e:
                logger.warning(f"Failed to record token usage: {e}")

        # Debug log for empty responses
        if not stream and response.choices:
            content = response.choices[0].message.content
            if not content:
                logger.warning(f"LLM returned empty content. Model: {TEXT_MODEL_NAME}, finish_reason: {response.choices[0].finish_reason}")
                # Log more details if available
                if hasattr(response.choices[0].message, 'refusal'):
                    logger.warning(f"Refusal: {response.choices[0].message.refusal}")
                if hasattr(response.choices[0].message, 'tool_calls') and response.choices[0].message.tool_calls:
                    logger.warning(f"Tool calls present: {response.choices[0].message.tool_calls}")

        return response

    except TokenBudgetExceeded:
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
                # Use app context if provided (needed for generators where context may be lost)
                if app:
                    with app.app_context():
                        token_tracker.record_usage(
                            user_id=user_id,
                            operation_type=operation_type,
                            prompt_tokens=chunk.usage.prompt_tokens,
                            completion_tokens=chunk.usage.completion_tokens,
                            total_tokens=chunk.usage.total_tokens,
                            model_name=model_name or TEXT_MODEL_NAME,
                            cost=getattr(chunk.usage, 'cost', None)
                        )
                else:
                    token_tracker.record_usage(
                        user_id=user_id,
                        operation_type=operation_type,
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.total_tokens,
                        model_name=model_name or TEXT_MODEL_NAME,
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



