"""
Push Notification API Endpoints
Handles push notification subscriptions and delivery
"""
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from src.database import db
from src.models.push_subscription import PushSubscription
from src.utils.localization import api_error, translate
from src.utils.request_body import json_object_body


push_bp = Blueprint('push', __name__)

# VAPID config is loaded lazily to avoid startup issues
_vapid_config = None


def _get_vapid_config():
    """Load VAPID configuration lazily"""
    global _vapid_config
    if _vapid_config is None:
        try:
            from src.utils.vapid_keys import VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_ENABLED
            _vapid_config = {
                'enabled': VAPID_ENABLED,
                'public_key': VAPID_PUBLIC_KEY,
                'private_key': VAPID_PRIVATE_KEY
            }
        except Exception as e:
            print(f"[Push] Failed to load VAPID config: {e}")
            _vapid_config = {
                'enabled': False,
                'public_key': None,
                'private_key': None
            }
    return _vapid_config


@push_bp.route('/api/push/config', methods=['GET'])
def get_push_config():
    """Get push notification configuration for client"""
    config = _get_vapid_config()
    return jsonify({
        'enabled': config['enabled'],
        'public_key': config['public_key'] if config['enabled'] else None
    })


@push_bp.route('/api/push/subscribe', methods=['POST'])
@login_required
def subscribe():
    """Store push subscription for current user"""
    config = _get_vapid_config()
    if not config['enabled']:
        return jsonify({
            'success': False,
            **api_error('push_unavailable')
        }), 503

    try:
        subscription_data, body_error = json_object_body()
        if body_error:
            return body_error

        if not subscription_data or 'endpoint' not in subscription_data:
            return jsonify({
                'success': False,
                **api_error('invalid_subscription_data')
            }), 400

        # Check if subscription already exists
        existing = PushSubscription.query.filter_by(
            user_id=current_user.id,
            endpoint=subscription_data['endpoint']
        ).first()

        if existing:
            return jsonify({
                'success': True,
                'message': translate('server.apiMessage.alreadySubscribed'),
                'subscription_id': existing.id
            })

        # Create new subscription
        subscription = PushSubscription(
            user_id=current_user.id,
            endpoint=subscription_data['endpoint'],
            p256dh_key=subscription_data.get('keys', {}).get('p256dh', ''),
            auth_key=subscription_data.get('keys', {}).get('auth', '')
        )

        db.session.add(subscription)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.subscriptionSaved'),
            'subscription_id': subscription.id
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[Push] Subscription error: {e}", exc_info=True)
        return jsonify({
            'success': False,
            **api_error('unexpected_error')
        }), 500


@push_bp.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def unsubscribe():
    """Remove push subscription for current user"""
    config = _get_vapid_config()
    if not config['enabled']:
        return jsonify({'success': True, 'message': translate('server.apiMessage.pushNotificationsNotEnabled')})

    try:
        subscription_data, body_error = json_object_body()
        if body_error:
            return body_error

        if not subscription_data or 'endpoint' not in subscription_data:
            return jsonify({
                'success': False,
                **api_error('invalid_subscription_data')
            }), 400

        subscription = PushSubscription.query.filter_by(
            user_id=current_user.id,
            endpoint=subscription_data['endpoint']
        ).first()

        if subscription:
            db.session.delete(subscription)
            db.session.commit()
            return jsonify({
                'success': True,
                'message': translate('server.apiMessage.subscriptionRemoved')
            })

        return jsonify({
            'success': False,
            **api_error('subscription_not_found')
        }), 404

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[Push] Unsubscribe error: {e}", exc_info=True)
        return jsonify({
            'success': False,
            **api_error('unexpected_error')
        }), 500
