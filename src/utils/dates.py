"""
Datetime normalization helpers.

Backend timestamps — including meeting_date — are stored as naive UTC and
converted to the viewer's timezone client-side (parseServerInstant). These
helpers normalize incoming values to that convention.
"""

from datetime import datetime, timezone
from typing import Optional


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
