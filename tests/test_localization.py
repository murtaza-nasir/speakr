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
# t('a.b') / $t("a.b") / i18n.t('a.b'). Requires a dot so bare t('x') helpers
# and unrelated single-argument calls are not mistaken for keys.
JS_PATTERN = re.compile(r"""(?:\$t|\bt|i18n\.t)\(\s*['"]([a-zA-Z][\w.]*\.[\w.]+)['"]""")


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
        for key in TEMPLATE_PATTERN.findall(text):
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
