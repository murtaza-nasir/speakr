"""Filename dates across a daylight-saving change (#412 B9).

The browser sent only its current UTC offset, so a filename dated in summer,
uploaded in winter, was converted with the winter offset: one hour off. The
browser's IANA zone gives the offset that applied on the file's date.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.filename_dates import parse_filename_date


def test_named_zone_uses_the_offset_on_the_files_date():
    # 16 Jul 2026 11:02 in New York is EDT (UTC-4) -> 15:02 UTC.
    winter_offset = 300   # getTimezoneOffset() in January (EST)
    assert parse_filename_date("20260716_1102.m4a", "yyyymmdd_hhmm", tz_offset_minutes=winter_offset,
                               tz_name="America/New_York") == datetime(2026, 7, 16, 15, 2)


def test_offset_only_keeps_the_old_behaviour():
    assert parse_filename_date("20260716_1102.m4a", "yyyymmdd_hhmm", tz_offset_minutes=300) == datetime(2026, 7, 16, 16, 2)


def test_invalid_zone_falls_back_to_the_offset():
    assert parse_filename_date("20260716_1102.m4a", "yyyymmdd_hhmm", tz_offset_minutes=300,
                               tz_name="Not/AZone") == datetime(2026, 7, 16, 16, 2)
