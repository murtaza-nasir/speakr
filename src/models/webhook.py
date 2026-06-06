"""Webhook models for outbound event notifications (#275).

Two tables:

- ``webhook`` — the user-owned endpoint. Each row has a URL, an HMAC
  secret, a subscription list, and the running health counters
  (``consecutive_failures``, ``last_delivery_at``).
- ``webhook_delivery`` — per-attempt audit log. One row per fired event
  per webhook; the row tracks attempts, status code, response preview,
  and the next retry timestamp.

Design choices documented in ``temp/DESIGN_webhooks.md``.
"""

import json
import secrets
from datetime import datetime

from src.database import db


WEBHOOK_EVENT_TYPES = (
    'recording.created',
    'recording.transcription.started',
    'recording.transcription.completed',
    'recording.transcription.failed',
    'recording.summary.completed',
    'recording.summary.failed',
    'recording.events.extracted',
    'recording.updated',
    'recording.deleted',
    'webhook.test',
)


WEBHOOK_DELIVERY_STATUSES = (
    'pending',           # queued, not yet attempted
    'success',           # 2xx received
    'failed',            # last attempt was a retryable error; may be re-tried
    'permanent_failure', # retries exhausted, or non-retryable status
)


def generate_webhook_secret():
    """Return a URL-safe random secret used for HMAC signing.

    256 bits of entropy is overkill for SHA-256 HMAC but keeps the
    output short and avoids any chance of collision pressure if we
    ever want to use the secret as an opaque lookup key elsewhere.
    """
    return secrets.token_urlsafe(32)


class Webhook(db.Model):
    """A user-owned webhook endpoint that receives event notifications."""

    __tablename__ = 'webhook'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('user.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)

    # When true, the endpoint may use http:// (intranet-only). Off by
    # default so an accidental misconfiguration cannot leak event payloads
    # in plain text. The SSRF guard in the dispatcher additionally
    # validates that the destination is not a private IP unless the
    # admin has explicitly allowlisted intranet hosts.
    allow_http = db.Column(db.Boolean, default=False, nullable=False)

    # HMAC signing secret. Treat as a credential: redact in API
    # responses, surface to the user only on creation, allow rotation
    # via a dedicated endpoint.
    secret = db.Column(db.String(120), nullable=False, default=generate_webhook_secret)

    # JSON array of event type strings the endpoint is subscribed to.
    # Validated against WEBHOOK_EVENT_TYPES on write.
    events = db.Column(db.Text, nullable=False, default='[]')

    # Toggleable by the user (and admin). Auto-paused (also flips this
    # to False) when consecutive_failures crosses WEBHOOK_AUTOPAUSE_FAILURES.
    enabled = db.Column(db.Boolean, default=True, nullable=False, index=True)
    auto_paused = db.Column(db.Boolean, default=False, nullable=False)

    consecutive_failures = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_delivery_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        'User',
        backref=db.backref('webhooks', lazy='dynamic', cascade='all, delete-orphan'),
    )
    deliveries = db.relationship(
        'WebhookDelivery',
        back_populates='webhook',
        cascade='all, delete-orphan',
        lazy='dynamic',
    )

    def __repr__(self):  # pragma: no cover
        return f'<Webhook {self.id} user={self.user_id} name={self.name!r} enabled={self.enabled}>'

    @property
    def event_list(self):
        """Decoded subscription list; empty list on missing/invalid JSON."""
        if not self.events:
            return []
        try:
            value = json.loads(self.events)
        except (TypeError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        return [e for e in value if isinstance(e, str)]

    @event_list.setter
    def event_list(self, values):
        if values is None:
            self.events = '[]'
            return
        if not isinstance(values, (list, tuple)):
            raise ValueError('events must be a list')
        deduped = []
        seen = set()
        for v in values:
            if not isinstance(v, str):
                continue
            if v not in WEBHOOK_EVENT_TYPES:
                raise ValueError(f'Unknown webhook event type: {v!r}')
            if v in seen:
                continue
            seen.add(v)
            deduped.append(v)
        self.events = json.dumps(deduped)

    def to_dict(self, include_secret=False):
        """Public API view. ``include_secret`` is True only at creation
        time and on explicit rotation; never include the secret on list
        or get responses."""
        payload = {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'url': self.url,
            'allow_http': bool(self.allow_http),
            'events': self.event_list,
            'enabled': bool(self.enabled),
            'auto_paused': bool(self.auto_paused),
            'consecutive_failures': self.consecutive_failures,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_delivery_at': self.last_delivery_at.isoformat() if self.last_delivery_at else None,
        }
        if include_secret:
            payload['secret'] = self.secret
        return payload


class WebhookDelivery(db.Model):
    """A single delivery attempt (or attempt series) for one fired event."""

    __tablename__ = 'webhook_delivery'

    id = db.Column(db.Integer, primary_key=True)
    webhook_id = db.Column(
        db.Integer,
        db.ForeignKey('webhook.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    # UUID written into the payload `id` field and the
    # Speakr-Delivery-Id header. Receivers use it for idempotency.
    event_id = db.Column(db.String(36), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)

    # Serialised JSON envelope that gets POSTed. Stored verbatim so
    # replay re-uses the original signature payload.
    payload = db.Column(db.Text, nullable=False)

    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False, index=True)

    response_status = db.Column(db.Integer, nullable=True)
    response_body_preview = db.Column(db.String(2000), nullable=True)
    error_message = db.Column(db.String(500), nullable=True)

    next_retry_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    delivered_at = db.Column(db.DateTime, nullable=True)

    webhook = db.relationship('Webhook', back_populates='deliveries')

    # Composite index for the dispatcher's main query: due deliveries
    # are those with status in ('pending', 'failed') AND
    # next_retry_at <= now (or null). The single-column indexes don't
    # help much on a multi-thousand-row delivery table; this composite
    # makes the dispatcher sweep O(log n) instead of a scan.
    __table_args__ = (
        db.Index('idx_delivery_status_retry', 'status', 'next_retry_at'),
    )

    def __repr__(self):  # pragma: no cover
        return f'<WebhookDelivery {self.id} webhook={self.webhook_id} status={self.status}>'

    def to_dict(self):
        return {
            'id': self.id,
            'webhook_id': self.webhook_id,
            'event_id': self.event_id,
            'event_type': self.event_type,
            'attempt_count': self.attempt_count,
            'status': self.status,
            'response_status': self.response_status,
            'response_body_preview': self.response_body_preview,
            'error_message': self.error_message,
            'next_retry_at': self.next_retry_at.isoformat() if self.next_retry_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'delivered_at': self.delivered_at.isoformat() if self.delivered_at else None,
        }
