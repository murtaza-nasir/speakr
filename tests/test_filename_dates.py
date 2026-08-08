"""
Tests for filename meeting-date parsing (#342).

Covers the parsing utility (presets, auto mode, custom regex, timezone
conversion, validation) and the account-settings save path.
"""

import sys
import os
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.filename_dates import (
    parse_filename_date,
    validate_custom_regex,
    VALID_PATTERN_KEYS,
)


# ===========================================================================
# Preset patterns
# ===========================================================================

def test_yyyymmdd_basic():
    assert parse_filename_date('20260716_meeting.mp3', 'yyyymmdd') == datetime(2026, 7, 16, 12, 0)


def test_yyyymmdd_hhmm():
    # Time-bearing match without tz info is treated as UTC.
    assert parse_filename_date('20260716_1102.mp3', 'yyyymmdd_hhmm') == datetime(2026, 7, 16, 11, 2)


def test_yyyy_mm_dd_hyphen():
    assert parse_filename_date('2026-07-16 team sync.m4a', 'yyyy-mm-dd') == datetime(2026, 7, 16, 12, 0)


def test_yyyy_mm_dd_with_time():
    assert parse_filename_date('2026-07-16 11-02 sync.m4a', 'yyyy-mm-dd') == datetime(2026, 7, 16, 11, 2)


def test_yyyy_mm_dd_underscore_and_dot_separators():
    assert parse_filename_date('2026_07_16.wav', 'yyyy-mm-dd') == datetime(2026, 7, 16, 12, 0)
    assert parse_filename_date('2026.07.16.wav', 'yyyy-mm-dd') == datetime(2026, 7, 16, 12, 0)


def test_yymmdd_hhmm():
    assert parse_filename_date('260716_1102.mp3', 'yymmdd_hhmm') == datetime(2026, 7, 16, 11, 2)


def test_yymmdd_explicit_only():
    assert parse_filename_date('260716.mp3', 'yymmdd') == datetime(2026, 7, 16, 12, 0)


# ===========================================================================
# Auto mode
# ===========================================================================

def test_auto_matches_common_formats():
    assert parse_filename_date('20260716_meeting.mp3', 'auto') == datetime(2026, 7, 16, 12, 0)
    assert parse_filename_date('20260716_1102_call.mp3', 'auto') == datetime(2026, 7, 16, 11, 2)
    assert parse_filename_date('rec 2026-07-16.ogg', 'auto') == datetime(2026, 7, 16, 12, 0)
    assert parse_filename_date('260716_1102.mp3', 'auto') == datetime(2026, 7, 16, 11, 2)


def test_auto_excludes_bare_yymmdd():
    # Any six digits would qualify as yymmdd, so auto must not guess.
    assert parse_filename_date('123456.mp3', 'auto') is None


def test_auto_ignores_phone_numbers():
    # 10-digit run must not donate an inner "date".
    assert parse_filename_date('8005551234.wav', 'auto') is None


def test_no_match_returns_none():
    assert parse_filename_date('team meeting notes.mp3', 'auto') is None


def test_invalid_calendar_date_rejected():
    assert parse_filename_date('20261340_x.mp3', 'yyyymmdd') is None  # month 13
    assert parse_filename_date('20260732_x.mp3', 'yyyymmdd') is None  # day 32


def test_invalid_time_rejected():
    assert parse_filename_date('20260716_2560.mp3', 'yyyymmdd_hhmm') is None


def test_empty_and_none_filename():
    assert parse_filename_date('', 'auto') is None
    assert parse_filename_date(None, 'auto') is None


# ===========================================================================
# Timezone conversion
# ===========================================================================

def test_tz_offset_applied_to_time_match():
    # UTC-5 (offset +300): 11:02 local -> 16:02 UTC
    got = parse_filename_date('20260716_1102.mp3', 'yyyymmdd_hhmm', tz_offset_minutes=300)
    assert got == datetime(2026, 7, 16, 16, 2)


def test_tz_offset_applied_to_date_only_match():
    # With a known offset a date-only match anchors at local midnight.
    got = parse_filename_date('20260716.mp3', 'yyyymmdd', tz_offset_minutes=300)
    assert got == datetime(2026, 7, 16, 5, 0)


def test_date_only_without_offset_anchors_at_noon_utc():
    got = parse_filename_date('20260716.mp3', 'yyyymmdd')
    assert got == datetime(2026, 7, 16, 12, 0)


