"""Raising, resolving and reading in-app notifications.

Callers go through this module rather than touching the model, so that
deduplication and the admin fan-out are decided in one place. See
`src/models/notification.py` for why notices store an i18n key rather than
text, and why some of them are conditions rather than events.
"""

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from src.database import db
from src.models import Notification, User

logger = logging.getLogger(__name__)


def admin_user_ids():
    """Everyone who should see instance-level notices."""
    return [u.id for u in User.query.filter_by(is_admin=True).all()]


def notify(kind, message_key, *, user_ids=None, admins=False, level='info',
           params=None, link=None, dedupe_key=None):
    """Raise a notice for one or more users, without duplicating it.

    `dedupe_key` defaults to `kind`, which is what makes a notice describing a
    single instance-wide condition safe to raise on every startup: the second
    raise updates the existing row instead of adding one.

    Returns the number of rows created or refreshed. Never raises: a
    notification that cannot be stored must not take down whatever produced it.
    """
    if admins:
        user_ids = list(set((user_ids or []) + admin_user_ids()))
    if not user_ids:
        return 0

    # The link is rendered as an href. Only a same-origin path is accepted,
    # so a future producer cannot put a javascript: or off-site URL into
    # the one place users are told to click. Today's single producer is
    # server-internal; this is for the next one.
    if link is not None and not (link.startswith('/') and not link.startswith('//')):
        logger.warning("Dropping non-relative notification link %r for %s", link, kind)
        link = None

    key = dedupe_key or kind
    now = datetime.utcnow()
    touched = 0

    for user_id in user_ids:
        try:
            existing = Notification.query.filter_by(
                user_id=user_id, dedupe_key=key).first()

            if existing is not None:
                # Re-raising a condition that was resolved, or that the user
                # dismissed, brings it back: the situation is live again and
                # silence would be wrong. `read_at` is cleared for the same
                # reason, so it counts as unread once more.
                was_inactive = not existing.is_active
                existing.level = level
                existing.message_key = message_key
                existing.params = params or {}
                existing.link = link
                existing.resolved_at = None
                existing.dismissed_at = None
                if was_inactive:
                    existing.read_at = None
                existing.updated_at = now
            else:
                db.session.add(Notification(
                    user_id=user_id, kind=kind, level=level,
                    message_key=message_key, params=params or {},
                    link=link, dedupe_key=key, created_at=now, updated_at=now))
            touched += 1
        except Exception as e:
            logger.warning("Could not raise notification %s for user %s: %s", kind, user_id, e)

    try:
        db.session.commit()
    except IntegrityError:
        # Two processes raced the same dedupe key. The row exists either way,
        # which is the outcome we wanted.
        db.session.rollback()
        logger.debug("Notification %s already raised concurrently", key)
        return 0
    except Exception as e:
        db.session.rollback()
        logger.warning("Could not commit notifications for %s: %s", kind, e)
        return 0

    return touched


def resolve(dedupe_key, user_ids=None):
    """Mark a condition as over.

    The counterpart to raising. Called by whatever detected the condition, not
    by the reader, so a resolved notice disappears without anyone clicking it.
    """
    query = Notification.query.filter(
        Notification.dedupe_key == dedupe_key,
        Notification.resolved_at.is_(None))
    if user_ids:
        query = query.filter(Notification.user_id.in_(user_ids))

    now = datetime.utcnow()
    count = 0
    for notice in query.all():
        notice.resolved_at = now
        notice.updated_at = now
        count += 1

    if count:
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning("Could not resolve notifications %s: %s", dedupe_key, e)
            return 0
    return count


def active_for(user, limit=50):
    """Notices still worth showing this user, newest first."""
    return (Notification.query
            .filter(Notification.user_id == user.id,
                    Notification.dismissed_at.is_(None),
                    Notification.resolved_at.is_(None))
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .all())


def unread_count(user):
    """What the badge shows. Deliberately a count, not a list."""
    return (Notification.query
            .filter(Notification.user_id == user.id,
                    Notification.read_at.is_(None),
                    Notification.dismissed_at.is_(None),
                    Notification.resolved_at.is_(None))
            .count())


def mark_read(user, notification_ids=None):
    """Mark some or all of a user's notices as seen."""
    query = Notification.query.filter(
        Notification.user_id == user.id,
        Notification.read_at.is_(None))
    if notification_ids:
        query = query.filter(Notification.id.in_(notification_ids))

    now = datetime.utcnow()
    count = 0
    for notice in query.all():
        notice.read_at = now
        count += 1
    if count:
        db.session.commit()
    return count


def dismiss(user, notification_id):
    """Clear one notice away. Scoped to the owner, so an id from elsewhere
    cannot be used to dismiss somebody else's."""
    notice = Notification.query.filter_by(
        id=notification_id, user_id=user.id).first()
    if notice is None:
        return False
    notice.dismissed_at = datetime.utcnow()
    if notice.read_at is None:
        notice.read_at = notice.dismissed_at
    db.session.commit()
    return True
