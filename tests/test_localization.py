"""Every localized string a user can see resolves in every language.

A missing key is not an exception: `i18n.t()` returns the key itself, so the
interface renders the literal string `status.pending` where a word should be.
Nothing fails, no test breaks, and it survives until somebody notices in a
screenshot. That is exactly how `status.pending` shipped: it is the default
status of every new recording, referenced in app.modular.js, and present in
none of the seven locale files.

So this checks the two properties that make that impossible rather than
checking any particular key:

  1. every key the interface REFERENCES exists in en.json
  2. every locale carries exactly the same keys as en.json

Both run without a browser, which is the point. The existing browser checks
only cover pages somebody thought to open, in the language they happened to be
using.
"""

import json
import os
import re

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALE_DIR = os.path.join(REPO, 'static', 'locales')

# data-i18n="a.b", plus the -placeholder / -title / -aria-label variants.
TEMPLATE_PATTERN = re.compile(r'data-i18n(?:-[a-z-]+)?="([^"]+)"')
# t('a.b') / $t("a.b") / i18n.t('a.b'), and the guarded page helpers tf(),
# tr() and sharedT(). Requires a dot so bare t('x') helpers and unrelated
# single-argument calls are not mistaken for keys. A prefix completed at run
# time, t('colorScheme.themes.' + name), is not a key.
JS_PATTERN = re.compile(r"""(?:\$t|\b(?:t|tf|tr|sharedT)|i18n\.t)\(\s*['"]([a-zA-Z][\w.]*\.[\w.]+)['"](?!\s*\+)""")


def _flatten(d, prefix=''):
    keys = set()
    for k, v in d.items():
        key = f'{prefix}{k}'
        if isinstance(v, dict):
            keys |= _flatten(v, key + '.')
        else:
            keys.add(key)
    return keys


def _load(lang):
    with open(os.path.join(LOCALE_DIR, f'{lang}.json'), encoding='utf-8') as f:
        return json.load(f)


def _languages():
    return sorted(f[:-5] for f in os.listdir(LOCALE_DIR) if f.endswith('.json'))


def _walk(root, suffix, skip=()):
    for base, dirs, files in os.walk(os.path.join(REPO, root)):
        dirs[:] = [d for d in dirs if d not in ('node_modules', '__pycache__', 'vendor')]
        for name in files:
            if name.endswith(suffix) and not any(s in name for s in skip):
                yield os.path.join(base, name)


def _referenced_keys():
    """Every i18n key the templates and frontend actually ask for."""
    found = {}
    for path in _walk('templates', '.html'):
        text = open(path, encoding='utf-8').read()
        # Vue templates and inline scripts call t() as the JS does.
        for key in TEMPLATE_PATTERN.findall(text) + JS_PATTERN.findall(text):
            found.setdefault(key, set()).add(os.path.relpath(path, REPO))
    for path in _walk('static/js', '.js', skip=('.test.js',)):
        text = open(path, encoding='utf-8').read()
        for key in JS_PATTERN.findall(text):
            found.setdefault(key, set()).add(os.path.relpath(path, REPO))
    return found


def test_there_are_locale_files_to_check():
    """Guards the tests below from passing vacuously if the glob breaks."""
    langs = _languages()
    assert 'en' in langs
    assert len(langs) >= 7, f'expected the full set of locales, found {langs}'


def test_the_scanner_actually_finds_keys():
    """Same guard for the reference scan: a broken regex would make the
    coverage test below pass while checking nothing."""
    referenced = _referenced_keys()
    assert len(referenced) > 200, (
        f'only {len(referenced)} keys found across templates and JS; the scanner is broken')


def test_every_referenced_key_exists_in_english():
    """A key with no entry renders as the literal key. This is the check that
    `status.pending` failed."""
    english = _flatten(_load('en'))
    referenced = _referenced_keys()

    missing = {k: v for k, v in referenced.items() if k not in english}
    assert not missing, 'referenced but absent from en.json:\n' + '\n'.join(
        f'  {k}  <- {", ".join(sorted(paths))}' for k, paths in sorted(missing.items()))


