"""
Datetime normalization helpers.

Backend timestamps — including meeting_date — are stored as naive UTC and
converted to the viewer's timezone client-side (parseServerInstant). These
helpers normalize incoming values to that convention.
"""

import re
from datetime import date, datetime, timezone
from typing import Optional

# The days a recording list date filter accepts (#631). A value outside is
# refused with 400, never dropped: a filter that does nothing returned the
# whole list. The date inputs of the filter panel use the same bounds.
FILTER_DATE_MIN = date(1900, 1, 1)
FILTER_DATE_MAX = date(2100, 12, 31)
_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def filter_day(value) -> Optional[date]:
    """The day of a YYYY-MM-DD filter value, or None when the value is not a
    calendar day from FILTER_DATE_MIN to FILTER_DATE_MAX."""
    if not isinstance(value, str) or not _DAY.match(value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if FILTER_DATE_MIN <= day <= FILTER_DATE_MAX else None


def filter_year_ok(year: int) -> bool:
    """True for a year inside the filter bounds."""
    return FILTER_DATE_MIN.year <= year <= FILTER_DATE_MAX.year


def to_utc_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to naive UTC.

    Aware datetimes are converted to UTC before the tzinfo is stripped.
    Naive datetimes are returned unchanged (assumed to be UTC already).
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """An ISO 8601 string with an explicit UTC marker, e.g. 2026-11-15T09:30:00Z.

    For answers whose reader computes with the instant (the elapsed time of a
    running job). Stored values are naive UTC; the trailing Z says so, and
    the browser's parseServerInstant reads both forms.
    """
    dt = to_utc_naive(dt)
    if dt is None:
        return None
    return dt.isoformat() + 'Z'
