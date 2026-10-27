"""
Server-side localization utilities.

This module loads localized strings from static/locales/*.json, the catalogs
the browser uses, for export templates and, through translate(), for text
produced on the server (flash messages, account emails).
"""

import json
import os
import re
import logging
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Cache for loaded locales
_locale_cache: Dict[str, dict] = {}


def get_locales_dir() -> Path:
    """Get the path to the locales directory."""
    # Navigate from src/utils to static/locales
    base_dir = Path(__file__).parent.parent.parent
    return base_dir / 'static' / 'locales'


def load_locale(language: str) -> dict:
    """
    Load locale data for a given language.

    Args:
        language: Language code (e.g., 'en', 'de', 'fr')

    Returns:
        Dictionary containing all locale strings
    """
    # Check cache first
    if language in _locale_cache:
        return _locale_cache[language]

    locales_dir = get_locales_dir()
    locale_file = locales_dir / f'{language}.json'

    # Fallback to English if requested language doesn't exist
    if not locale_file.exists():
        logger.warning(f"Locale file not found for '{language}', falling back to English")
        locale_file = locales_dir / 'en.json'
        language = 'en'

    try:
        with open(locale_file, 'r', encoding='utf-8') as f:
            locale_data = json.load(f)
            _locale_cache[language] = locale_data
            return locale_data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading locale file '{locale_file}': {e}")
        # Return empty dict on error
        return {}


def get_export_labels(language: str) -> dict:
    """
    Get localized export labels for a given language.

    Args:
        language: Language code (e.g., 'en', 'de', 'fr')

    Returns:
        Dictionary containing export-specific labels
    """
    locale_data = load_locale(language)

    # Get exportLabels section, or fall back to defaults
    export_labels = locale_data.get('exportLabels', {})

    # Default English labels as fallback
    defaults = {
        'metadata': 'Metadata',
        'notes': 'Notes',
        'summary': 'Summary',
        'transcription': 'Transcription',
        'date': 'Date',
        'created': 'Created',
        'originalFile': 'Original File',
        'fileSize': 'File Size',
        'participants': 'Participants',
        'tags': 'Tags',
        'transcriptionTime': 'Transcription Time',
        'summarizationTime': 'Summarization Time',
        'footer': 'Generated with [Speakr](https://github.com/learnedmachine/speakr)'
    }

    # Merge defaults with loaded labels
    result = defaults.copy()
    result.update(export_labels)

    return result


def format_date_localized(dt: datetime, language: str) -> str:
    """
    Format a datetime in a localized format.

    Args:
        dt: The datetime to format
        language: Language code for localization

    Returns:
        Localized date string
    """
    if dt is None:
        return ''

    # Define locale-specific date formats
    date_formats = {
        'en': '%B %d, %Y',           # January 15, 2026
        'de': '%d. %B %Y',            # 15. Januar 2026
        'fr': '%d %B %Y',             # 15 janvier 2026
        'es': '%d de %B de %Y',       # 15 de enero de 2026
        'zh': '%Y年%m月%d日',          # 2026年01月15日
        'ru': '%d %B %Y г.',          # 15 января 2026 г.
    }

    # Month names for different languages
    month_names = {
        'de': {
            'January': 'Januar', 'February': 'Februar', 'March': 'März',
            'April': 'April', 'May': 'Mai', 'June': 'Juni',
            'July': 'Juli', 'August': 'August', 'September': 'September',
            'October': 'Oktober', 'November': 'November', 'December': 'Dezember'
        },
        'fr': {
            'January': 'janvier', 'February': 'février', 'March': 'mars',
            'April': 'avril', 'May': 'mai', 'June': 'juin',
            'July': 'juillet', 'August': 'août', 'September': 'septembre',
            'October': 'octobre', 'November': 'novembre', 'December': 'décembre'
        },
        'es': {
            'January': 'enero', 'February': 'febrero', 'March': 'marzo',
            'April': 'abril', 'May': 'mayo', 'June': 'junio',
            'July': 'julio', 'August': 'agosto', 'September': 'septiembre',
            'October': 'octubre', 'November': 'noviembre', 'December': 'diciembre'
        },
        'ru': {
            'January': 'января', 'February': 'февраля', 'March': 'марта',
            'April': 'апреля', 'May': 'мая', 'June': 'июня',
            'July': 'июля', 'August': 'августа', 'September': 'сентября',
            'October': 'октября', 'November': 'ноября', 'December': 'декабря'
        }
    }

    # Get format for language, default to English
    date_format = date_formats.get(language, date_formats['en'])

    # Format the date
    formatted = dt.strftime(date_format)

    # Replace English month names with localized versions
    if language in month_names:
        for eng, local in month_names[language].items():
            formatted = formatted.replace(eng, local)

    return formatted