@pytest.mark.parametrize('lang', [l for l in _languages() if l != 'en'])
def test_each_locale_covers_every_english_key(lang):
    """A key present in English and missing elsewhere shows English speakers
    nothing wrong while everyone else sees a raw key."""
    english = _flatten(_load('en'))
    other = _flatten(_load(lang))

    missing = sorted(english - other)
    assert not missing, f'{lang}.json is missing {len(missing)} key(s): {missing[:15]}'


@pytest.mark.parametrize('lang', [l for l in _languages() if l != 'en'])
def test_no_locale_carries_keys_english_does_not(lang):
    """Extra keys are dead weight, and usually mean a key was renamed in
    English and left behind here."""
    english = _flatten(_load('en'))
    other = _flatten(_load(lang))

    extra = sorted(other - english)
    assert not extra, f'{lang}.json has {len(extra)} key(s) not in en.json: {extra[:15]}'


@pytest.mark.parametrize('lang', _languages())
def test_no_value_is_empty(lang):
    """An empty string renders as nothing at all, which is worse than the
    English fallback would have been."""
    def empties(d, prefix=''):
        out = []
        for k, v in d.items():
            key = f'{prefix}{k}'
            if isinstance(v, dict):
                out += empties(v, key + '.')
            elif isinstance(v, str) and not v.strip():
                out.append(key)
        return out

    blank = empties(_load(lang))
    assert not blank, f'{lang}.json has empty values: {blank[:15]}'


# i18n.interpolate() only fills {{name}}. A single-brace {name} is shown to the
# user verbatim, as "(HTTP {status})" was in #388. The one allowed exception
# quotes the literal prompt text the backend appends, where {language} is the
# point.
SINGLE_BRACE = re.compile(r'(?<!\{)\{[A-Za-z_]\w*\}(?!\})')
LITERAL_BRACE_KEYS = {'languagePreferenceNote'}


@pytest.mark.parametrize('lang', _languages())
def test_placeholders_use_the_double_brace_syntax_interpolate_fills(lang):
    def offenders(d, prefix=''):
        out = []
        for k, v in d.items():
            key = f'{prefix}{k}'
            if isinstance(v, dict):
                out += offenders(v, key + '.')
            elif isinstance(v, str) and k not in LITERAL_BRACE_KEYS and SINGLE_BRACE.search(v):
                out.append(f'{key}: {v}')
        return out

    bad = offenders(_load(lang))
    assert not bad, f'{lang}.json uses single-brace placeholders that never get filled: {bad[:15]}'


def test_every_recording_status_the_backend_can_set_has_a_label():
    """The specific gap that motivated this file. PENDING is the column
    default, so every recording passes through it, and it had no label."""
    english = _load('en')['status']
    from src.models import Recording

    column_default = Recording.__table__.columns['status'].default
    assert column_default is not None, 'status lost its default; update this test'
    assert str(column_default.arg).lower() in english, (
        f'the default status {column_default.arg!r} has no label in status.*')

    # The set the backend actually assigns, from the model's own comment.
    for status in ('PENDING', 'PROCESSING', 'SUMMARIZING', 'COMPLETED', 'FAILED'):
        assert status.lower() in english, f'status.{status.lower()} is missing'


# translate('server.a.b') in Python code: text the server produces in the
# user's interface language (#477). The browser scan above does not see it.
SERVER_PATTERN = re.compile(r"""\b(?:translate|LazyTranslation)\(\s*['"](server\.[\w.]+)['"]""")
# api_error('code') reads server.apiError.<code>.
API_ERROR_PATTERN = re.compile(r"""\bapi_error\(\s*['"](\w+)['"]""")
# Error codes that helpers return or raise (ReprocessError('code'),
# webhook_url_problem, can_merge_speakers ...) reach api_error() or
# translate(f'server.apiError.{code}') as values; each still appears in src/
# as a quoted literal.
CODE_LITERAL = re.compile(r"""['"]([a-z][a-z0-9]*(?:_[a-z0-9]+)+)['"]""")


def _server_keys_used():
    found = {}
    for path in _walk('src', '.py'):
        text = open(path, encoding='utf-8').read()
        for key in SERVER_PATTERN.findall(text):
            found.setdefault(key, set()).add(os.path.relpath(path, REPO))
        for code in API_ERROR_PATTERN.findall(text):
            found.setdefault(f'server.apiError.{code}', set()).add(os.path.relpath(path, REPO))
    return found


