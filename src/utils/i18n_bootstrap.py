"""Translations embedded in the page for the client-side i18n (static/js/i18n.js).

Pages that load i18n.js include templates/includes/i18n_bootstrap.html first.
In that include, ``window.__I18N_BOOTSTRAP = {locale, translations}`` is set
to the user's interface language and English, so the first render is already
translated and no locale file has to be fetched before it.

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


def i18n_bootstrap(locale=None):
    """Return ``{'locale': ..., 'translations': {code: dict}}`` for the page.

    ``locale`` defaults to the signed-in user's interface language. English
    is always included as the fallback; for an unknown locale, only English is returned.
    """
    if not locale:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                locale = getattr(current_user, 'ui_language', None)
        except Exception:  # outside a request context
            locale = None
    locale = locale or FALLBACK_LOCALE

    translations = {}
    data = _load(locale)
    if data is None:
        locale = FALLBACK_LOCALE
    else:
        translations[locale] = data
    if FALLBACK_LOCALE not in translations:
        fallback = _load(FALLBACK_LOCALE)
        if fallback is not None:
            translations[FALLBACK_LOCALE] = fallback
    return {'locale': locale, 'translations': translations}
