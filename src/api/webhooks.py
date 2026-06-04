"""User-facing webhook CRUD endpoints (#275).

Lives under ``/api/v1/webhooks`` and follows the same conventions as the
rest of the v1 API: login_required, JSON in/out, OpenAPI-documented.

The endpoints are exposed at ``/api/v1/webhooks*`` through registration
in ``api_v1.py`` (which is CSRF-exempt at the blueprint level), so the
CRUD operations also work for API-token authenticated clients.
"""

import os
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from src.database import db
from src.models import Webhook, WebhookDelivery, WEBHOOK_EVENT_TYPES, generate_webhook_secret
from src.services.webhook_dispatch import (
    is_url_safe_for_webhook,
    emit_webhook_event,
)


webhooks_bp = Blueprint('webhooks', __name__, url_prefix='/api/v1/webhooks')


def _max_per_user() -> int:
    try:
        return max(1, int(os.environ.get('WEBHOOK_MAX_PER_USER', '10')))
    except (TypeError, ValueError):
        return 10


def _load_owned(webhook_id: int):
    """Return the webhook if the current user owns it, else (None, 404 tuple)."""
    wh = db.session.get(Webhook, webhook_id)
    if wh is None or wh.user_id != current_user.id:
        return None, (jsonify({'error': 'Webhook not found'}), 404)
    return wh, None


# ---- Collection ------------------------------------------------------------

@webhooks_bp.route('', methods=['GET'])
@login_required
def list_webhooks():
    """List the caller's webhooks."""
    rows = (
        Webhook.query
        .filter_by(user_id=current_user.id)
        .order_by(Webhook.created_at.desc())
        .all()
    )
    return jsonify({
        'webhooks': [w.to_dict() for w in rows],
        'event_types': list(WEBHOOK_EVENT_TYPES),
        'max_per_user': _max_per_user(),
    })