def _error_code_literals():
    codes = set()
    for path in _walk('src', '.py'):
        codes.update(CODE_LITERAL.findall(open(path, encoding='utf-8').read()))
    return codes


def test_every_server_key_used_in_src_exists_in_english():
    english = _flatten(_load('en'))
    used = _server_keys_used()
    assert len(used) > 50, f'only {len(used)} server keys found in src/; the scanner is broken'

    missing = {k: v for k, v in used.items() if k not in english}
    assert not missing, 'translate() keys absent from en.json:\n' + '\n'.join(
        f'  {k}  <- {", ".join(sorted(paths))}' for k, paths in sorted(missing.items()))


def test_every_server_key_in_english_is_used():
    """server.* keys are only read from Python, so an unused one is dead weight."""
    used = _server_keys_used()
    codes = _error_code_literals()
    unused = sorted(k for k in _flatten(_load('en')) if k.startswith('server.') and k not in used
                    and not (k.startswith('server.apiError.') and k.rsplit('.', 1)[1] in codes))
    assert not unused, f'server keys in en.json that no translate() call uses: {unused}'


# Dates, times and numbers follow the interface language (#480). A
# toLocale*String call or an Intl formatter that passes no locale, undefined,
# [], navigator.language or a hardcoded 'en-US' uses the browser language or
# English whatever the user selected. Numbers count too: the scanner cannot tell
# a Date receiver from a number, and both should follow the interface locale.
# Intl.DateTimeFormat().resolvedOptions() is time zone detection, not
# formatting, and stays allowed.
LOCALE_OPT_OUT = re.compile(
    r"""(?:\.toLocale(?:Date|Time)?String"""
    r"""|\bIntl\.(?:DateTimeFormat|NumberFormat|RelativeTimeFormat|PluralRules|ListFormat|DisplayNames))"""
    r"""\(\s*(?:\)(?!\s*\.resolvedOptions\b)|undefined\b|\[\s*\]|navigator\.languages?\b|(['"])en-US\1)""")


def _locale_opt_outs():
    found = []
    paths = list(_walk('static/js', '.js', skip=('.test.js',))) + list(_walk('templates', '.html'))
    for path in paths:
        text = open(path, encoding='utf-8').read()
        for m in LOCALE_OPT_OUT.finditer(text):
            line = text.count('\n', 0, m.start()) + 1
            found.append(f'{os.path.relpath(path, REPO)}:{line}: {m.group(0)}')
    return found


def test_no_date_or_number_formatting_ignores_the_interface_locale():
    offenders = _locale_opt_outs()
    assert not offenders, (
        'locale-less date/number formatting (pass window.i18n?.getIntlLocale?.(), '
        'or uiLocale() in a Vue template):\n' + '\n'.join(f'  {o}' for o in offenders))


@pytest.mark.parametrize('call', [
    'x.toLocaleDateString(undefined, {})',
    'x.toLocaleTimeString([], {})',
    'x.toLocaleTimeString([ ], {})',
    "x.toLocaleDateString('en-US')",
    'x.toLocaleDateString("en-US", {})',
    'x.toLocaleDateString()',
    'x.toLocaleTimeString()',
    'n.toLocaleString()',
    'x.toLocaleString(\n    undefined, opts)',
    'x.toLocaleString(navigator.language)',
    'new Intl.NumberFormat(undefined, { notation: "compact" })',
    'new Intl.DateTimeFormat().format(d)',
    "new Intl.RelativeTimeFormat('en-US')",
])
def test_locale_scanner_matches_the_opt_outs(call):
    """A broken regex would make the guard above pass while checking nothing."""
    assert LOCALE_OPT_OUT.search(call), call


