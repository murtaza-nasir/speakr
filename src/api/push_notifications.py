"""
Push Notification API Endpoints
Handles push notification subscriptions and delivery
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from src.database import db
from src.models.push_subscription import PushSubscription
import json


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
            'error': 'Push notifications not available'
        }), 503

    try:
        subscription_data = request.json

        if not subscription_data or 'endpoint' not in subscription_data:
            return jsonify({
                'success': False,
                'error': 'Invalid subscription data'
            }), 400

        # Check if subscription already exists
        existing = PushSubscription.query.filter_by(
            user_id=current_user.id,
            endpoint=subscription_data['endpoint']
        ).first()

        if existing:
            return jsonify({
                'success': True,
                'message': 'Already subscribed',
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
            'message': 'Subscription saved',
            'subscription_id': subscription.id
        })

    except Exception as e:
        db.session.rollback()
        print(f"[Push] Subscription error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@push_bp.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def unsubscribe():
    """Remove push subscription for current user"""
    config = _get_vapid_config()
    if not config['enabled']:
        return jsonify({'success': True, 'message': 'Push notifications not enabled'})

    try:
        subscription_data = request.json

        if not subscription_data or 'endpoint' not in subscription_data:
            return jsonify({
                'success': False,
                'error': 'Invalid subscription data'
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
                'message': 'Subscription removed'
            })

        return jsonify({
            'success': False,
            'error': 'Subscription not found'
        }), 404

    except Exception as e:
        db.session.rollback()
        print(f"[Push] Unsubscribe error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def send_push_notification(user_id, title, body, data=None, url=None):
    """
    Send push notification to all subscriptions for a user

    Args:
        user_id: User ID to send notification to
        title: Notification title
        body: Notification body text
        data: Optional dictionary of extra data
        url: Optional URL to open when notification is clicked
    """
    config = _get_vapid_config()
    if not config['enabled']:
        print("[Push] Push notifications not enabled, skipping")
        return

    try:
        from pywebpush import webpush, WebPushException

        subscriptions = PushSubscription.query.filter_by(user_id=user_id).all()

        if not subscriptions:
            print(f"[Push] No subscriptions found for user {user_id}")
            return

        notification_data = {
            'title': title,
            'body': body,
            'icon': '/static/img/icon-192x192.png',
            'badge': '/static/img/icon-192x192.png',
            'data': data or {}
        }

        if url:
            notification_data['data']['url'] = url

        sent_count = 0
        failed_count = 0

        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info={
                        'endpoint': subscription.endpoint,
                        'keys': {
                            'p256dh': subscription.p256dh_key,
                            'auth': subscription.auth_key
                        }
                    },
                    data=json.dumps(notification_data),
                    vapid_private_key=config['private_key'],
                    vapid_claims={
                        'sub': 'mailto:admin@speakr.app'
                    }
                )
                sent_count += 1
                print(f'[Push] Sent notification to user {user_id} subscription {subscription.id}')

            except WebPushException as e:
                failed_count += 1
                print(f'[Push] Failed to send to subscription {subscription.id}: {e}')

                # Remove expired subscriptions
                if e.response and e.response.status_code in [404, 410]:
                    print(f'[Push] Removing expired subscription {subscription.id}')
                    db.session.delete(subscription)

            except Exception as e:
                failed_count += 1
                print(f'[Push] Unexpected error sending to subscription {subscription.id}: {e}')

        # Commit any deletions
        if failed_count > 0:
            db.session.commit()

        print(f'[Push] Sent {sent_count} notifications, {failed_count} failed')

    except ImportError:
        print("[Push] pywebpush not installed, cannot send notifications")
    except Exception as e:
        print(f"[Push] Error sending notifications: {e}")