@webhooks_bp.route('', methods=['POST'])
@login_required
def create_webhook():
    """Create a webhook.

    Body: ``{name, url, events, allow_http=false, enabled=true}``.
    The newly-minted HMAC secret is returned **once** in this response;
    after that it is never exposed.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '').strip()
    events = data.get('events') or []
    allow_http = bool(data.get('allow_http', False))
    enabled = bool(data.get('enabled', True))

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if not url:
        return jsonify({'error': 'url is required'}), 400
    if not isinstance(events, list) or not events:
        return jsonify({'error': 'events must be a non-empty list'}), 400

    unknown = [e for e in events if e not in WEBHOOK_EVENT_TYPES]
    if unknown:
        return jsonify({
            'error': f'unknown event types: {unknown}',
            'allowed': list(WEBHOOK_EVENT_TYPES),
        }), 400

    ok, reason = is_url_safe_for_webhook(url, allow_http=allow_http)
    if not ok:
        return jsonify({'error': reason}), 400

    existing = Webhook.query.filter_by(user_id=current_user.id).count()
    if existing >= _max_per_user():
        return jsonify({
            'error': f'Per-user webhook cap reached ({existing}/{_max_per_user()})',
            'max_per_user': _max_per_user(),
        }), 409

    wh = Webhook(
        user_id=current_user.id,
        name=name[:100],
        url=url[:500],
        allow_http=allow_http,
        enabled=enabled,
        secret=generate_webhook_secret(),
    )
    try:
        wh.event_list = events
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db.session.add(wh)
    db.session.commit()
    # Include the secret in the create response so the user can copy it
    # to their receiver. Subsequent reads omit it.
    return jsonify(wh.to_dict(include_secret=True)), 201


# ---- Single resource -------------------------------------------------------

@webhooks_bp.route('/<int:webhook_id>', methods=['GET'])
@login_required
def get_webhook(webhook_id):
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    return jsonify(wh.to_dict())


@webhooks_bp.route('/<int:webhook_id>', methods=['PATCH'])
@login_required
def update_webhook(webhook_id):
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}

    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400
        wh.name = name[:100]
    if 'url' in data or 'allow_http' in data:
        new_url = (data.get('url') or wh.url).strip()
        new_allow_http = bool(data.get('allow_http', wh.allow_http))
        ok, reason = is_url_safe_for_webhook(new_url, allow_http=new_allow_http)
        if not ok:
            return jsonify({'error': reason}), 400
        wh.url = new_url[:500]
        wh.allow_http = new_allow_http
    if 'events' in data:
        events = data.get('events') or []
        if not isinstance(events, list) or not events:
            return jsonify({'error': 'events must be a non-empty list'}), 400
        try:
            wh.event_list = events
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
    if 'enabled' in data:
        enabled = bool(data.get('enabled'))
        wh.enabled = enabled
        if enabled:
            wh.auto_paused = False  # Manual re-enable resets auto-pause flag

    db.session.commit()
    return jsonify(wh.to_dict())


@webhooks_bp.route('/<int:webhook_id>', methods=['DELETE'])
@login_required
def delete_webhook(webhook_id):
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    db.session.delete(wh)
    db.session.commit()
    return ('', 204)


# ---- Secret rotation -------------------------------------------------------

@webhooks_bp.route('/<int:webhook_id>/rotate-secret', methods=['POST'])
@login_required
def rotate_secret(webhook_id):
    """Generate a fresh HMAC secret and return it once."""
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    wh.secret = generate_webhook_secret()
    db.session.commit()
    return jsonify(wh.to_dict(include_secret=True))


# ---- Test fire -------------------------------------------------------------

@webhooks_bp.route('/<int:webhook_id>/test', methods=['POST'])
@login_required
def test_fire(webhook_id):
    """Enqueue a synthetic ``webhook.test`` delivery against this webhook.

    Unlike ``emit_webhook_event`` which fans out to all subscribed
    webhooks for a user, this one targets a single webhook by id and
    bypasses the subscription check (so the user can verify reachability
    even before subscribing to any production events).
    """
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    if not wh.enabled:
        return jsonify({'error': 'Webhook is disabled; enable it first'}), 409

    import uuid
    from src.services.webhook_dispatch import _build_envelope, serialize_envelope
    event_id = str(uuid.uuid4())
    envelope = _build_envelope(event_id, 'webhook.test', current_user.id, {
        'reason': 'manual test from /api/v1/webhooks/{id}/test',
        'webhook_id': wh.id,
    })
    delivery = WebhookDelivery(
        webhook_id=wh.id,
        event_id=event_id,
        event_type='webhook.test',
        payload=serialize_envelope(envelope),
        status='pending',
        next_retry_at=datetime.utcnow(),
    )
    db.session.add(delivery)
    db.session.commit()
    return jsonify(delivery.to_dict()), 202


# ---- Deliveries listing / replay ------------------------------------------

@webhooks_bp.route('/<int:webhook_id>/deliveries', methods=['GET'])
@login_required
def list_deliveries(webhook_id):
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    try:
        limit = min(200, max(1, int(request.args.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    rows = (
        WebhookDelivery.query
        .filter_by(webhook_id=wh.id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({
        'deliveries': [d.to_dict() for d in rows],
        'limit': limit,
    })


@webhooks_bp.route('/<int:webhook_id>/deliveries/<int:delivery_id>', methods=['GET'])
@login_required
def get_delivery(webhook_id, delivery_id):
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    d = db.session.get(WebhookDelivery, delivery_id)
    if not d or d.webhook_id != wh.id:
        return jsonify({'error': 'Delivery not found'}), 404
    payload = d.to_dict()
    payload['payload'] = d.payload  # full body, for debugging
    return jsonify(payload)


@webhooks_bp.route('/<int:webhook_id>/deliveries/<int:delivery_id>/replay', methods=['POST'])
@login_required
def replay_delivery(webhook_id, delivery_id):
    """Re-enqueue the delivery as a brand-new attempt with the same payload."""
    wh, err = _load_owned(webhook_id)
    if err:
        return err
    src = db.session.get(WebhookDelivery, delivery_id)
    if not src or src.webhook_id != wh.id:
        return jsonify({'error': 'Delivery not found'}), 404

    import uuid
    import json as _json
    from src.services.webhook_dispatch import serialize_envelope
    new_event_id = str(uuid.uuid4())
    # Update the envelope's id field so the receiver sees a fresh delivery id.
    try:
        envelope = _json.loads(src.payload)
        envelope['id'] = new_event_id
        envelope['timestamp'] = datetime.utcnow().isoformat() + 'Z'
        envelope['replayed_from'] = src.event_id
        new_payload = serialize_envelope(envelope)
    except Exception:
        new_payload = src.payload

    replay = WebhookDelivery(
        webhook_id=wh.id,
        event_id=new_event_id,
        event_type=src.event_type,
        payload=new_payload,
        status='pending',
        next_retry_at=datetime.utcnow(),
    )
    db.session.add(replay)
    db.session.commit()
    return jsonify(replay.to_dict()), 202