@pytest.mark.parametrize('call', [
    'x.toLocaleDateString(window.i18n?.getIntlLocale?.(), {})',
    'x.toLocaleString(uiLocale())',
    'x.toLocaleTimeString(locale, {})',
    'x.toLocaleDateString(undefinedLocale)',
    "x.toLocaleDateString('de', {})",
    "x.toLocaleDateString('en-GB')",
    'new Intl.NumberFormat(uiLocale(), {})',
    'Intl.DateTimeFormat().resolvedOptions().timeZone',
    'Intl.DateTimeFormat()\n    .resolvedOptions().timeZone',
    'x.toLocaleUpperCase()',
    'a.localeCompare(b)',
])
def test_locale_scanner_allows_a_resolved_locale(call):
    """The guard targets the opt-out, not every call: an explicit locale, time
    zone detection and unrelated locale methods pass."""
    assert not LOCALE_OPT_OUT.search(call), call


# File sizes have one formatter per side (#481): i18n.formatFileSize() in the
# browser and format_file_size() in src/utils/localization.py, both with the
# fileSize.* units. A local copy with its own unit list ('B', 'KB', ...) is how
# the dot decimal and "0 Bytes" came back on five pages.
LOCAL_SIZE_UNITS = re.compile(r"""\[\s*['"](?:B|Bytes)['"]\s*,\s*['"]KB['"]""")
SIZE_FORMATTER_OWNERS = {'static/js/i18n.js', 'src/utils/localization.py'}


def _local_size_formatters():
    found = []
    paths = (list(_walk('static/js', '.js', skip=('.test.js',))) + list(_walk('templates', '.html'))
             + list(_walk('src', '.py')))
    for path in paths:
        rel = os.path.relpath(path, REPO)
        if rel in SIZE_FORMATTER_OWNERS:
            continue
        text = open(path, encoding='utf-8').read()
        for m in LOCAL_SIZE_UNITS.finditer(text):
            found.append(f'{rel}:{text.count(chr(10), 0, m.start()) + 1}')
    return found


# The API's own byte labels (used_human in /api/v1) stay English on purpose:
# API clients parse them, and they do not render in the interface.
API_SIZE_FORMATTERS = {'src/api/api_v1.py'}


def test_no_local_file_size_formatter():
    offenders = [o for o in _local_size_formatters() if o.split(':')[0] not in API_SIZE_FORMATTERS]
    assert not offenders, (
        'file sizes formatted outside i18n.formatFileSize / format_file_size:\n'
        + '\n'.join(f'  {o}' for o in offenders))


@pytest.mark.parametrize('snippet,matches', [
    ("const sizes = ['B', 'KB', 'MB', 'GB'];", True),
    ('const sizes = ["Bytes", "KB", "MB", "GB", "TB"];', True),
    ("for unit in ['B', 'KB', 'MB', 'GB']:", True),
    ("const units = ['bytes', 'kilobytes', 'megabytes'];", False),
    ("t('fileSize.kilobytes')", False),
])
def test_size_formatter_scanner(snippet, matches):
    """Same guard against a regex that silently matches nothing."""
    assert bool(LOCAL_SIZE_UNITS.search(snippet)) is matches


@pytest.mark.parametrize('path', [
    'static/js/app.modular.js',
    'static/js/modules/composables/ui.js',
    'templates/admin.html',
    'templates/group-admin.html',
])
def test_file_size_call_sites_delegate_to_i18n(path):
    """Positive control: removing a copy must not leave the page without one."""
    text = open(os.path.join(REPO, path), encoding='utf-8').read()
    assert 'window.i18n.formatFileSize(' in text, path
    # Guarded: the page still renders before i18n.js has loaded.
    assert 'window.i18n?.formatFileSize ?' in text, path


# static/sw.js serves i18n.js stale-while-revalidate while the ES modules load
# fresh, so a page can run against an older i18n.js. A method that file may
# lack is called as window.i18n?.method?.(): getIntlLocale() is new in #480.
UNGUARDED_I18N_CALL = re.compile(r'\bi18n\??\.getIntlLocale\(')


def test_new_i18n_methods_are_called_guarded():
    offenders = []
    paths = list(_walk('static/js', '.js', skip=('.test.js',))) + list(_walk('templates', '.html'))
    for path in paths:
        rel = os.path.relpath(path, REPO)
        if rel == 'static/js/i18n.js':
            continue
        text = open(path, encoding='utf-8').read()
        offenders += [f'{rel}:{text.count(chr(10), 0, m.start()) + 1}' for m in UNGUARDED_I18N_CALL.finditer(text)]
    assert not offenders, 'call as window.i18n?.getIntlLocale?.():\n' + '\n'.join(f'  {o}' for o in offenders)


