"""In-app notifications.

Speakr already had a *delivery* channel in `push_subscription.py` (web push to
a browser) but nowhere to keep a notice, so nothing could be listed, counted,
or marked read. This is the store. Push stays a channel it can fan out to.

Three decisions are worth stating, because each one is cheap now and expensive
to retrofit.

**The text is not stored.** A `message_key` plus a JSON `params` blob is,
and the frontend renders it with the same `t()` every other string goes
through. Storing English sentences would make notifications the one part of
the interface that ignores the seven locales.

**Notices are deduplicated by `dedupe_key`.** The voice-embedding check runs
at startup, and startup happens on every restart and deploy, so an insert per
run would pile up identical rows. Unique per (user, key), so re-raising an
existing condition touches the existing row instead.

**A notice can be a condition, not only an event.** Most are events: something
happened, the user dismisses it. But "your embedding model changed" describes a
state that ends when the backend is restored, so it has to be resolvable by the
producer rather than only dismissable by the reader. `resolved_at` is what
separates the two.

Rows are one per recipient. The alternative, one row plus a read-state join
table, saves rows and costs a join on every unread count, every list and every
dismissal. Fan-out is what GitHub and Discourse do and it keeps per-user state
trivial.
"""

from datetime import datetime

from src.database import db


# Severity drives presentation only; nothing branches on it server-side.
NOTIFICATION_LEVELS = ('info', 'warning', 'error')

# Stable machine names. The value is written into rows, so renaming one orphans
# every existing notice of that kind; add a new name instead.
KIND_VOICE_EMBEDDING_CHANGED = 'voice_embedding.changed'


class Notification(db.Model):
    """One notice for one recipient."""

    __tablename__ = 'notification'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'),
                        nullable=False, index=True)

    # What kind of thing this is, for filtering and for the producer to find
    # its own notices again.
    kind = db.Column(db.String(80), nullable=False, index=True)
    level = db.Column(db.String(20), nullable=False, default='info')

    # Localized at render time. `params` is interpolated into the string.
    message_key = db.Column(db.String(200), nullable=False)
    params = db.Column(db.JSON, nullable=True)

    # Optional in-app destination, e.g. '/admin'. Relative on purpose: an
    # absolute URL in a notification is a phishing shape.
    link = db.Column(db.String(500), nullable=True)

    # Idempotency. Unique per user so re-raising the same condition updates the
    # existing row rather than adding another.
    dedupe_key = db.Column(db.String(200), nullable=False, index=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    # Read: the user has seen it. Dismissed: the user cleared it away.
    # Resolved: the condition it described has ended, cleared by the producer
    # and not by the reader.
    read_at = db.Column(db.DateTime, nullable=True)
    dismissed_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True, index=True)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'dedupe_key', name='uq_notification_user_dedupe'),
    )

    user = db.relationship('User', backref=db.backref(
        'notifications', lazy='dynamic', cascade='all, delete-orphan'))

    @property
    def is_active(self):
        """Still worth showing: neither cleared by the user nor ended."""
        return self.dismissed_at is None and self.resolved_at is None

    def to_dict(self):
        return {
            'id': self.id,
            'kind': self.kind,
            'level': self.level,
            'message_key': self.message_key,
            'params': self.params or {},
            'link': self.link,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'read': self.read_at is not None,
            'resolved': self.resolved_at is not None,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<Notification {self.id} {self.kind} user={self.user_id}>'
