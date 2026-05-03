"""
Tests for the prompt-template variable utility.

These cover the public surface: extract, substitute, infer_label, and the
sanitise step that gates user input before it lands in the database.
"""

import pytest

from src.utils.prompt_variables import (
    extract_template_variables,
    substitute_template_variables,
    infer_label,
    sanitize_variable_values,
    MAX_VARIABLES,
    MAX_VALUE_CHARS,
    MAX_TOTAL_CHARS,
)


# ---------------------------------------------------------------------------
# extract_template_variables
# ---------------------------------------------------------------------------

def test_extract_returns_unique_in_first_seen_order():
    assert extract_template_variables('Hi {{name}}, {{agenda}} and again {{name}}') == ['name', 'agenda']


def test_extract_tolerates_whitespace_inside_braces():
    assert extract_template_variables('{{ name }} and {{  agenda  }}') == ['name', 'agenda']


def test_extract_ignores_text_without_placeholders():
    assert extract_template_variables('plain text without variables') == []


def test_extract_handles_empty_input():
    assert extract_template_variables('') == []
    assert extract_template_variables(None) == []
    assert extract_template_variables(123) == []


def test_extract_does_not_match_single_braces():
    assert extract_template_variables('this {is not} a placeholder') == []


def test_extract_rejects_invalid_identifier_starts():
    # Numeric-leading identifiers must not match
    assert extract_template_variables('hi {{1bad}}') == []


# ---------------------------------------------------------------------------
# substitute_template_variables
# ---------------------------------------------------------------------------

def test_substitute_replaces_known_keys():
    assert substitute_template_variables('Hi {{name}}', {'name': 'Anna'}) == 'Hi Anna'


def test_substitute_uses_empty_string_for_missing_keys():
    assert substitute_template_variables('Hi {{name}}', {}) == 'Hi '


def test_substitute_coerces_non_string_values():
    assert substitute_template_variables('count {{n}}', {'n': 42}) == 'count 42'


def test_substitute_passes_html_unchanged_to_caller():
    # The substitution layer is text-only; XSS sanitisation is a downstream
    # concern of the summary renderer, not the variable substituter.
    raw = substitute_template_variables('hi {{x}}', {'x': '<script>alert(1)</script>'})
    assert raw == 'hi <script>alert(1)</script>'


def test_substitute_no_template_engine_invocation():
    # Even a value that looks like Python format-string mischief is treated as
    # plain text on the value side.
    out = substitute_template_variables('A {{x}} B', {'x': '{0.__class__.__bases__}'})
    assert out == 'A {0.__class__.__bases__} B'


def test_substitute_handles_empty_input():
    assert substitute_template_variables('', {'x': 'y'}) == ''
    assert substitute_template_variables(None, {'x': 'y'}) is None


def test_substitute_with_non_dict_values_treated_as_empty():
    assert substitute_template_variables('Hi {{name}}', None) == 'Hi '
    assert substitute_template_variables('Hi {{name}}', 'not-a-dict') == 'Hi '


# ---------------------------------------------------------------------------
# infer_label
# ---------------------------------------------------------------------------

def test_infer_label_basic_cases():
    assert infer_label('agenda') == 'Agenda'
    assert infer_label('meeting_date') == 'Meeting date'
    assert infer_label('whoIsHere') == 'WhoIsHere'


def test_infer_label_handles_empty():
    assert infer_label('') == ''
    assert infer_label(None) == ''


# ---------------------------------------------------------------------------
# sanitize_variable_values
# ---------------------------------------------------------------------------

def test_sanitize_strips_invalid_keys():
    cleaned = sanitize_variable_values({
        'good': 'value',
        '1bad': 'starts with digit',
        'has space': 'no spaces',
        'with-dash': 'no dashes',
    })
    assert cleaned == {'good': 'value'}


def test_sanitize_drops_empty_and_whitespace_values():
    assert sanitize_variable_values({'a': '', 'b': '   ', 'c': 'real'}) == {'c': 'real'}


def test_sanitize_returns_none_for_empty_result():
    assert sanitize_variable_values({}) is None
    assert sanitize_variable_values({'a': ''}) is None


def test_sanitize_returns_none_for_non_dict_input():
    assert sanitize_variable_values(['a', 'b']) is None
    assert sanitize_variable_values('hello') is None
    assert sanitize_variable_values(None) is None


def test_sanitize_strips_control_characters():
    cleaned = sanitize_variable_values({'k': 'hello\x00\x01\x02world'})
    assert cleaned == {'k': 'helloworld'}


def test_sanitize_preserves_common_whitespace():
    cleaned = sanitize_variable_values({'k': 'line1\nline2\ttabbed'})
    assert cleaned == {'k': 'line1\nline2\ttabbed'}


def test_sanitize_caps_variable_count():
    raw = {f'k{i}': 'v' for i in range(MAX_VARIABLES + 20)}
    cleaned = sanitize_variable_values(raw)
    assert len(cleaned) == MAX_VARIABLES


def test_sanitize_caps_value_length():
    cleaned = sanitize_variable_values({'k': 'A' * (MAX_VALUE_CHARS + 1000)})
    assert len(cleaned['k']) == MAX_VALUE_CHARS


def test_sanitize_caps_total_size():
    big_value = 'A' * 4000
    raw = {f'k{i}': big_value for i in range(20)}
    cleaned = sanitize_variable_values(raw)
    total = sum(len(v) for v in cleaned.values())
    assert total <= MAX_TOTAL_CHARS


def test_sanitize_coerces_non_string_values():
    cleaned = sanitize_variable_values({'count': 42, 'flag': True})
    assert cleaned == {'count': '42', 'flag': 'True'}


def test_sanitize_dunder_keys_are_safe_lookups():
    # __class__ is allowed by the identifier regex but the substitution path
    # is a plain dict lookup with no code execution. Verify the value is
    # returned verbatim.
    cleaned = sanitize_variable_values({'__class__': 'value'})
    assert cleaned == {'__class__': 'value'}
    out = substitute_template_variables('a {{__class__}} b', cleaned)
    assert out == 'a value b'