@pytest.mark.parametrize('snippet,matches', [
    ('x.toLocaleString(window.i18n?.getIntlLocale())', True),
    ('x.toLocaleString(window.i18n.getIntlLocale())', True),
    ('x.toLocaleString(window.i18n?.getIntlLocale?.())', False),
])
def test_unguarded_i18n_call_scanner(snippet, matches):
    assert bool(UNGUARDED_I18N_CALL.search(snippet)) is matches


# Percentages shown as text follow the interface language too (#480): a
# template that writes ${ x }% or ${ x.toFixed(1) }%, or appends '%' in a
# v-text or :title, shows "12.5%" in German. formatPercent() (i18n.formatPercent)
# gives "12,5 %". Bar widths (:style, style=) are layout and stay as they are.
PERCENT_TEXT = re.compile(r"""\$\{[^}]*\}\s*%|(?:v-text|:title|:aria-label)="[^"]*\+\s*'%""")


def test_no_percentage_text_ignores_the_interface_locale():
    offenders = []
    for path in _walk('templates', '.html'):
        text = open(path, encoding='utf-8').read()
        for m in PERCENT_TEXT.finditer(text):
            line = text[text.rfind(chr(10), 0, m.start()) + 1:text.find(chr(10), m.end())]
            if 'window.i18n?.formatPercent ?' in line:
                continue  # the fallback of a guarded formatPercent, for a page without i18n.js
            offenders.append(f'{os.path.relpath(path, REPO)}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}')
    assert not offenders, 'format percentages with formatPercent():\n' + '\n'.join(f'  {o}' for o in offenders)


@pytest.mark.parametrize('snippet,matches', [
    ('<span>${ row.pct.toFixed(1) }%</span>', True),
    ('<span>${ tokenBudget.percentage }%</span>', True),
    ("""<span v-text="contextUsage + '%'"></span>""", True),
    (':title="\'Context: \' + contextUsage + \'% used\'"', True),
    ('<span>${ formatPercent(row.pct) }</span>', False),
    (':style="{ width: row.pct + \'%\' }"', False),
    ('style="width: {{ [pct, 100]|min }}%"', False),
])
def test_percentage_text_scanner(snippet, matches):
    assert bool(PERCENT_TEXT.search(snippet)) is matches


def test_every_locale_has_every_file_size_unit():
    units = {'bytes', 'kilobytes', 'megabytes', 'gigabytes', 'terabytes'}
    for lang in _languages():
        table = _load(lang).get('fileSize', {})
        assert set(table) == units, f'{lang}.json fileSize has {sorted(table)}'
        assert all('{{count}}' in v for v in table.values()), f'{lang}.json fileSize lacks {{{{count}}}}'


# API responses (#477): an 'error' or 'message' in a response comes from the
# catalogs, through api_error('code') or translate('server.apiMessage.key'),
# so a new handler cannot return a fixed English string.
API_LITERAL = re.compile(r"""['"](?:error|message)['"]\s*:\s*[fFrR]?['"]""")


def _api_literals():
    found = []
    paths = list(_walk('src/api', '.py')) + [os.path.join(REPO, 'src', 'app.py')]
    for path in paths:
        for number, line in enumerate(open(path, encoding='utf-8'), 1):
            if API_LITERAL.search(line) and not line.lstrip().startswith('#'):
                found.append(f'{os.path.relpath(path, REPO)}:{number}: {line.strip()}')
    return found


def test_no_fixed_english_error_or_message_in_api_responses():
    offenders = _api_literals()
    assert not offenders, 'use api_error() or translate():\n' + '\n'.join(f'  {o}' for o in offenders)


@pytest.mark.parametrize('snippet,matches', [
    ("return jsonify({'error': 'Recording not found'}), 404", True),
    ('return jsonify({"message": f"Deleted {n} tags"})', True),
    ("return jsonify(api_error('recording_not_found')), 404", False),
    ("return jsonify({'message': translate('server.apiMessage.tagDeleted')})", False),
    ("return jsonify({'error': str(e)}), 500", False),
])
def test_api_literal_scanner(snippet, matches):
    assert bool(API_LITERAL.search(snippet)) is matches
