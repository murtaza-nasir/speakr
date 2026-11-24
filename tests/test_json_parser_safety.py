"""safe_json_loads accepts JSON only (#532), and auto_close_json repairs
truncated model output (#642).

Model output is untrusted. A Python literal (single quotes, True/None, a tuple
or a set) is not JSON, and is not evaluated: the fallback value is returned.
"""
import pytest

from src.utils.json_parser import auto_close_json, safe_json_loads

_SENTINEL = object()


@pytest.mark.parametrize("text", [
    "{'title': 'Simple', 'summary': 'Test'}",
    "{'flag': True, 'value': None}",
    "{'a': (1, 2)}",
    "{'s': {1, 2}}",
    "[(1, 2)]",
    "(1, 2, 3)",
])
def test_a_python_literal_is_not_parsed(text):
    assert safe_json_loads(text, fallback_value=_SENTINEL) is _SENTINEL


@pytest.mark.parametrize("text,expected", [
    ('{"title": "Complete", "summary": "ok"}', {"title": "Complete", "summary": "ok"}),
    ('[1, 2, 3]', [1, 2, 3]),
    ('```json\n{"title": "Test"}\n```', {"title": "Test"}),
    ('Before {"title": "Extracted"} after', {"title": "Extracted"}),
])
def test_json_is_still_parsed(text, expected):
    assert safe_json_loads(text, fallback_value=_SENTINEL) == expected


@pytest.mark.parametrize("text", ["not json at all", ""])
def test_text_that_is_not_json_returns_the_fallback(text):
    assert safe_json_loads(text, fallback_value=_SENTINEL) is _SENTINEL


@pytest.mark.parametrize("text,expected", [
    ('{"title": "Test", "summary": "This is a test', '{"title": "Test", "summary": "This is a test"}'),
    ('[{"item": 1}, {"item": 2}', '[{"item": 1}, {"item": 2}]'),
    ('{"data": {"items": [1, 2', '{"data": {"items": [1, 2]}}'),
    ('{"key": "value', '{"key": "value"}'),
    ('{"a": {"b": {"c": [1, 2, {"d": "e', '{"a": {"b": {"c": [1, 2, {"d": "e"}]}}}'),
    ('', ''),
    # Complete JSON is returned unchanged
    ('{"title": "Test", "summary": "This is a test"}', '{"title": "Test", "summary": "This is a test"}'),
    ('{"a": 1}', '{"a": 1}'),
])
def test_auto_close_json_repairs_truncated_payloads(text, expected):
    assert auto_close_json(text) == expected


@pytest.mark.parametrize("text,expected", [
    ('{"title": "Test", "summary": "This is a test', {"title": "Test", "summary": "This is a test"}),
    ('```json\n{"title": "Test", "summary": "This is a test\n```', {"title": "Test", "summary": "This is a test"}),
    ('{"title": "Escaped", "summary": "This is a \\"test\\" with quotes."}',
     {"title": "Escaped", "summary": 'This is a "test" with quotes.'}),
])
def test_truncated_or_escaped_json_is_parsed(text, expected):
    assert safe_json_loads(text, fallback_value=_SENTINEL) == expected