def test_absurd_tz_offset_ignored():
    got = parse_filename_date('20260716_1102.mp3', 'yyyymmdd_hhmm', tz_offset_minutes=100000)
    assert got == datetime(2026, 7, 16, 11, 2)


# ===========================================================================
# Custom regex
# ===========================================================================

def test_custom_regex_named_groups():
    rx = r'rec-(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})'
    got = parse_filename_date('rec-16.07.2026.mp3', 'custom', custom_regex=rx)
    assert got == datetime(2026, 7, 16, 12, 0)


def test_custom_regex_with_time_groups():
    rx = r'(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})T(?P<hour>\d{2})(?P<minute>\d{2})'
    got = parse_filename_date('20260716T1102.mp3', 'custom', custom_regex=rx)
    assert got == datetime(2026, 7, 16, 11, 2)


def test_custom_regex_invalid_returns_none():
    assert parse_filename_date('x.mp3', 'custom', custom_regex='(unclosed') is None


def test_custom_without_regex_returns_none():
    assert parse_filename_date('20260716.mp3', 'custom') is None


def test_unknown_pattern_key_returns_none():
    assert parse_filename_date('20260716.mp3', 'nonsense') is None


def test_validate_custom_regex_accepts_good():
    assert validate_custom_regex(r'(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})') is None


def test_validate_custom_regex_missing_groups():
    err = validate_custom_regex(r'(?P<year>\d{4})')
    assert err is not None and 'month' in err and 'day' in err


def test_validate_custom_regex_broken():
    assert validate_custom_regex('(unclosed') is not None


def test_validate_custom_regex_empty():
    assert validate_custom_regex('') is not None


def test_pattern_keys_include_ui_options():
    for key in ('auto', 'yyyymmdd', 'yyyymmdd_hhmm', 'yyyy-mm-dd', 'yymmdd_hhmm', 'yymmdd', 'custom'):
        assert key in VALID_PATTERN_KEYS


# ===========================================================================
# Settings save path (POST /account preferences form)
# ===========================================================================

os.environ.setdefault('WTF_CSRF_ENABLED', 'false')

from src.app import app, db
from src.models import User

app.config['WTF_CSRF_ENABLED'] = False


def _make_settings_user(suffix):
    user = User(
        username=f'fnd_settings_user_{suffix}',
        email=f'fnd_settings_{suffix}@local.test',
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_settings_save_and_validation():
    with app.app_context():
        user = _make_settings_user('a')
        client = app.test_client()
        _login(client, user)
        try:
            # Enable with a preset.
            resp = client.post('/account', data={
                'preferences_form': '1',
                'parse_filename_dates': 'on',
                'filename_date_pattern': 'yyyymmdd',
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
            assert resp.status_code == 200, resp.data
            db.session.refresh(user)
            assert user.parse_filename_dates is True
            assert user.filename_date_pattern == 'yyyymmdd'

            # Custom with an invalid regex is rejected and does not save.
            resp = client.post('/account', data={
                'preferences_form': '1',
                'parse_filename_dates': 'on',
                'filename_date_pattern': 'custom',
                'filename_date_regex': '(unclosed',
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
            assert resp.status_code == 400
            db.session.rollback()
            db.session.refresh(user)
            assert user.filename_date_pattern == 'yyyymmdd'

            # Custom with a valid regex saves.
            rx = r'(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})'
            resp = client.post('/account', data={
                'preferences_form': '1',
                'parse_filename_dates': 'on',
                'filename_date_pattern': 'custom',
                'filename_date_regex': rx,
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
            assert resp.status_code == 200
            db.session.refresh(user)
            assert user.filename_date_pattern == 'custom'
            assert user.filename_date_regex == rx

            # A preferences POST WITHOUT the section (e.g. the quick
            # ui-language change) must not clobber the settings.
            resp = client.post('/account', data={
                'preferences_form': '1',
                'ui_language': 'en',
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
            assert resp.status_code == 200
            db.session.refresh(user)
            assert user.parse_filename_dates is True
            assert user.filename_date_pattern == 'custom'

            # Unknown pattern key falls back to auto.
            resp = client.post('/account', data={
                'preferences_form': '1',
                'filename_date_pattern': 'evil-key',
            }, headers={'X-Requested-With': 'XMLHttpRequest'})
            assert resp.status_code == 200
            db.session.refresh(user)
            assert user.filename_date_pattern == 'auto'
        finally:
            db.session.delete(user)
            db.session.commit()
