"""Timezones for text the server writes (#412).

Datetimes are stored as naive UTC and the browser converts them for display;
nothing here changes that. Text the server generates itself (titles from naming
templates, default titles) is written in the owner's local time:

1. The user's timezone (an IANA name such as "Europe/Istanbul"). In 'auto'
   mode the browser's zone is saved as the user visits; in 'fixed' mode the
   user picked one in Account settings.
2. Otherwise the admin default timezone (System Settings, seeded from the
   legacy TIMEZONE variable when it is set).
3. Otherwise UTC, which is what all generated text used before #412.

The container's own TZ never matters: conversions use zoneinfo explicitly.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SETTING_KEY = 'default_timezone'
MODE_AUTO = 'auto'
MODE_FIXED = 'fixed'


def is_valid_timezone(name):
    """True for a known IANA zone name."""
    if not name or not isinstance(name, str) or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def deployment_timezone():
    """The admin default timezone, or UTC."""
    from src.models import SystemSetting
    name = SystemSetting.get_setting(DEFAULT_SETTING_KEY, 'UTC')
    return name if is_valid_timezone(name) else 'UTC'


def user_timezone(user):
    """The zone name used for a user's generated text."""
    name = getattr(user, 'timezone', None) if user is not None else None
    return name if is_valid_timezone(name) else deployment_timezone()


def to_local(dt, zone_name):
    """A stored naive-UTC datetime as naive wall-clock time in zone_name.

    Aware datetimes are converted from their own zone. None passes through.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(zone_name)).replace(tzinfo=None)


def to_user_local(dt, user):
    """A stored naive-UTC datetime in the user's local wall-clock time."""
    return to_local(dt, user_timezone(user))


def now_local(zone_name):
    """The current wall-clock time in zone_name (naive)."""
    return to_local(datetime.utcnow(), zone_name)


def record_browser_timezone(user, name):
    """Save the browser's zone for a user in auto mode. Returns True if it changed.

    Fixed-mode users keep their chosen zone; invalid names are ignored. The
    caller commits.
    """
    if user is None or not is_valid_timezone(name):
        return False
    if (getattr(user, 'timezone_mode', None) or MODE_AUTO) != MODE_AUTO:
        return False
    if user.timezone == name:
        return False
    user.timezone = name
    return True
