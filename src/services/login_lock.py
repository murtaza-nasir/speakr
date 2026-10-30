"""Password sign-in lock per account and client address (discussion #529).

After login_max_failed_attempts wrong passwords for one account from one
address, password sign-ins for that account from that address are refused
for login_lockout_minutes. 0 attempts turns the lock off. The counter lives
in the database, so every worker sees the same count.
"""

import ipaddress
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from src.database import db
from src.models import LoginFailure, SystemSetting

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_MINUTES = 15
# Rows that are not locked and have seen no failure for this long are removed.
STALE_AFTER = timedelta(days=1)


def client_key(remote_addr):
    """The address a failure is counted against: IPv4 as is, IPv6 as its /64."""
    raw = (remote_addr or '').split('%', 1)[0].strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return (remote_addr or 'unknown')[:64]
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f'{address}/64', strict=False))
    return str(address)


def lock_settings():
    """(max_attempts, lockout_minutes); max_attempts 0 means no lock."""
    try:
        max_attempts = int(SystemSetting.get_setting('login_max_failed_attempts', DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = DEFAULT_MAX_ATTEMPTS
    try:
        minutes = int(SystemSetting.get_setting('login_lockout_minutes', DEFAULT_LOCKOUT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_LOCKOUT_MINUTES
    if minutes < 1:
        minutes = DEFAULT_LOCKOUT_MINUTES
    return max(max_attempts, 0), minutes


def is_locked(user_id, key, now=None):
    now = now or datetime.utcnow()
    return db.session.query(LoginFailure.id).filter(
        LoginFailure.user_id == user_id,
        LoginFailure.address == key,
        LoginFailure.locked_until.isnot(None),
        LoginFailure.locked_until > now,
    ).first() is not None


def _increment(user_id, key, now):
    return db.session.query(LoginFailure).filter_by(user_id=user_id, address=key).update(
        {LoginFailure.failed_count: LoginFailure.failed_count + 1, LoginFailure.updated_at: now},
        synchronize_session=False)


def record_failure(user_id, key, max_attempts, minutes):
    """Count one wrong password; lock the account for this address at the maximum."""
    now = datetime.utcnow()
    db.session.query(LoginFailure).filter(
        LoginFailure.updated_at < now - STALE_AFTER,
        db.or_(LoginFailure.locked_until.is_(None), LoginFailure.locked_until <= now),
    ).delete(synchronize_session=False)
    if not _increment(user_id, key, now):
        db.session.add(LoginFailure(user_id=user_id, address=key, failed_count=1, updated_at=now))
        try:
            db.session.commit()
        except IntegrityError:
            # Another worker inserted the row first; count on that row.
            db.session.rollback()
            _increment(user_id, key, now)
    db.session.commit()

    locked = db.session.query(LoginFailure).filter(
        LoginFailure.user_id == user_id,
        LoginFailure.address == key,
        LoginFailure.failed_count >= max_attempts,
    ).update({LoginFailure.locked_until: now + timedelta(minutes=minutes),
              LoginFailure.failed_count: 0}, synchronize_session=False)
    db.session.commit()
    if locked:
        logger.warning("Password sign-in locked for user id %s from %s for %d minutes", user_id, key, minutes)


def clear_failures(user_id, key=None):
    """Forget the failures for one address, or for every address when key is None.

    The caller commits."""
    query = db.session.query(LoginFailure).filter(LoginFailure.user_id == user_id)
    if key is not None:
        query = query.filter(LoginFailure.address == key)
    query.delete(synchronize_session=False)


def locked_user_ids(user_ids=None, now=None):
    """Ids of accounts locked from at least one address right now."""
    now = now or datetime.utcnow()
    query = db.session.query(LoginFailure.user_id).filter(
        LoginFailure.locked_until.isnot(None), LoginFailure.locked_until > now)
    if user_ids is not None:
        query = query.filter(LoginFailure.user_id.in_(list(user_ids)))
    return {row[0] for row in query.distinct()}
