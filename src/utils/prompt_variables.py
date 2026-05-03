"""
Templated prompt variables.

Authors of custom summarisation prompts (on tags, folders, users, or admin
defaults) can embed `{{variable_name}}` placeholders. At upload time the user
fills in values for whichever variables appear in the resolved prompt; the
values are stored on the recording and substituted into the prompt at
summarisation time.

Variable naming follows simple identifier rules so the regex never
accidentally matches natural prose: must start with a letter or underscore,
followed by letters, digits, or underscores. Whitespace inside the braces is
allowed (`{{ name }}`).
"""

import re

# Match `{{ name }}` with optional whitespace, capture the bare identifier.
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def extract_template_variables(text):
    """
    Return a list of unique variable names that appear in `text`, in the
    order of their first occurrence. An empty or non-string input returns [].
    """
    if not text or not isinstance(text, str):
        return []
    seen = []
    for match in _VARIABLE_PATTERN.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def substitute_template_variables(text, values):
    """
    Replace every `{{name}}` placeholder in `text` with `values.get(name, '')`.
    Whitespace inside the braces is tolerated. Returns the substituted string.
    A missing or non-string `text` is returned unchanged. A non-dict `values`
    is treated as empty.
    """
    if not text or not isinstance(text, str):
        return text
    if not isinstance(values, dict):
        values = {}

    def _replace(match):
        name = match.group(1)
        replacement = values.get(name, '')
        if replacement is None:
            return ''
        return str(replacement)

    return _VARIABLE_PATTERN.sub(_replace, text)


def infer_label(variable_name):
    """
    Derive a human-readable label from a variable name. `meeting_date` becomes
    "Meeting date"; `agenda` becomes "Agenda". Used by the upload form so the
    user does not have to declare labels separately.
    """
    if not variable_name:
        return ''
    cleaned = variable_name.replace('_', ' ').strip()
    if not cleaned:
        return ''
    return cleaned[0].upper() + cleaned[1:]


# Hard limits enforced on user-supplied variable maps so a malicious or buggy
# client cannot blow up storage or downstream LLM calls.
MAX_VARIABLES = 50
MAX_VALUE_CHARS = 8000
MAX_TOTAL_CHARS = 32000

# Keys must match the same identifier shape as the regex above so we never
# store names that the substitution pass cannot match anyway.
_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_variable_values(raw):
    """
    Coerce a user-supplied variable map into a safe canonical form.

    - Returns a plain ``{str: str}`` dict, or ``None`` if the input is empty
      or unusable.
    - Drops keys that are not valid identifiers (the substitution regex would
      not match them anyway).
    - Drops empty / whitespace-only values so the column stays ``None`` when
      nothing was supplied.
    - Caps the number of entries, each value's length, and the total size to
      prevent abuse of an unbounded JSON column.
    - Strips control characters except ``\\n``, ``\\r``, and ``\\t``.

    The function is deliberately defensive: bad input becomes empty rather
    than raising, so callers can use it as the only validation step.
    """
    if not isinstance(raw, dict):
        return None

    cleaned = {}
    total = 0
    for key, value in raw.items():
        if len(cleaned) >= MAX_VARIABLES:
            break
        if not isinstance(key, str) or not _KEY_PATTERN.match(key):
            continue
        if value is None:
            continue
        text = str(value)
        # Strip C0 control characters except common whitespace; this stops
        # null bytes and other oddities from landing in the prompt.
        text = ''.join(
            ch for ch in text
            if ch in ('\n', '\r', '\t') or ord(ch) >= 0x20
        )
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_VALUE_CHARS:
            text = text[:MAX_VALUE_CHARS]
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        cleaned[key] = text
        total += len(text)

    return cleaned or None