def format_datetime_localized(dt: datetime, language: str) -> str:
    """
    Format a datetime with time in a localized format.

    Args:
        dt: The datetime to format
        language: Language code for localization

    Returns:
        Localized datetime string
    """
    if dt is None:
        return ''

    date_part = format_date_localized(dt, language)

    # Time format varies by language
    time_formats = {
        'en': '%I:%M %p',       # 02:30 PM
        'de': '%H:%M Uhr',      # 14:30 Uhr
        'fr': '%H:%M',          # 14:30
        'es': '%H:%M',          # 14:30
        'zh': '%H:%M',          # 14:30
        'ru': '%H:%M',          # 14:30
    }

    time_format = time_formats.get(language, time_formats['en'])
    time_part = dt.strftime(time_format)

    # Combine with appropriate connector
    connectors = {
        'en': ' at ',
        'de': ' um ',
        'fr': ' à ',
        'es': ' a las ',
        'zh': ' ',
        'ru': ' в ',
    }

    connector = connectors.get(language, ' at ')

    return f"{date_part}{connector}{time_part}"


FALLBACK_LANGUAGE = 'en'
# {{name}}, the placeholder syntax interpolate() in static/js/i18n.js fills.
_PLACEHOLDER_RE = re.compile(r'\{\{(\w+)\}\}')


@lru_cache(maxsize=1)
def available_languages() -> tuple:
    """Codes of the shipped locale files, e.g. ('de', 'en', ..., 'pt-BR', 'zh')."""
    return tuple(sorted(p.stem for p in get_locales_dir().glob('*.json')))


def match_language(code: Optional[str]) -> Optional[str]:
    """
    Map a language code to a shipped locale, or None when none fits.

    Case is ignored and '_' is read as '-'. A region without its own catalog
    falls back to the base language ('de-AT' -> 'de', 'zh-CN' -> 'zh'), and a
    base language to its only regional catalog ('pt' -> 'pt-BR').
    """
    if not code or not isinstance(code, str):
        return None
    shipped = {lang.lower(): lang for lang in available_languages()}
    code = code.strip().replace('_', '-').lower()
    base = code.split('-')[0]
    if code in shipped or base in shipped:
        return shipped.get(code) or shipped[base]
    regional = [lang for key, lang in shipped.items() if key.split('-')[0] == base]
    return regional[0] if len(regional) == 1 else None


def request_language() -> str:
    """
    Language for text produced in the current request.

    The signed-in user's interface language comes first, then the
    Accept-Language header. Outside a request, English is used.
    """
    from flask import has_request_context, request
    if not has_request_context():
        return FALLBACK_LANGUAGE
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            language = match_language(getattr(current_user, 'ui_language', None))
            if language:
                return language
    except Exception:  # app without a login manager
        pass
    # best_match also pairs 'de-AT' with 'de' and 'pt' with 'pt-BR'.
    return request.accept_languages.best_match(available_languages()) or FALLBACK_LANGUAGE


def _resolve_language(language: Optional[str]) -> str:
    """A shipped locale for the given code, else for the current request."""
    return (match_language(language) if language else request_language()) or FALLBACK_LANGUAGE


def _lookup(catalog: dict, key: str) -> Optional[str]:
    value = catalog
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value if isinstance(value, str) and value else None


