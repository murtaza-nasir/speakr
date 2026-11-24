"""HTML built in script goes through safeHtml, which escapes every value (#692).

The account page built its cards by assigning template literals to innerHTML
and escaping each value by hand; a value that was missed (an identifier, the
text of a date that did not parse) was parsed as markup. Markup is now built
with safeHtml (static/js/safe-html.js), a tagged template that escapes every
interpolated value and inserts only its own results as markup. These checks
cover every template and script of the app:

- no plain template literal builds markup from values;
- innerHTML, outerHTML and insertAdjacentHTML take a safeHtml result or a
  constant string (a short list of reviewed exceptions below);
- no value goes into an inline event handler;
- the account page and its webhooks script have no escape helpers of their own.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = re.compile(r'</?[a-zA-Z][a-zA-Z0-9-]*[\s>/]')
SINK = re.compile(r'\.(?:innerHTML|outerHTML)\s*\+?=(?!=)|\.insertAdjacentHTML\s*\(\s*[\'"][a-zA-Z]+[\'"]\s*,')
PLAIN_STRING = re.compile(r"""^(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`$\\]*`)$""", re.S)

# Sinks that take a value which is not built from data, each reviewed
REVIEWED_SINKS = {
    ('templates/account.html', 'original'): 'the button markup read from innerHTML before a busy state',
    ('static/js/modules/assistant/chat.js', 'original'): 'the button markup read from innerHTML before a copy',
    ('static/js/modules/composables/chat.js', 'originalContent'): 'the button markup read from innerHTML before a copy',
    ('static/js/global-header.js', 'serverRendered'): 'the header markup read from innerHTML before the Vue mount',
    ('static/js/modules/assistant/render.js', 'html'): 'DOMPurify output (renderMarkdownSafe) into a <template>',
}


class Literal:
    def __init__(self, start):
        self.start, self.end, self.parts, self.exprs = start, None, [], []


def _parse(src, start, stop):
    """Top-level template literals of src[start:stop], nested ones in .exprs.
    Skips strings, comments and regular expression literals."""
    n = stop

    def regex_start(i):
        j = i - 1
        while j >= start and src[j] in ' \t\r\n':
            j -= 1
        if j < start or src[j] in '(,=:[!&|?{};+-*%<>~^':
            return True
        return bool(re.search(r'\b(?:return|typeof|case|in|of)$', src[max(0, j - 6):j + 1]))

    def skip_regex(i):
        i += 1
        in_class = False
        while i < n and src[i] != '\n':
            c = src[i]
            if c == '\\':
                i += 2
                continue
            if c == '[':
                in_class = True
            elif c == ']':
                in_class = False
            elif c == '/' and not in_class:
                return i + 1
            i += 1
        return i

    def skip_string(i, quote):
        i += 1
        while i < n and src[i] != quote:
            i += 2 if src[i] == '\\' else 1
        return i + 1

    def code(i, until_brace, out):
        depth = 0
        while i < n:
            c = src[i]
            if src.startswith('//', i):
                j = src.find('\n', i)
                i = n if j < 0 else j
            elif src.startswith('/*', i):
                j = src.find('*/', i)
                i = n if j < 0 else j + 2
            elif c in '\'"':
                i = skip_string(i, c)
            elif c == '`':
                literal = Literal(i)
                i = template(i, literal)
                out.append(literal)
            elif c == '/' and regex_start(i):
                i = skip_regex(i)
            else:
                if until_brace:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        if depth == 0:
                            return i
                        depth -= 1
                i += 1
        return i

    def template(i, literal):
        i += 1
        buf = []
        while i < n:
            c = src[i]
            if c == '\\':
                buf.append(src[i:i + 2])
                i += 2
            elif c == '`':
                literal.parts.append(''.join(buf))
                literal.end = i + 1
                return i + 1
            elif src.startswith('${', i):
                literal.parts.append(''.join(buf))
                buf = []
                children = []
                end = code(i + 2, True, children)
                literal.exprs.append((i + 2, end, children))
                i = end + 1
            else:
                buf.append(c)
                i += 1
        literal.end = n
        return n

    out = []
    code(start, False, out)
    return out


def _walk(literals):
    for literal in literals:
        yield literal
        for _, _, children in literal.exprs:
            yield from _walk(children)


def _scripts():
    """(path, source, [(start, stop)]) for every template and app script."""
    for folder in ('templates', os.path.join('static', 'js')):
        for directory, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in sorted(files):
                if name.endswith('.test.js') or not name.endswith(('.html', '.js')):
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding='utf-8') as f:
                    src = f.read()
                if name.endswith('.js'):
                    ranges = [(0, len(src))]
                else:
                    ranges = [(m.start(1), m.end(1)) for m in
                              re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', src, re.S)]
                yield os.path.relpath(path, ROOT).replace(os.sep, '/'), src, ranges


def _tag(src, literal):
    m = re.search(r'([A-Za-z_$][\w$.]*)$', src[max(0, literal.start - 40):literal.start])
    return m.group(1) if m else None


def _line(src, index):
    return src.count('\n', 0, index) + 1


def _statement(src, i):
    """The expression from i to the end of its statement or call argument."""
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c in '\'"':
            j += 1
            while src[j] != c:
                j += 2 if src[j] == '\\' else 1
        elif c == '`':
            literal = _parse(src, j, len(src))[0]
            j = literal.end - 1
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif c == ';' and depth == 0:
            break
        j += 1
    return src[i:j].strip()


def test_safe_html_helper_is_loaded_where_it_is_used():
    for page in ('account.html', 'index.html'):
        with open(os.path.join(ROOT, 'templates', page), encoding='utf-8') as f:
            assert "filename='js/safe-html.js'" in f.read(), page


def test_no_plain_template_literal_builds_markup():
    offenders = []
    for path, src, ranges in _scripts():
        for start, stop in ranges:
            for literal in _walk(_parse(src, start, stop)):
                if literal.exprs and any(TAG.search(p) for p in literal.parts) and _tag(src, literal) != 'safeHtml':
                    offenders.append(f'{path}:{_line(src, literal.start)}')
    assert not offenders, f'markup built from a plain template literal (use safeHtml): {offenders}'


def test_html_sinks_take_safe_html_or_a_constant():
    offenders = []
    for path, src, ranges in _scripts():
        for start, stop in ranges:
            for m in SINK.finditer(src, start, stop):
                value = _statement(src, m.end())
                if value.startswith('safeHtml`') or PLAIN_STRING.match(value) or (path, value) in REVIEWED_SINKS:
                    continue
                offenders.append(f'{path}:{_line(src, m.start())} {value[:80]!r}')
    assert not offenders, f'HTML sinks given markup not built by safeHtml: {offenders}'


def test_reviewed_sinks_still_exist():
    """An exception that no longer matches a sink is removed from the list."""
    found = set()
    for path, src, ranges in _scripts():
        for start, stop in ranges:
            for m in SINK.finditer(src, start, stop):
                found.add((path, _statement(src, m.end())))
    assert set(REVIEWED_SINKS) <= found, set(REVIEWED_SINKS) - found


def test_no_value_in_an_inline_event_handler():
    """Escaping for HTML does not make a value safe inside JavaScript: an
    &#39; in an onclick attribute is a quote again when the handler runs."""
    offenders = []
    for path, src, ranges in _scripts():
        for start, stop in ranges:
            for literal in _walk(_parse(src, start, stop)):
                text = src[literal.start:literal.end]
                if re.search(r'\son[a-z]+\s*=\s*["\'][^"\']*\$\{', text):
                    offenders.append(f'{path}:{_line(src, literal.start)}')
    assert not offenders, offenders


