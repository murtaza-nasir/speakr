"""When an account was last active, for the admin user list.

Presence is a side effect of authenticated requests: the request hook in
``src/app.py`` calls :func:`record_activity` for the signed-in user, and the
admin user listing asks :func:`is_online` whether the stored timestamp falls
inside the online window.

Two constants tune the feature:

``ONLINE_WINDOW``
    How recently a request must have happened for the account to count as
    online. The admin table colours the value with it.

``WRITE_THROTTLE``
    The shortest interval between two writes of the same account. Every
    request from an active user inside the interval issues an UPDATE that
    matches no row, which is cheaper than the write and keeps an idle tab from
    writing once per poll. A user who makes no requests issues no statement
    at all.

The value is persisted rather than kept in memory so that several workers and
processes agree on it, and so that it survives a restart. SQLite runs in WAL
mode, where this single-row write does not block readers.
"""

from datetime import datetime, timedelta

from sqlalchemy import or_

from src.database import db
from src.models import User

ONLINE_WINDOW = timedelta(minutes=5)
WRITE_THROTTLE = timedelta(minutes=1)


def is_online(last_seen_at, now=None):
    """True when ``last_seen_at`` is inside the online window. NULL is offline."""
    if last_seen_at is None:
        return False
    now = now or datetime.utcnow()
    return now - last_seen_at < ONLINE_WINDOW


def record_activity(user, now=None):
    """Store the account's last activity, throttled to one write per minute.

    The UPDATE carries its own WHERE clause as well, so two workers that
    decide to write at the same time cannot both move the timestamp backwards.
    A request from an account whose timestamp is still recent returns without
    issuing any statement. Returns True when the timestamp was written.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False

    now = now or datetime.utcnow()
    cutoff = now - WRITE_THROTTLE
    # The user row was just loaded for this request, so a recent value means
    # there is nothing to write and no statement needs to run at all.
    seen = getattr(user, 'last_seen_at', None)
    if seen is not None and seen >= cutoff:
        return False
    updated = (
        User.query
        .filter(User.id == user.id)
        .filter(or_(User.last_seen_at.is_(None), User.last_seen_at < cutoff))
        .update({User.last_seen_at: now}, synchronize_session=False)
    )
    if not updated:
        return False
    db.session.commit()
    return True
