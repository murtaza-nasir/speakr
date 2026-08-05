"""
Parse meeting dates out of uploaded filenames (#342).

Voice recorders and phone apps commonly encode the recording date in the
filename (``20260716_meeting.mp3``, ``260716_1102.m4a``). When a user
enables filename date parsing, the date found here takes precedence over
file metadata / browser lastModified when setting ``meeting_date``.

Timezone semantics: a filename date is a *wall-clock* value in the
uploader's local timezone. ``meeting_date`` is stored as naive UTC and
rendered viewer-local, so the parsed value must be shifted to UTC before
storage. Callers pass ``tz_offset_minutes`` in JavaScript
``getTimezoneOffset()`` convention (minutes to ADD to local time to get
UTC; positive west of UTC). When no offset is known, date-only matches
are anchored at 12:00 UTC so the calendar date survives rendering in any
viewer timezone, and time-bearing matches are treated as already UTC.
"""

import re
from datetime import datetime, timedelta
from typing import Optional


# Presets are matched against the filename stem (extension stripped).
# Digit lookarounds stop a longer digit run (e.g. a phone number) from
# donating a spurious "date". Order within AUTO_PRESET_ORDER runs the most
# specific patterns first.
PRESET_PATTERNS = {
    # 20260716_1102, 20260716-1102, "20260716 1102"
    'yyyymmdd_hhmm': re.compile(
        r'(?<!\d)(?P<year>(?:19|20)\d{2})(?P<month>\d{2})(?P<day>\d{2})'
        r'[_\- ](?P<hour>\d{2})[:.]?(?P<minute>\d{2})(?!\d)'
    ),
    # 2026-07-16, 2026_07_16, 2026.07.16, optionally followed by 11-02 / 11:02 / 1102
    'yyyy-mm-dd': re.compile(
        r'(?<!\d)(?P<year>(?:19|20)\d{2})[-_.](?P<month>\d{2})[-_.](?P<day>\d{2})'
        r'(?:[_\- T](?P<hour>\d{2})[:.\-_]?(?P<minute>\d{2})(?!\d))?'
    ),
    # 20260716
    'yyyymmdd': re.compile(
        r'(?<!\d)(?P<year>(?:19|20)\d{2})(?P<month>\d{2})(?P<day>\d{2})(?!\d)'
    ),
    # 260716_1102
    'yymmdd_hhmm': re.compile(
        r'(?<!\d)(?P<year>\d{2})(?P<month>\d{2})(?P<day>\d{2})'
        r'[_\- ](?P<hour>\d{2})[:.]?(?P<minute>\d{2})(?!\d)'
    ),
    # 260716 — ambiguous six digits; only used when explicitly selected,
    # never as part of 'auto' (see AUTO_PRESET_ORDER).
    'yymmdd': re.compile(
        r'(?<!\d)(?P<year>\d{2})(?P<month>\d{2})(?P<day>\d{2})(?!\d)'
    ),
}

# 'auto' tries these in order. Bare yymmdd is excluded: any six digits in
# a filename would qualify, which produces far too many false positives.
AUTO_PRESET_ORDER = ('yyyymmdd_hhmm', 'yyyy-mm-dd', 'yyyymmdd', 'yymmdd_hhmm')

VALID_PATTERN_KEYS = ('auto',) + tuple(PRESET_PATTERNS.keys()) + ('custom',)

_MIN_YEAR = 1970
_MAX_YEAR = 2100


def _build_datetime(groups) -> Optional[datetime]:
    """Turn named regex groups into a validated datetime, or None."""
    try:
        year = int(groups['year'])
        if year < 100:
            year += 2000
        month = int(groups['month'])
        day = int(groups['day'])
        hour = int(groups.get('hour') or 0)
        minute = int(groups.get('minute') or 0)
    except (KeyError, TypeError, ValueError):
        return None

    if not (_MIN_YEAR <= year <= _MAX_YEAR):
        return None
    if hour > 23 or minute > 59:
        return None
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _has_time(groups) -> bool:
    return bool(groups.get('hour'))


def parse_filename_date(filename: str, pattern_key: str = 'auto',
                        custom_regex: Optional[str] = None,
                        tz_offset_minutes: Optional[int] = None) -> Optional[datetime]:
    """
    Extract a meeting date from *filename* and return it as naive UTC.

    Args:
        filename: Original filename (extension is stripped before matching).
        pattern_key: One of VALID_PATTERN_KEYS.
        custom_regex: Required when pattern_key == 'custom'. Must define
            named groups ``year``, ``month``, ``day`` and may define
            ``hour`` and ``minute``.
        tz_offset_minutes: JavaScript getTimezoneOffset() value for the
            uploader (minutes to add to local time to reach UTC). None
            when the client timezone is unknown.

    Returns:
        Naive-UTC datetime, or None when nothing (valid) matched. Never
        raises: a broken custom regex simply yields None.
    """
    if not filename:
        return None
    stem = filename.rsplit('.', 1)[0] if '.' in filename else filename

    candidates = []
    if pattern_key == 'auto':
        candidates = [PRESET_PATTERNS[k] for k in AUTO_PRESET_ORDER]
    elif pattern_key in PRESET_PATTERNS:
        candidates = [PRESET_PATTERNS[pattern_key]]
    elif pattern_key == 'custom' and custom_regex:
        try:
            candidates = [re.compile(custom_regex)]
        except re.error:
            return None
    else:
        return None

    for pattern in candidates:
        try:
            match = pattern.search(stem)
        except Exception:
            continue
        if not match:
            continue
        groups = match.groupdict()
        parsed = _build_datetime(groups)
        if parsed is None:
            continue

        # Local wall-clock -> naive UTC per the storage convention.
        if tz_offset_minutes is not None:
            try:
                offset = int(tz_offset_minutes)
            except (TypeError, ValueError):
                offset = None
            # Sanity-bound the offset to real-world values (UTC-12..UTC+14).
            if offset is not None and -14 * 60 <= offset <= 12 * 60:
                return parsed + timedelta(minutes=offset)

        if not _has_time(groups):
            # Date-only with unknown timezone: anchor at 12:00 UTC so the
            # calendar date renders unchanged in any viewer timezone.
            return parsed.replace(hour=12, minute=0)
        return parsed

    return None


def validate_custom_regex(custom_regex: str) -> Optional[str]:
    """
    Validate a user-supplied custom regex. Returns an error message string,
    or None when the regex is acceptable.
    """
    if not custom_regex or not custom_regex.strip():
        return 'Custom pattern is required when the custom option is selected.'
    if len(custom_regex) > 500:
        return 'Custom pattern is too long (max 500 characters).'
    try:
        compiled = re.compile(custom_regex)
    except re.error as e:
        return f'Invalid regular expression: {e}'
    required = {'year', 'month', 'day'}
    missing = required - set(compiled.groupindex.keys())
    if missing:
        return ('Custom pattern must define named groups (?P<year>...), '
                '(?P<month>...) and (?P<day>...); missing: '
                + ', '.join(sorted(missing)))
    return None
