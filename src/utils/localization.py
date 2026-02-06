"""
Server-side localization utilities for export templates.

This module provides utilities to load localized labels from
static/locales/*.json files for use in export templates.
"""

import json
import os
import logging
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


def clear_locale_cache():
    """Clear the locale cache (useful for testing or hot-reloading)."""
    global _locale_cache
    _locale_cache = {}
