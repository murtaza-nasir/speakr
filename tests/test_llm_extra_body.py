"""Provider-specific request parameters (TEXT_MODEL_EXTRA_BODY and
CHAT_MODEL_EXTRA_BODY), merged into every request as extra_body."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import openai
import pytest

import src.services.llm as llm


def _completion(content="ok"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                    finish_reason="stop")], usage=None)


def _client(monkeypatch, *names, **kwargs):
    create = MagicMock(return_value=_completion(), **kwargs)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    for name in names:
        monkeypatch.setattr(llm, name, fake)
    return create


@pytest.fixture
def text_model(monkeypatch):
    monkeypatch.setattr(llm, "TEXT_MODEL_NAME", "qwen3")
    monkeypatch.setattr(llm, "TEXT_MODEL_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setattr(llm, "TEXT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_API_KEY", None)
    monkeypatch.setattr(llm, "CHAT_MODEL_BASE_URL", None)
    monkeypatch.delenv("GPT5_FORCE_MODE", raising=False)


@pytest.mark.parametrize("raw, expected", [
    ("", ({}, None)),
    ('{"chat_template_kwargs": {"enable_thinking": false}}',
     ({"chat_template_kwargs": {"enable_thinking": False}}, None)),
    ('{"reasoning": {"effort": "low"}}', ({"reasoning": {"effort": "low"}}, None)),
])
def test_parse_valid_settings(monkeypatch, raw, expected):
    monkeypatch.setenv("TEXT_MODEL_EXTRA_BODY", raw)
    assert llm._parse_extra_body("TEXT_MODEL_EXTRA_BODY") == expected


@pytest.mark.parametrize("raw, message", [
    ("{reasoning: low}", "is not valid JSON"),
    ('["low"]', "must be a JSON object"),
    ('"low"', "must be a JSON object"),
])
def test_parse_invalid_settings(monkeypatch, raw, message):
    monkeypatch.setenv("TEXT_MODEL_EXTRA_BODY", raw)
    value, error = llm._parse_extra_body("TEXT_MODEL_EXTRA_BODY")
    assert value == {} and message in error and "TEXT_MODEL_EXTRA_BODY" in error


def test_no_setting_sends_no_extra_body(monkeypatch, text_model):
    create = _client(monkeypatch, "client")
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY", {})
    llm.call_llm_completion([{"role": "user", "content": "q"}])
    assert "extra_body" not in create.call_args.kwargs


def test_text_model_parameters_are_sent(monkeypatch, text_model):
    create = _client(monkeypatch, "client")
    body = {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY", body)
    llm.call_llm_completion([{"role": "user", "content": "q"}], max_tokens=100)
    kwargs = create.call_args.kwargs
    assert kwargs["extra_body"] == body
    assert kwargs["extra_body"] is not body  # a copy per request
    assert kwargs["max_tokens"] == 100


def test_parameters_are_added_to_gpt5_requests(monkeypatch, text_model):
    create = _client(monkeypatch, "client")
    monkeypatch.setenv("GPT5_FORCE_MODE", "true")
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY", {"reasoning_effort": "minimal"})
    llm.call_llm_completion([{"role": "user", "content": "q"}])
    kwargs = create.call_args.kwargs
    # The SDK merges extra_body last, so the configured value is the one sent.
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["extra_body"] == {"reasoning_effort": "minimal"}


def test_invalid_setting_fails_the_request(monkeypatch, text_model):
    create = _client(monkeypatch, "client")
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY_ERROR", "TEXT_MODEL_EXTRA_BODY is not valid JSON (x)")
    with pytest.raises(ValueError, match="TEXT_MODEL_EXTRA_BODY is not valid JSON"):
        llm.call_llm_completion([{"role": "user", "content": "q"}])
    create.assert_not_called()


def test_chat_on_the_text_model_uses_its_parameters(monkeypatch, text_model):
    create = _client(monkeypatch, "client", "chat_client")
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY", {"reasoning": {"enabled": False}})
    monkeypatch.setattr(llm, "CHAT_MODEL_EXTRA_BODY", {"ignored": True})
    llm.call_chat_completion([{"role": "user", "content": "q"}])
    assert create.call_args.kwargs["extra_body"] == {"reasoning": {"enabled": False}}


def test_a_separate_chat_model_does_not_inherit_them(monkeypatch, text_model):
    create = _client(monkeypatch, "client", "chat_client")
    monkeypatch.setattr(llm, "CHAT_MODEL_NAME", "openai/gpt-4o")
    monkeypatch.setattr(llm, "CHAT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY", {"chat_template_kwargs": {"enable_thinking": False}})
    monkeypatch.setattr(llm, "CHAT_MODEL_EXTRA_BODY", {})
    llm.call_chat_completion([{"role": "user", "content": "q"}])
    assert "extra_body" not in create.call_args.kwargs

    monkeypatch.setattr(llm, "CHAT_MODEL_EXTRA_BODY", {"reasoning": {"effort": "low"}})
    llm.call_chat_completion([{"role": "user", "content": "q"}])
    assert create.call_args.kwargs["extra_body"] == {"reasoning": {"effort": "low"}}


def test_a_rejected_request_names_the_setting(monkeypatch, text_model, caplog):
    response = httpx.Response(400, request=httpx.Request("POST", "http://vllm:8000/v1/chat/completions"))
    error = openai.BadRequestError("unknown field", response=response, body=None)
    _client(monkeypatch, "client", side_effect=error)
    monkeypatch.setattr(llm, "TEXT_MODEL_EXTRA_BODY", {"reasoning": {"effort": "low"}})
    with pytest.raises(openai.BadRequestError):
        llm.call_llm_completion([{"role": "user", "content": "q"}])
    assert "TEXT_MODEL_EXTRA_BODY adds reasoning" in caplog.text


def test_incognito_requests_use_the_shared_helpers():
    """Incognito title, summary and chat go through call_llm_completion and
    call_chat_completion, so they get the GPT-5 handling and these settings."""
    import ast, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in ("src/tasks/processing.py", "src/api/recordings.py"):
        tree = ast.parse(open(os.path.join(root, path), encoding="utf-8").read())
        direct = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "create"
                  and isinstance(n.value, ast.Attribute) and n.value.attr == "completions"]
        assert direct == [], f"{path} calls the client directly at lines {direct}"
