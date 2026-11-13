"""A short, safe summary of an error body returned by an outside service (#593).

When a transcription service answers with an HTTP error, its body is kept
so that a real message (an invalid model size, a worker's last traceback
line) reaches the recording's technical details. That text also goes into
the log, the failure webhook and the failure email. A gateway in front of
the service may answer with an HTML page instead, and such pages can carry
tokens, configuration paths and other deployment details.

summarize_error_body() is what a connector puts in its exception and log
in place of the raw body: HTML is reduced to its text, anything that looks
like a credential is replaced with <redacted>, directories in absolute
paths are replaced with <path>, and the result is bounded in length.
"""

import html
import re

from src.utils.error_formatting import truncate_middle

REDACTED = '<redacted>'

_HTML_MARK = re.compile(r'<\s*(?:!doctype|html|head|body|title|meta|div|p|h[1-6]|center|pre|br)\b', re.I)
_HTML_DROP = re.compile(r'<\s*(script|style)\b.*?<\s*/\s*\1\s*>|<!--.*?-->', re.I | re.S)
_HTML_TAG = re.compile(r'<[^>]*>')

# name=value, name: value and "name": "value" where the name says it holds a credential
_SECRET_NAME = (r'[\w-]*(?:token(?![s\w])|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|access[_-]?key|'
                r'private[_-]?key|authorization|cookie|session[_-]?id|credentials?|signature)[\w-]*')
_SECRET_PAIR = re.compile(r'(?i)(["\']?\b' + _SECRET_NAME + r'["\']?\s*[:=]\s*)(["\']?)([^\s"\'&,;<>]+)')
_AUTH_SCHEME = re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}')
_URL_CREDENTIALS = re.compile(r'(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@', re.I)
_KNOWN_KEYS = re.compile(
    r'\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*'   # JWT
    r'|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}'                           # sk-... style API keys
    r'|\bgh[pousr]_[A-Za-z0-9]{20,}|\bhf_[A-Za-z0-9]{20,}'          # GitHub, Hugging Face
    r'|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b'        # Slack, AWS
)
# A long run of letters and digits (both present) reads as a key or a session id
_OPAQUE = re.compile(r'(?<![\w/+=-])[A-Za-z0-9+/_-]{32,}={0,2}(?![\w/+=-])')
# The directories of an absolute path; the file name is kept (a traceback names it)
_ABS_PATH = re.compile(r'(?<![\w:/.~<>-])/(?:[\w.@+-]+/)+(?=[\w.@+-])')


def looks_like_html(text):
    stripped = (text or '').lstrip()
    return stripped.startswith('<') or bool(_HTML_MARK.search(stripped[:2000]))


def html_to_text(text):
    """The visible text of an HTML page, on one line."""
    text = _HTML_DROP.sub(' ', text)
    text = _HTML_TAG.sub(' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _opaque(match):
    value = match.group(0)
    if re.search(r'[A-Za-z]', value) and re.search(r'\d', value):
        return REDACTED
    return value


def redact_secrets(text):
    """Replace credential-like values and absolute directories in text."""
    if not text:
        return text
    text = _URL_CREDENTIALS.sub(r'\1' + REDACTED + '@', text)
    text = _AUTH_SCHEME.sub(r'\1 ' + REDACTED, text)
    text = _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _KNOWN_KEYS.sub(REDACTED, text)
    text = _OPAQUE.sub(_opaque, text)
    text = _ABS_PATH.sub('<path>/', text)
    return text


def summarize_error_body(body, limit=800, html_limit=200):
    """A bounded, redacted excerpt of an error body, safe to log and store.

    An HTML page becomes "HTML error page: <its text>", cut to html_limit;
    any other body keeps its lines and is cut to limit, keeping both ends
    (a traceback names the exception on its last line).
    """
    if body is None:
        return ''
    if isinstance(body, bytes):
        body = body.decode('utf-8', errors='replace')
    text = str(body).strip()
    if not text:
        return ''
    if looks_like_html(text):
        page_text = truncate_middle(redact_secrets(html_to_text(text)), html_limit)
        return f'HTML error page: {page_text}' if page_text else 'HTML error page'
    return truncate_middle(redact_secrets(text), limit)
