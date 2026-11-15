"""The time of an account's last use, for the "Last active" column of the
admin user list.

One value per account is kept, ``user.last_seen_at``: the time of the most
recent request that counted as use. No history is kept, and nothing about the
request itself (route, address, client) is stored.

What counts
    A request of a signed-in account, from a browser session or with an API
    token, to any route that is not listed in ``BACKGROUND_ENDPOINTS``. The
    routes listed there are requested by an open tab on a timer, without any
    action of the user, so they say nothing about use. Requests without an
    account (anonymous, public routes, an invalid token) never count.

How it is written
    At most once per ``WRITE_INTERVAL`` per account. The decision is taken in
    memory from the user row that was loaded for the request, so most requests
    issue no statement. A due write is one conditional UPDATE on a connection
    of its own, outside the session of the request:

    * the request's transaction is never committed or rolled back by it;
    * the connection waits at most ``LOCK_WAIT_SECONDS`` for a lock. On
      SQLite the application's own connections wait up to 30 seconds for the
      single writer. Such a wait is out of proportion for a last-active
      time, so on a busy database the write is skipped;
    * every error is swallowed. A failed write is tried again after
      ``RETRY_INTERVAL_SECONDS`` at the earliest, so a database that stays
      locked costs one short wait per account and minute, not one per request.
"""

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import create_engine, or_, text, update
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.pool import NullPool

from src.database import db
from src.models import User

logger = logging.getLogger(__name__)

# The shortest interval between two writes for the same account.
WRITE_INTERVAL = timedelta(minutes=5)
# How long the write may wait for a database lock before it is skipped.
LOCK_WAIT_SECONDS = 0.25
# After a failed write, the next attempt for the same account in this process
# is made this much later at the earliest, so a failing write is not repeated
# on every request.
RETRY_INTERVAL_SECONDS = 60

# Routes that an open tab requests on a timer. A request to one of them is not
# counted as use. The list is explicit on purpose: tests/test_admin_last_active.py
# checks that every name is a registered endpoint and that the addresses polled
# by the front end resolve to names in this list.
BACKGROUND_ENDPOINTS = frozenset({
    # Files of the interface itself, also fetched by the service worker.
    'static',
    # Notification badge, every 60 seconds on every signed-in page
    # (static/js/modules/composables/notifications.js).
    'notifications.notification_count',
    # Job queue, every 5 seconds while something is processing
    # (static/js/app.modular.js).
    'recordings.get_job_queue_status',
    # Status of one recording or of several, every few seconds while it is
    # processing (reprocess.js, summary-follow-up.js).
    'recordings.get_recording_status_only',
    'recordings.get_status',
    'recordings.get_batch_recording_status',
    # CSRF token, every 45 minutes and after a rejected token
    # (static/js/csrf-refresh.js).
    'system.get_csrf_token',
})

_engines = {}
# user id -> time.monotonic() before which no further attempt is made
_retry_after = {}


def counts_as_activity(endpoint):
    """True when a request to this endpoint is use of the application.

    ``endpoint`` is ``request.endpoint``; None (no route matched) never counts.
    """
    return bool(endpoint) and endpoint not in BACKGROUND_ENDPOINTS


def _write_engine():
    """A separate engine for the one UPDATE, with no pooled connections.

    The connection is opened for the write and closed after it, so nothing is
    held between writes and a forked worker inherits nothing.
    """
    main = db.engine
    engine = _engines.get(main)
    if engine is None:
        connect_args = {}
        if main.dialect.name == 'sqlite':
            # The busy timeout of this connection only.
            connect_args['timeout'] = LOCK_WAIT_SECONDS
        engine = create_engine(main.url, poolclass=NullPool, connect_args=connect_args)
        _engines[main] = engine
    return engine


def _write(user_id, now, cutoff):
    engine = _write_engine()
    table = User.__table__
    with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            # Bounds the wait for a row lock held by another transaction.
            wait_ms = int(LOCK_WAIT_SECONDS * 1000)
            conn.execute(text(f"SET LOCAL lock_timeout = '{wait_ms}ms'"))
        # Conditional, so that two workers that decide to write at the same
        # moment cannot move the time backwards.
        result = conn.execute(
            update(table)
            .where(table.c.id == user_id)
            .where(or_(table.c.last_seen_at.is_(None), table.c.last_seen_at < cutoff))
            .values(last_seen_at=now)
        )
        return result.rowcount > 0


def record_activity(user):
    """Store the time of this use of the account, when a write is due.

    Never raises and never touches the session of the request. Returns True
    when the time was written.
    """
    try:
        if user is None or not getattr(user, 'is_authenticated', False):
            return False
        user_id = user.id
        now = datetime.utcnow()
        cutoff = now - WRITE_INTERVAL
        # The row was loaded for this request; a recent value means no write.
        seen = getattr(user, 'last_seen_at', None)
        if seen is not None and seen >= cutoff:
            return False
        retry_after = _retry_after.get(user_id)
        if retry_after is not None:
            if time.monotonic() < retry_after:
                return False
            del _retry_after[user_id]
        if not _write(user_id, now, cutoff):
            return False
        # The row loaded for this request now shows the stored time (the user
        # list of an admin includes the admin). The session is not made dirty.
        loaded = user._get_current_object() if hasattr(user, '_get_current_object') else user
        set_committed_value(loaded, 'last_seen_at', now)
        return True
    except Exception as exc:
        user_id = getattr(user, 'id', None)
        if user_id is not None:
            _retry_after[user_id] = time.monotonic() + RETRY_INTERVAL_SECONDS
        # The kind of error only: a locked database is expected now and then.
        logger.warning('Last-active time of user %s was not stored (%s)',
                       user_id, type(exc).__name__)
        return False
