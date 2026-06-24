"""
Coverage tests for src/services/llm.py — the LLM client wrapper.

Covers:
- Module config helpers: is_gpt5_model, is_using_openai_api, get_chat_config
- call_llm_completion / call_chat_completion: success, param passthrough,
  GPT-5 branch, error/timeout handling, budget enforcement, token tracking,
  empty-content logging, streaming path
- format_api_error_message: all branches
- process_streaming_with_thinking: plain content, thinking tags, unclosed
  tags, usage recording

Strategy: the OpenAI client lives at module level (src.services.llm.client).
We patch that object plus the relevant module-level config constants so no
network is touched and behaviour is deterministic.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.services.llm as llm
from src.services.llm import (
    is_gpt5_model,
    is_using_openai_api,
    get_chat_config,
    call_llm_completion,
    call_chat_completion,
    format_api_error_message,
    process_streaming_with_thinking,
    TokenBudgetExceeded,
)


# ---------------------------------------------------------------------------
# Helpers to build canned completion / chunk objects
# ---------------------------------------------------------------------------

def make_usage(prompt=10, completion=5, total=15, cost=None):
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )
    if cost is not None:
        usage.cost = cost
    return usage


def make_message(content="hello", **extra):
    msg = SimpleNamespace(content=content)
    for k, v in extra.items():
        setattr(msg, k, v)
    return msg


def make_choice(content="hello", finish_reason="stop", **msg_extra):
    return SimpleNamespace(
        message=make_message(content, **msg_extra),
        finish_reason=finish_reason,
    )


def make_completion(content="hello", usage=None, finish_reason="stop", **msg_extra):
    return SimpleNamespace(
        choices=[make_choice(content, finish_reason, **msg_extra)],
        usage=usage,
    )


def make_chunk(content=None, usage=None):
    """A streaming chunk with one choice delta."""
    delta = SimpleNamespace(content=content)
    choice = SimpleNamespace(delta=delta)
    chunk = SimpleNamespace(choices=[choice], usage=usage)
    return chunk


class FakeClient:
    """Stand-in for the OpenAI client exposing chat.completions.create."""

    def __init__(self, return_value=None, side_effect=None):
        self.create = MagicMock(return_value=return_value, side_effect=side_effect)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )


def install_client(monkeypatch, **kwargs):
    fake = FakeClient(**kwargs)
    monkeypatch.setattr(llm, "client", fake)
    return fake


# ===========================================================================
# is_gpt5_model
# ===========================================================================

def test_is_gpt5_model_true_prefix():
    assert is_gpt5_model("gpt-5-turbo") is True


def test_is_gpt5_model_true_exact():
    assert is_gpt5_model("gpt-5-mini") is True


def test_is_gpt5_model_case_insensitive():
    assert is_gpt5_model("GPT-5") is True


def test_is_gpt5_model_false():
    assert is_gpt5_model("gpt-4o") is False


def test_is_gpt5_model_none():
    assert is_gpt5_model(None) is False


def test_is_gpt5_model_empty():
    assert is_gpt5_model("") is False


# ===========================================================================
# is_using_openai_api
# ===========================================================================

def test_is_using_openai_api_true(monkeypatch):
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://api.openai.com/v1")
    assert is_using_openai_api() is True


def test_is_using_openai_api_false(monkeypatch):
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    assert is_using_openai_api() is False


# ===========================================================================
# get_chat_config
# ===========================================================================

def test_get_chat_config_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "text-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://text.example/v1")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "text-model")
    cfg = get_chat_config()
    assert cfg["api_key"] == "text-key"
    assert cfg["base_url"] == "https://text.example/v1"
    assert cfg["model_name"] == "text-model"
    assert cfg["gpt5_reasoning_effort"] in ("medium", "low", "high", "minimal")


def test_get_chat_config_uses_dedicated_chat(monkeypatch):
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", "chat-key")
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", "chat-model")
    monkeypatch.setattr(llm, "CHAT_MODEL_BASE_URL", "https://chat.example/v1")
    monkeypatch.setattr(llm, "CHAT_GPT5_REASONING_EFFORT", "high")
    monkeypatch.setattr(llm, "CHAT_GPT5_VERBOSITY", "low")
    cfg = get_chat_config()
    assert cfg["api_key"] == "chat-key"
    assert cfg["model_name"] == "chat-model"
    assert cfg["base_url"] == "https://chat.example/v1"
    assert cfg["gpt5_reasoning_effort"] == "high"
    assert cfg["gpt5_verbosity"] == "low"


def test_get_chat_config_base_url_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", "chat-key")
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", "chat-model")
    monkeypatch.setattr(llm, "CHAT_MODEL_BASE_URL", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://text.example/v1")
    cfg = get_chat_config()
    assert cfg["base_url"] == "https://text.example/v1"


# ===========================================================================
# call_llm_completion — guard clauses
# ===========================================================================

def test_call_llm_completion_no_client(monkeypatch):
    monkeypatch.setattr(llm, "client", None)
    with pytest.raises(ValueError, match="not initialized"):
        call_llm_completion([{"role": "user", "content": "hi"}])


def test_call_llm_completion_no_api_key(monkeypatch):
    install_client(monkeypatch)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", None)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        call_llm_completion([{"role": "user", "content": "hi"}])


# ===========================================================================
# call_llm_completion — success / passthrough
# ===========================================================================

def test_call_llm_completion_success_returns_content(monkeypatch):
    fake = install_client(monkeypatch, return_value=make_completion("the answer"))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    resp = call_llm_completion(
        [{"role": "user", "content": "q"}],
        temperature=0.3,
        max_tokens=128,
    )
    assert resp.choices[0].message.content == "the answer"
    kwargs = fake.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_tokens"] == 128
    assert kwargs["stream"] is False


def test_call_llm_completion_response_format_passthrough(monkeypatch):
    fake = install_client(monkeypatch, return_value=make_completion("{}"))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    call_llm_completion(
        [{"role": "user", "content": "q"}],
        response_format={"type": "json_object"},
    )
    assert fake.create.call_args.kwargs["response_format"] == {"type": "json_object"}


def test_call_llm_completion_gpt5_branch(monkeypatch):
    fake = install_client(monkeypatch, return_value=make_completion("ok"))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-5")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("GPT5_REASONING_EFFORT", "high")
    monkeypatch.setenv("GPT5_VERBOSITY", "low")
    call_llm_completion([{"role": "user", "content": "q"}], max_tokens=200)
    kwargs = fake.create.call_args.kwargs
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["verbosity"] == "low"
    assert kwargs["max_completion_tokens"] == 200
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs


def test_call_llm_completion_streaming_adds_stream_options(monkeypatch):
    fake = install_client(monkeypatch, return_value=iter([]))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "ENABLE_STREAM_OPTIONS", True)
    call_llm_completion([{"role": "user", "content": "q"}], stream=True)
    kwargs = fake.create.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}


def test_call_llm_completion_streaming_no_stream_options(monkeypatch):
    fake = install_client(monkeypatch, return_value=iter([]))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "ENABLE_STREAM_OPTIONS", False)
    call_llm_completion([{"role": "user", "content": "q"}], stream=True)
    assert "stream_options" not in fake.create.call_args.kwargs


def test_call_llm_completion_empty_content_logs(monkeypatch):
    completion = make_completion(content="", refusal="nope", tool_calls=None)
    install_client(monkeypatch, return_value=completion)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    # Should not raise; just logs warnings about empty content.
    resp = call_llm_completion([{"role": "user", "content": "q"}])
    assert resp.choices[0].message.content == ""


# ===========================================================================
# call_llm_completion — error / timeout handling
# ===========================================================================

def test_call_llm_completion_generic_error_reraised(monkeypatch):
    install_client(monkeypatch, side_effect=RuntimeError("boom"))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    with pytest.raises(RuntimeError, match="boom"):
        call_llm_completion([{"role": "user", "content": "q"}])


def test_call_llm_completion_timeout_reraised(monkeypatch):
    from openai import APITimeoutError
    err = APITimeoutError(request=MagicMock())
    install_client(monkeypatch, side_effect=err)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    with pytest.raises(APITimeoutError):
        call_llm_completion([{"role": "user", "content": "q"}])


# ===========================================================================
# call_llm_completion — budget + token tracking
# ===========================================================================

def test_call_llm_completion_budget_exceeded(monkeypatch):
    install_client(monkeypatch, return_value=make_completion("ok"))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (False, 120.0, "over budget")
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        with pytest.raises(TokenBudgetExceeded):
            call_llm_completion(
                [{"role": "user", "content": "q"}],
                user_id=99001,
                operation_type="summarization",
            )


def test_call_llm_completion_records_usage(monkeypatch):
    usage = make_usage(prompt=11, completion=7, total=18, cost=0.001)
    install_client(monkeypatch, return_value=make_completion("ok", usage=usage))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (True, 50.0, None)
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        call_llm_completion(
            [{"role": "user", "content": "q"}],
            user_id=99002,
            operation_type="summarization",
        )
    fake_tracker.record_usage.assert_called_once()
    rec_kwargs = fake_tracker.record_usage.call_args.kwargs
    assert rec_kwargs["user_id"] == 99002
    assert rec_kwargs["prompt_tokens"] == 11
    assert rec_kwargs["completion_tokens"] == 7
    assert rec_kwargs["total_tokens"] == 18
    assert rec_kwargs["cost"] == 0.001


def test_call_llm_completion_budget_warning_near_limit(monkeypatch):
    install_client(monkeypatch, return_value=make_completion("ok", usage=make_usage()))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (True, 85.0, None)
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        resp = call_llm_completion(
            [{"role": "user", "content": "q"}],
            user_id=99003,
            operation_type="chat",
        )
    assert resp.choices[0].message.content == "ok"


def test_call_llm_completion_budget_check_error_non_blocking(monkeypatch):
    install_client(monkeypatch, return_value=make_completion("ok"))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.side_effect = RuntimeError("db down")
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        # Budget check failure must not block the call.
        resp = call_llm_completion(
            [{"role": "user", "content": "q"}],
            user_id=99004,
            operation_type="chat",
        )
    assert resp.choices[0].message.content == "ok"


def test_call_llm_completion_record_usage_error_non_blocking(monkeypatch):
    usage = make_usage()
    install_client(monkeypatch, return_value=make_completion("ok", usage=usage))
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (True, 10.0, None)
    fake_tracker.record_usage.side_effect = RuntimeError("write failed")
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        resp = call_llm_completion(
            [{"role": "user", "content": "q"}],
            user_id=99005,
            operation_type="chat",
        )
    assert resp.choices[0].message.content == "ok"


# ===========================================================================
# call_chat_completion
# ===========================================================================

def test_call_chat_completion_no_client(monkeypatch):
    monkeypatch.setattr(llm, "chat_client", None)
    monkeypatch.setattr(llm, "client", None)
    with pytest.raises(ValueError, match="not initialized"):
        call_chat_completion([{"role": "user", "content": "hi"}])


def test_call_chat_completion_no_api_key(monkeypatch):
    fake = FakeClient(return_value=make_completion("ok"))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", None)
    with pytest.raises(ValueError, match="API key not configured"):
        call_chat_completion([{"role": "user", "content": "hi"}])


def test_call_chat_completion_success(monkeypatch):
    fake = FakeClient(return_value=make_completion("chat answer"))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    resp = call_chat_completion(
        [{"role": "user", "content": "q"}],
        temperature=0.2,
        max_tokens=64,
    )
    assert resp.choices[0].message.content == "chat answer"
    kwargs = fake.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 64


def test_call_chat_completion_gpt5_branch(monkeypatch):
    fake = FakeClient(return_value=make_completion("ok"))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", "chat-key")
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", "gpt-5-mini")
    monkeypatch.setattr(llm, "CHAT_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(llm, "CHAT_GPT5_REASONING_EFFORT", "low")
    monkeypatch.setattr(llm, "CHAT_GPT5_VERBOSITY", "high")
    call_chat_completion([{"role": "user", "content": "q"}], max_tokens=50)
    kwargs = fake.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5-mini"
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["verbosity"] == "high"
    assert kwargs["max_completion_tokens"] == 50
    assert "temperature" not in kwargs


def test_call_chat_completion_falls_back_to_main_client(monkeypatch):
    fake = FakeClient(return_value=make_completion("ok"))
    # chat_client None -> uses client
    monkeypatch.setattr(llm, "chat_client", None)
    monkeypatch.setattr(llm, "client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    resp = call_chat_completion([{"role": "user", "content": "q"}])
    assert resp.choices[0].message.content == "ok"


def test_call_chat_completion_records_usage(monkeypatch):
    usage = make_usage(prompt=3, completion=4, total=7)
    fake = FakeClient(return_value=make_completion("ok", usage=usage))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (True, 10.0, None)
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        call_chat_completion(
            [{"role": "user", "content": "q"}],
            user_id=99010,
            operation_type="chat",
        )
    fake_tracker.record_usage.assert_called_once()


def test_call_chat_completion_budget_exceeded(monkeypatch):
    fake = FakeClient(return_value=make_completion("ok"))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (False, 120.0, "over")
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        with pytest.raises(TokenBudgetExceeded):
            call_chat_completion(
                [{"role": "user", "content": "q"}],
                user_id=99011,
                operation_type="chat",
            )


def test_call_chat_completion_timeout_reraised(monkeypatch):
    from openai import APITimeoutError
    err = APITimeoutError(request=MagicMock())
    fake = FakeClient(side_effect=err)
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    with pytest.raises(APITimeoutError):
        call_chat_completion([{"role": "user", "content": "q"}])


def test_call_chat_completion_generic_error_reraised(monkeypatch):
    fake = FakeClient(side_effect=RuntimeError("kaboom"))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    with pytest.raises(RuntimeError, match="kaboom"):
        call_chat_completion([{"role": "user", "content": "q"}])


def test_call_chat_completion_response_format_and_stream(monkeypatch):
    fake = FakeClient(return_value=iter([]))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm, "ENABLE_STREAM_OPTIONS", True)
    call_chat_completion(
        [{"role": "user", "content": "q"}],
        response_format={"type": "json_object"},
        stream=True,
    )
    kwargs = fake.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["stream_options"] == {"include_usage": True}


def test_call_chat_completion_empty_content_logs(monkeypatch):
    fake = FakeClient(return_value=make_completion(content=""))
    monkeypatch.setattr(llm, "chat_client", fake)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    resp = call_chat_completion([{"role": "user", "content": "q"}])
    assert resp.choices[0].message.content == ""


# ===========================================================================
# format_api_error_message
# ===========================================================================

def test_format_error_context_length():
    msg = format_api_error_message("This model's maximum context length is 4096 tokens")
    assert "too long" in msg


def test_format_error_rate_limit():
    assert "rate limit" in format_api_error_message("429 Rate limit reached").lower()


def test_format_error_insufficient_funds():
    assert "quota exceeded" in format_api_error_message("insufficient funds on account").lower()


def test_format_error_quota_exceeded():
    assert "quota exceeded" in format_api_error_message("Your quota exceeded").lower()


def test_format_error_timeout():
    assert "timed out" in format_api_error_message("Request timeout occurred").lower()


def test_format_error_generic():
    out = format_api_error_message("some weird error")
    assert "some weird error" in out
    assert out.startswith("[Summary generation failed")


# ===========================================================================
# process_streaming_with_thinking
# ===========================================================================

def _collect(gen):
    return [json.loads(line[len("data: "):].strip()) for line in gen]


def test_streaming_plain_content():
    stream = [make_chunk("Hello "), make_chunk("world")]
    events = _collect(process_streaming_with_thinking(stream))
    deltas = [e["delta"] for e in events if "delta" in e]
    assert "".join(deltas) == "Hello world"
    assert events[-1] == {"end_of_stream": True}


def test_streaming_with_thinking_tags():
    stream = [
        make_chunk("before "),
        make_chunk("<think>secret thought</think>"),
        make_chunk("after"),
    ]
    events = _collect(process_streaming_with_thinking(stream))
    thinking = [e["thinking"] for e in events if "thinking" in e]
    deltas = "".join(e["delta"] for e in events if "delta" in e)
    assert thinking == ["secret thought"]
    assert "before " in deltas
    assert "after" in deltas


def test_streaming_unclosed_thinking_tag():
    stream = [make_chunk("<thinking>still thinking")]
    events = _collect(process_streaming_with_thinking(stream))
    thinking = [e["thinking"] for e in events if "thinking" in e]
    assert thinking == ["still thinking"]
    assert events[-1] == {"end_of_stream": True}


def test_streaming_records_usage(monkeypatch):
    usage = make_usage(prompt=2, completion=3, total=5)
    stream = [make_chunk("hi"), make_chunk(None, usage=usage)]
    fake_tracker = MagicMock()
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "gpt-4o")
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        events = _collect(
            process_streaming_with_thinking(
                stream, user_id=99020, operation_type="chat", model_name="gpt-4o"
            )
        )
    fake_tracker.record_usage.assert_called_once()
    assert events[-1] == {"end_of_stream": True}


def test_streaming_records_usage_with_app_context(monkeypatch):
    usage = make_usage()
    stream = [make_chunk("hi"), make_chunk(None, usage=usage)]
    fake_tracker = MagicMock()

    class FakeAppCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_app = MagicMock()
    fake_app.app_context.return_value = FakeAppCtx()
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        list(
            process_streaming_with_thinking(
                stream, user_id=99021, operation_type="chat", app=fake_app
            )
        )
    fake_app.app_context.assert_called_once()
    fake_tracker.record_usage.assert_called_once()


def test_streaming_usage_record_error_non_blocking(monkeypatch):
    usage = make_usage()
    stream = [make_chunk("hi"), make_chunk(None, usage=usage)]
    fake_tracker = MagicMock()
    fake_tracker.record_usage.side_effect = RuntimeError("fail")
    with patch("src.services.token_tracking.token_tracker", fake_tracker):
        events = _collect(
            process_streaming_with_thinking(
                stream, user_id=99022, operation_type="chat"
            )
        )
    # Generator must still complete despite recording failure.
    assert events[-1] == {"end_of_stream": True}
