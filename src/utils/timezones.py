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


def local_day_start_utc(day, zone_name):
    """Midnight at the start of a local calendar day, as naive UTC (for queries)."""
    from datetime import time as _time
    start = datetime.combine(day, _time.min).replace(tzinfo=ZoneInfo(zone_name))
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def local_date_bounds(date_from, date_to, zone_name):
    """UTC bounds for an inclusive range of local calendar days.

    Returns (start, end) as naive UTC: start of date_from, and the start of the
    day after date_to (exclusive). Either may be None. Comparing a stored UTC
    datetime with a bare date dropped the last day and used UTC days (#412).
    """
    from datetime import timedelta
    start = local_day_start_utc(date_from, zone_name) if date_from else None
    end = local_day_start_utc(date_to + timedelta(days=1), zone_name) if date_to else None
    return start, end


def user_timezone_by_id(user_id):
    """user_timezone for a user id (the admin default when the user is unknown)."""
    from src.database import db
    from src.models import User
    return user_timezone(db.session.get(User, user_id) if user_id else None)


def request_timezone(value, user):
    """The zone for an interactive request: the browser's if valid, else the user's."""
    return value if is_valid_timezone(value) else user_timezone(user)


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
