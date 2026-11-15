"""Translations embedded in the page for the client-side i18n (static/js/i18n.js).

Pages that load i18n.js include templates/includes/i18n_bootstrap.html first.
In that include, ``window.__I18N_BOOTSTRAP = {locale, translations, fallback}``
is set to the user's interface language, so the first render is already
translated and no locale file has to be fetched before it.

English, the fallback language, is embedded only on English pages. On a page
in another language, ``fallback`` holds the English strings of the keys that
the language's file lacks (none while tests/test_localization.py passes), so
a key missing there still shows English text and the English file
(about 190 KB) is not part of the page.

Locale files are read from static/locales/ and cached per process; a file
is read again when its modification time changes.
"""

import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

FALLBACK_LOCALE = 'en'
_LOCALE_RE = re.compile(r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$')
_LOCALES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            'static', 'locales')

_cache = {}
_lock = threading.Lock()


def _load(locale):
    """Return the parsed locale file, or None when it is missing or invalid."""
    if not locale or not _LOCALE_RE.match(locale):
        return None
    path = os.path.join(_LOCALES_DIR, f'{locale}.json')
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    cached = _cache.get(locale)
    if cached and cached[0] == mtime:
        return cached[1]
    with _lock:
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.error("Locale file %s could not be read: %s", path, e)
            return None
        _cache[locale] = (mtime, data)
        return data


def _gaps(table, fallback):
    """The entries of ``fallback`` that ``table`` lacks, in the same nesting."""
    out = {}
    for key, value in fallback.items():
        mine = table.get(key) if isinstance(table, dict) else None
        if isinstance(value, dict):
            inner = _gaps(mine if isinstance(mine, dict) else {}, value)
            if inner:
                out[key] = inner
        elif not isinstance(mine, str) or not mine:
            out[key] = value
    return out


_gap_cache = {}


def _fallback_gaps(locale):
    """English strings for the keys missing in ``locale``; None without English."""
    table, fallback = _load(locale), _load(FALLBACK_LOCALE)
    if table is None or fallback is None:
        return None
    stamp = (_cache[locale][0], _cache[FALLBACK_LOCALE][0])
    cached = _gap_cache.get(locale)
    if cached and cached[0] == stamp:
        return cached[1]
    gaps = _gaps(table, fallback)
    _gap_cache[locale] = (stamp, gaps)
    return gaps


def i18n_bootstrap(locale=None):
    """Return ``{'locale': ..., 'translations': {code: dict}}`` for the page.

    ``locale`` defaults to the signed-in user's interface language. Only that
    language is included. For another language than English, ``fallback``
    is added: the English strings of the keys the language lacks (see the
    module text). For an unknown locale, English is returned.
    """
    if not locale:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                locale = getattr(current_user, 'ui_language', None)
        except Exception:  # outside a request context
            locale = None
    locale = locale or FALLBACK_LOCALE

    data = _load(locale)
    if data is None:
        locale, data = FALLBACK_LOCALE, _load(FALLBACK_LOCALE)
    result = {'locale': locale, 'translations': {locale: data} if data is not None else {}}
    if locale != FALLBACK_LOCALE and data is not None:
        gaps = _fallback_gaps(locale)
        if gaps is not None:
            result['fallback'] = gaps
    return result
