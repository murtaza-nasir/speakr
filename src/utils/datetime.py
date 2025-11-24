"""
Datetime utilities for timezone handling and formatting.

This module provides functions for converting and formatting datetimes
with timezone awareness.
"""

import os
import logging
import pytz
from babel.dates import format_datetime

# Module-level logger
logger = logging.getLogger(__name__)


def local_datetime_filter(dt):
    """
    Format a UTC datetime object to the user's local timezone.

    Args:
        dt: datetime object to format (assumed UTC if naive)

    Returns:
        str: Formatted datetime string in user's timezone
    """
    if dt is None:
        return ""

    # Get timezone from .env, default to UTC
    user_tz_name = os.environ.get('TIMEZONE', 'UTC')
    try:
        user_tz = pytz.timezone(user_tz_name)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.utc
        logger.warning(f"Invalid TIMEZONE '{user_tz_name}' in .env. Defaulting to UTC.")

    # If the datetime object is naive, assume it's UTC
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)

    # Convert to the user's timezone
    local_dt = dt.astimezone(user_tz)

    # Format it nicely
    return format_datetime(local_dt, format='medium', locale='en_US')