def translate(key: str, language: Optional[str] = None, **params) -> str:
    """
    Translate a dotted catalog key on the server, as i18n.t() does in the browser.

    Args:
        key: Dotted key, e.g. 'server.flash.passwordUpdated'
        language: Language code; defaults to request_language()
        **params: Values for {{name}} placeholders

    Returns:
        The string in that language, else in English, else the key itself.
    """
    language = _resolve_language(language)
    text = _lookup(load_locale(language), key)
    if text is None and language != FALLBACK_LANGUAGE:
        text = _lookup(load_locale(FALLBACK_LANGUAGE), key)
    if text is None:
        logger.warning("Translation not found for key: %s", key)
        return key
    return _PLACEHOLDER_RE.sub(
        lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


def format_number(value, language: Optional[str] = None, max_fraction_digits: int = 0,
                  grouping: bool = True) -> str:
    """
    A number in the interface language's notation, as i18n.formatNumber()
    does in the browser: '1,234.5' in English, '1.234,5' in German (#481).

    Rounds half up to at most max_fraction_digits and drops trailing zeros.
    """
    from babel import Locale
    from babel.numbers import format_decimal
    language = _resolve_language(language)
    rounded = Decimal(value).quantize(Decimal(1).scaleb(-max_fraction_digits), rounding=ROUND_HALF_UP)
    pattern = ('#,##0' if grouping else '0') + ('.' + '#' * max_fraction_digits if max_fraction_digits else '')
    return format_decimal(rounded, format=pattern, locale=Locale.parse(language, sep='-'))


def format_percent(percent, language: Optional[str] = None) -> str:
    """
    A percentage (0-100 scale) in the interface language's notation, as
    i18n.formatPercent() does in the browser: '85.3%' in English, '85,3 %' in
    German. At most one decimal, rounded half up.
    """
    from babel import Locale
    from babel.numbers import format_percent as babel_format_percent
    locale = Locale.parse(_resolve_language(language), sep='-')
    rounded = Decimal(str(percent)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    # The locale's own pattern ('#,##0%', '#,##0\xa0%') with one optional decimal.
    head, _, tail = locale.percent_formats[None].pattern.rpartition('0')
    return babel_format_percent(rounded / 100, format=f'{head}0.#{tail}', locale=locale)


def api_error(code: str, language: Optional[str] = None, **params) -> dict:
    """
    The body of an API error response (#477): the message from
    server.apiError.<code> in the interface language, and the code itself,
    which stays the same in every language, e.g.
    {'error': 'Aufnahme nicht gefunden', 'code': 'recording_not_found'}.
    """
    return {'error': translate(f'server.apiError.{code}', language, **params), 'code': code}


_FILE_SIZE_UNITS = ('bytes', 'kilobytes', 'megabytes', 'gigabytes', 'terabytes')


def format_file_size(size, language: Optional[str] = None) -> str:
    """
    A byte count with the fileSize.* units, as i18n.formatFileSize() does in
    the browser: 1024-based, at most one decimal, e.g. '1.5 MB' or '1,5 MB'.
    """
    size = max(Decimal(size or 0), Decimal(0))
    unit = 0
    while unit < len(_FILE_SIZE_UNITS) - 1 and size >= 1024 ** (unit + 1):
        unit += 1
    # 1023.97 KB would round to "1024 KB"; that is 1 MB.
    if unit < len(_FILE_SIZE_UNITS) - 1 and (size / 1024 ** unit).quantize(
            Decimal('0.1'), rounding=ROUND_HALF_UP) >= 1024:
        unit += 1
    # No grouping, as in the browser: German "1.023 B" reads as 1.023.
    count = format_number(size / 1024 ** unit, language, max_fraction_digits=1, grouping=False)
    return translate(f'fileSize.{_FILE_SIZE_UNITS[unit]}', language, count=count)


def clear_locale_cache():
    """Clear the locale cache (useful for testing or hot-reloading)."""
    global _locale_cache
    _locale_cache = {}
    available_languages.cache_clear()