def test_account_scripts_have_no_escape_helpers_of_their_own():
    for rel in ('templates/account.html', 'static/js/account-webhooks.js'):
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            src = f.read()
        assert 'function escapeHtml' not in src, rel
        assert 'escapeHtml(' not in src, rel


def _account():
    with open(os.path.join(ROOT, 'templates', 'account.html'), encoding='utf-8') as f:
        return f.read()


def test_formatinstant_and_identifiers_are_inside_safe_html():
    """The values named in #692 are interpolated only inside safeHtml literals."""
    src = _account()
    start, stop = [(m.start(1), m.end(1)) for m in
                   re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', src, re.S)][1]
    inside = []
    for literal in _walk(_parse(src, start, stop)):
        if _tag(src, literal) == 'safeHtml':
            inside.append(src[literal.start:literal.end])
    joined = '\n'.join(inside)
    for value in ('${formatInstant(item.created_at)}', '${formatInstant(share.created_at)}',
                  '${speaker.id}', '${share.id}', '${entry.recording_id}', '${item.recording_id}',
                  '${share.recording_id}', '${share.public_id}', '${p.share_id}'):
        assert value in joined, value


def test_text_values_are_not_escaped_twice():
    """Positive control: the names and titles are interpolated bare inside
    safeHtml, not run through a second escape."""
    src = _account()
    for value in ('${speaker.name}', '${share.recording_title}', '${item.recording_title}', '${token.name}'):
        assert value in src, value
