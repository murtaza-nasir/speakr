"""Webhook dispatch service (#275).

Two responsibilities:

1. ``emit_webhook_event(user_id, event_type, data, *, app=None)`` — the
   call-site API. Cheap and synchronous: looks up the user's subscribed
   webhooks, creates a ``WebhookDelivery`` row per match, and returns.
   Safe to call from request handlers and from the job_queue worker.
2. ``run_dispatcher_pass(app)`` — the background sweep. Picks up
   ``pending`` deliveries plus ``failed`` deliveries whose
   ``next_retry_at`` has elapsed, POSTs them with the HMAC signature,
   and updates the row. Auto-pauses the parent webhook after N
   consecutive failures.

The dispatcher is launched from ``src/config/startup.py`` as a daemon
thread, matching the pattern used by the recording-session cleanup
sweep.

Security:

- HMAC-SHA256 signature in ``Speakr-Signature`` (``sha256=<hex>``).
- ``Speakr-Delivery-Id`` and ``Speakr-Event`` headers for idempotency
  and cheap routing.
- ``Speakr-Timestamp`` so receivers can reject stale deliveries.
- SSRF guard: outbound URLs are resolved and validated against a
  private-network blocklist. ``allow_http`` only opens the http://
  scheme; private hosts still require the admin allowlist.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Iterable
from urllib.parse import urlparse

import requests

from src.database import db
from src.models import Webhook, WebhookDelivery, WEBHOOK_EVENT_TYPES


logger = logging.getLogger(__name__)


# ---- Config knobs (read fresh each call so tests can patch env) ------------

def _delivery_timeout() -> float:
    try:
        return float(os.environ.get('WEBHOOK_DELIVERY_TIMEOUT_SECONDS', '10'))
    except (TypeError, ValueError):
        return 10.0


def _max_attempts() -> int:
    try:
        return max(1, int(os.environ.get('WEBHOOK_MAX_ATTEMPTS', '5')))
    except (TypeError, ValueError):
        return 5


def _autopause_failures() -> int:
    try:
        return max(1, int(os.environ.get('WEBHOOK_AUTOPAUSE_FAILURES', '10')))
    except (TypeError, ValueError):
        return 10


def _trial_interval_seconds() -> int:
    """How often an auto-paused webhook gets a trial-fire delivery.

    Defaults to one hour: long enough that a flapping endpoint doesn't
    flood the receiver, short enough that a recovered endpoint resumes
    within a working day. Set 0 to disable trial-fires entirely (the
    webhook then stays paused until the user manually re-enables).
    """
    try:
        return max(0, int(os.environ.get('WEBHOOK_TRIAL_INTERVAL_SECONDS', '3600')))
    except (TypeError, ValueError):
        return 3600


def _dispatcher_interval() -> int:
    try:
        return max(1, int(os.environ.get('WEBHOOK_DISPATCHER_INTERVAL_SECONDS', '5')))
    except (TypeError, ValueError):
        return 5


def _max_per_user() -> int:
    try:
        return max(1, int(os.environ.get('WEBHOOK_MAX_PER_USER', '10')))
    except (TypeError, ValueError):
        return 10


def _global_enabled() -> bool:
    return os.environ.get('WEBHOOK_GLOBAL_ENABLED', 'true').lower() != 'false'


def _intranet_host_allowlist():
    """Return a compiled regex matching admin-allowlisted intranet hosts.

    Off by default: the env var is empty so the regex matches nothing,
    meaning private IPs are blocked. Operators who genuinely want to
    POST to internal services set ``WEBHOOK_INTRANET_HOST_ALLOWLIST`` to
    a regex like ``^(home\\.lan|192\\.168\\.1\\.\\d+)$``.
    """
    raw = os.environ.get('WEBHOOK_INTRANET_HOST_ALLOWLIST', '')
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as e:
        logger.warning(f"Invalid WEBHOOK_INTRANET_HOST_ALLOWLIST regex {raw!r}: {e}")
        return None


# Retry delays in seconds for attempts 1, 2, 3, 4, 5+.
_RETRY_DELAYS = [0, 30, 120, 600, 3600]


def _delay_for_attempt(attempt_count: int) -> int:
    """Return the delay (seconds) BEFORE this attempt. attempt_count is
    1-indexed: 1 = first attempt (immediate)."""
    if attempt_count <= 0:
        return 0
    if attempt_count - 1 < len(_RETRY_DELAYS):
        return _RETRY_DELAYS[attempt_count - 1]
    return _RETRY_DELAYS[-1]


# ---- URL safety / SSRF guard ----------------------------------------------

def is_url_safe_for_webhook(url: str, allow_http: bool = False) -> tuple:
    """Validate a webhook URL.

    Returns ``(ok: bool, reason: str)``. ``ok`` is False when the URL
    points at a private/loopback/link-local IP (unless the
    ``WEBHOOK_INTRANET_HOST_ALLOWLIST`` regex matches the hostname), or
    uses an unsupported scheme.
    """
    if not url:
        return False, 'URL is required'
    try:
        parsed = urlparse(url)
    except Exception:
        return False, 'URL could not be parsed'

    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        return False, 'Scheme must be http or https'
    if scheme == 'http' and not allow_http:
        return False, 'http:// URLs require allow_http=true'

    host = parsed.hostname
    if not host:
        return False, 'URL is missing a host'

    # Always resolve and inspect every address the hostname maps to,
    # THEN decide whether the allowlist permits a private/loopback IP.
    # Old behaviour returned early on an allowlist hostname match,
    # which (a) skipped DNS resolution entirely and (b) let a careless
    # regex like `^localhost` match `localhost.evil.com`. Now the
    # allowlist only relaxes the private-IP check; the resolution runs
    # unconditionally, and the allowlist is matched against the lower-
    # cased host so case quirks don't help an attacker.
    try:
        addrinfos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f'DNS resolution failed for {host!r}'

    allowlist = _intranet_host_allowlist()
    host_lower = host.lower()
    allowlist_matches = bool(allowlist and allowlist.search(host_lower))

    for _af, _stype, _proto, _name, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        is_private_ip = (
            ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast
            or (ip.version == 6 and ip.is_site_local)
        )
        if is_private_ip and not allowlist_matches:
            return False, (
                f'URL resolves to a private/loopback address ({ip_str}); '
                'set WEBHOOK_INTRANET_HOST_ALLOWLIST to permit this host '
                'explicitly, or use a public URL. The allowlist regex '
                'should be anchored (e.g. r"^(localhost|.*\\.intra)$") '
                'so it cannot be tricked by lookalike hostnames.'
            )

    return True, ''


# ---- Signing & headers -----------------------------------------------------

def sign_payload(secret: str, body: bytes) -> str:
    """Compute the Speakr-Signature header value for ``body``."""
    mac = hmac.new(secret.encode('utf-8'), body, hashlib.sha256)
    return f'sha256={mac.hexdigest()}'


def _build_envelope(event_id: str, event_type: str, user_id: int, data: dict):
    return {
        'id': event_id,
        'type': event_type,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'user_id': user_id,
        'data': data or {},
    }


def serialize_envelope(envelope: dict) -> str:
    """Canonical JSON serialization for webhook payloads.

    Compact form (no whitespace after ``,`` or ``:``) so receivers
    debugging with tools like webhook.site can copy-paste the displayed
    body and compute a matching HMAC without canonicalisation pitfalls.
    The wire format is also what other webhook providers (Stripe,
    GitHub, Mux) use, which means common signature-verification
    libraries Just Work.

    All three webhook payload producers (emit_webhook_event,
    test_fire, replay_delivery) MUST go through this function so the
    bytes signed are always the bytes sent.
    """
    return json.dumps(envelope, separators=(',', ':'), ensure_ascii=False)


# ---- Public entry point: enqueue ------------------------------------------

def emit_webhook_event(user_id: int, event_type: str, data: dict, *, app=None) -> int:
    """Enqueue a webhook delivery for each subscribed endpoint.

    Returns the count of WebhookDelivery rows created (0 if no enabled
    webhooks are subscribed). Safe to call from request handlers; the
    actual HTTP POST happens off-request in the dispatcher loop.
    """
    if not _global_enabled():
        return 0
    if event_type not in WEBHOOK_EVENT_TYPES:
        logger.warning(f"emit_webhook_event called with unknown type {event_type!r}; dropping")
        return 0

    def _do_enqueue():
        # Find enabled, non-paused webhooks for the user that subscribe to this event.
        subscriptions = (
            Webhook.query
            .filter_by(user_id=user_id, enabled=True)
            .all()
        )
        matched = [w for w in subscriptions if event_type in w.event_list]
        if not matched:
            return 0

        event_id = str(uuid.uuid4())
        first_attempt_at = datetime.utcnow()
        created = 0
        for wh in matched:
            envelope = _build_envelope(event_id, event_type, user_id, data)
            delivery = WebhookDelivery(
                webhook_id=wh.id,
                event_id=event_id,
                event_type=event_type,
                payload=serialize_envelope(envelope),
                status='pending',
                next_retry_at=first_attempt_at,
            )
            db.session.add(delivery)
            created += 1
        if created:
            db.session.commit()
        return created

    if app is not None:
        with app.app_context():
            return _do_enqueue()
    return _do_enqueue()


# ---- Dispatcher: one pass over due deliveries -----------------------------

def _post_delivery(delivery: WebhookDelivery, webhook: Webhook) -> tuple:
    """Run one HTTP attempt. Returns (response_status, body_preview, error).

    On network/timeout error, response_status is None and error is set.
    """
    body_bytes = (delivery.payload or '').encode('utf-8')
    signature = sign_payload(webhook.secret, body_bytes)
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': f'Speakr-Webhook/1.0',
        'Speakr-Signature': signature,
        'Speakr-Delivery-Id': delivery.event_id,
        'Speakr-Event': delivery.event_type,
        'Speakr-Timestamp': datetime.utcnow().isoformat() + 'Z',
    }
    try:
        resp = requests.post(
            webhook.url,
            data=body_bytes,
            headers=headers,
            timeout=_delivery_timeout(),
            allow_redirects=False,
        )
        preview = (resp.text or '')[:2000]
        return resp.status_code, preview, None
    except requests.RequestException as e:
        return None, None, str(e)[:500]


def _is_retryable_status(status_code) -> bool:
    if status_code is None:
        return True  # network error
    if 200 <= status_code < 300:
        return False
    if status_code in (408, 429):
        return True
    if 500 <= status_code < 600:
        return True
    # 3xx redirects (we disabled allow_redirects), 4xx others = permanent
    return False


def _apply_attempt_result(delivery: WebhookDelivery, status_code, body_preview, error):
    """Mutate the delivery row based on the HTTP outcome. Returns the
    new status string."""
    delivery.attempt_count = (delivery.attempt_count or 0) + 1
    delivery.response_status = status_code
    delivery.response_body_preview = body_preview
    delivery.error_message = error

    if status_code is not None and 200 <= status_code < 300:
        delivery.status = 'success'
        delivery.delivered_at = datetime.utcnow()
        delivery.next_retry_at = None
        return 'success'

    if not _is_retryable_status(status_code) or delivery.attempt_count >= _max_attempts():
        delivery.status = 'permanent_failure'
        delivery.next_retry_at = None
        return 'permanent_failure'

    delivery.status = 'failed'
    delay = _delay_for_attempt(delivery.attempt_count + 1)
    delivery.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
    return 'failed'


def _due_deliveries(now: datetime, limit: int = 100):
    """Find deliveries that should be attempted now: pending OR failed
    with elapsed next_retry_at."""
    return (
        WebhookDelivery.query
        .filter(
            WebhookDelivery.status.in_(('pending', 'failed')),
            (WebhookDelivery.next_retry_at == None) | (WebhookDelivery.next_retry_at <= now),  # noqa: E711
        )
        .order_by(WebhookDelivery.created_at.asc())
        .limit(limit)
        .all()
    )


def run_dispatcher_pass(app=None, limit: int = 100) -> dict:
    """Run one sweep of the dispatcher. Returns a counters dict.

    Safe to call from tests; for production use, the background thread
    started in startup.py invokes this on a fixed interval.
    """
    if not _global_enabled():
        return {'attempted': 0, 'success': 0, 'failed': 0, 'permanent_failure': 0}

    def _run_one():
        now = datetime.utcnow()
        due = _due_deliveries(now, limit=limit)
        attempted = 0
        outcomes = {'success': 0, 'failed': 0, 'permanent_failure': 0}
        autopause_thresh = _autopause_failures()
        trial_interval_s = _trial_interval_seconds()
        # Track which auto-paused webhooks have already been trialed
        # this pass so we attempt at most one delivery per webhook per
        # interval (the rest of their queue waits for the trial result).
        trialed_this_pass = set()
        for delivery in due:
            webhook = delivery.webhook
            if webhook is None:
                delivery.status = 'permanent_failure'
                delivery.next_retry_at = None
                continue
            if not webhook.enabled:
                if not webhook.auto_paused:
                    # User explicitly disabled; abandon queued rows.
                    delivery.status = 'permanent_failure'
                    delivery.next_retry_at = None
                    continue
                # Auto-paused webhook. Try at most ONE delivery per
                # pass as a trial. Subsequent queued deliveries for the
                # same webhook are pushed past the trial interval so
                # we recheck them after the next trial window.
                if webhook.id in trialed_this_pass:
                    delivery.next_retry_at = now + timedelta(seconds=trial_interval_s)
                    delivery.status = 'failed'
                    continue
                trialed_this_pass.add(webhook.id)
            status_code, body_preview, error = _post_delivery(delivery, webhook)
            outcome = _apply_attempt_result(delivery, status_code, body_preview, error)
            attempted += 1
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

            # Update the parent webhook's health counters.
            webhook.last_delivery_at = datetime.utcnow()
            if outcome == 'success':
                # Successful delivery resets failure counter AND
                # unpauses an auto-paused webhook so its remaining
                # queue can flush on subsequent passes.
                webhook.consecutive_failures = 0
                if webhook.auto_paused:
                    webhook.enabled = True
                    webhook.auto_paused = False
            else:
                if outcome == 'permanent_failure':
                    webhook.consecutive_failures = (webhook.consecutive_failures or 0) + 1
                    if webhook.consecutive_failures >= autopause_thresh and not webhook.auto_paused:
                        # First time crossing the threshold: pause and
                        # schedule the next trial 1h out (configurable
                        # via WEBHOOK_TRIAL_INTERVAL_SECONDS).
                        webhook.enabled = False
                        webhook.auto_paused = True
                        delivery.status = 'failed'
                        delivery.next_retry_at = now + timedelta(seconds=trial_interval_s)
                elif webhook.auto_paused:
                    # Failed trial of an already-paused webhook: push
                    # next trial forward by the interval.
                    delivery.next_retry_at = now + timedelta(seconds=trial_interval_s)

        if attempted or trialed_this_pass:
            db.session.commit()
        return {'attempted': attempted, **outcomes}

    if app is not None:
        with app.app_context():
            return _run_one()
    return _run_one()


# ---- Background dispatcher thread -----------------------------------------

_dispatcher_thread_started = False
_dispatcher_thread_lock = threading.Lock()


def _recover_stuck_pending_deliveries(app, stale_after_seconds: int = 300):
    """On dispatcher startup, reset deliveries that look orphaned.

    If the dispatcher died between fetching a delivery and committing
    its outcome, the row stays ``status='pending'`` with attempts > 0
    and ``next_retry_at`` either None or in the past. Without
    intervention the next dispatcher pass would already pick those up
    (status='pending' qualifies), but the ``created_at`` ordering means
    if the worker died on a specific row mid-update, the stale
    in-memory state can leave the row neither picked up nor cleaned.
    Scan once at startup and forcibly schedule them for immediate retry.
    """
    with app.app_context():
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
            stuck = WebhookDelivery.query.filter(
                WebhookDelivery.status == 'pending',
                WebhookDelivery.attempt_count > 0,
                WebhookDelivery.created_at < cutoff,
            ).all()
            if not stuck:
                return 0
            now = datetime.utcnow()
            for d in stuck:
                d.next_retry_at = now
            db.session.commit()
            app.logger.info(
                f"Webhook dispatcher: reset {len(stuck)} stuck pending deliveries at startup"
            )
            return len(stuck)
        except Exception as e:
            app.logger.warning(f"Webhook stuck-delivery recovery failed: {e}", exc_info=True)
            return 0


def start_dispatcher_thread(app):
    """Spawn the daemon dispatcher thread (idempotent)."""
    global _dispatcher_thread_started
    with _dispatcher_thread_lock:
        if _dispatcher_thread_started:
            return

        # Recover any deliveries left in a half-processed pending state
        # by a previous dispatcher that died mid-pass.
        _recover_stuck_pending_deliveries(app)

        interval = _dispatcher_interval()

        def _loop():
            app.logger.info(f"Webhook dispatcher started (interval={interval}s)")
            while True:
                try:
                    time.sleep(interval)
                    if not _global_enabled():
                        continue
                    counters = run_dispatcher_pass(app=app)
                    if counters.get('attempted'):
                        app.logger.info(
                            f"Webhook dispatcher pass: attempted={counters['attempted']} "
                            f"success={counters.get('success', 0)} "
                            f"failed={counters.get('failed', 0)} "
                            f"permanent_failure={counters.get('permanent_failure', 0)}"
                        )
                except Exception as e:
                    app.logger.error(f"Webhook dispatcher error: {e}", exc_info=True)
                    time.sleep(min(60, interval * 2))

        t = threading.Thread(target=_loop, daemon=True, name="WebhookDispatcher")
        t.start()
        _dispatcher_thread_started = True
        app.logger.info("✅ Webhook dispatcher thread initialized")
