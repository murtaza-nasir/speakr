# Push Notifications Setup Guide

This guide explains how to complete the push notification setup for Speakr.

## Overview

The client-side push notification infrastructure is now complete. To enable push notifications, you need to:

1. Generate VAPID keys
2. Configure the client with the public key
3. Implement backend endpoints to store subscriptions and send notifications

## Step 1: Generate VAPID Keys

### Method A: Using web-push (Node.js)

```bash
npm install -g web-push
web-push generate-vapid-keys
```

### Method B: Using Python

```bash
pip install pywebpush
```

```python
from pywebpush import vapid_keys

vapid_keys = vapid_keys()
print("Public Key:", vapid_keys['publicKey'])
print("Private Key:", vapid_keys['privateKey'])
```

### Method C: Using pywebpush CLI

```bash
pywebpush generate-vapid-keys
```

**IMPORTANT:** Keep the private key secret! Never commit it to version control.

## Step 2: Configure Client

1. Open `static/js/config/push-config.js`
2. Set `ENABLED: true`
3. Add your VAPID public key to `VAPID_PUBLIC_KEY`
4. Update `CONTACT_INFO` with your admin email or website

```javascript
export const PUSH_CONFIG = {
    ENABLED: true,
    VAPID_PUBLIC_KEY: 'YOUR_PUBLIC_KEY_HERE',
    CONTACT_INFO: 'mailto:admin@yourdomain.com'
};
```

## Step 3: Implement Backend Endpoints

### Required Backend Endpoints

#### 1. Store Push Subscription

**Endpoint:** `POST /api/push/subscribe`

**Purpose:** Save user's push subscription to database

**Request Body:**
```json
{
    "endpoint": "https://fcm.googleapis.com/fcm/send/...",
    "keys": {
        "p256dh": "...",
        "auth": "..."
    }
}
```

**Response:**
```json
{
    "success": true,
    "message": "Subscription saved"
}
```

**Implementation Example (Flask):**

```python
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, PushSubscription

push_bp = Blueprint('push', __name__)

@push_bp.route('/api/push/subscribe', methods=['POST'])
@login_required
def subscribe():
    """Store push subscription for current user"""
    subscription_data = request.json

    # Check if subscription already exists
    existing = PushSubscription.query.filter_by(
        user_id=current_user.id,
        endpoint=subscription_data['endpoint']
    ).first()

    if existing:
        return jsonify({'success': True, 'message': 'Already subscribed'})

    # Create new subscription
    subscription = PushSubscription(
        user_id=current_user.id,
        endpoint=subscription_data['endpoint'],
        p256dh_key=subscription_data['keys']['p256dh'],
        auth_key=subscription_data['keys']['auth']
    )

    db.session.add(subscription)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Subscription saved'})
```

#### 2. Remove Push Subscription

**Endpoint:** `POST /api/push/unsubscribe`

**Purpose:** Remove user's push subscription from database

**Request Body:** Same as subscribe

**Response:**
```json
{
    "success": true,
    "message": "Subscription removed"
}
```

**Implementation Example:**

```python
@push_bp.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def unsubscribe():
    """Remove push subscription for current user"""
    subscription_data = request.json

    subscription = PushSubscription.query.filter_by(
        user_id=current_user.id,
        endpoint=subscription_data['endpoint']
    ).first()

    if subscription:
        db.session.delete(subscription)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Subscription removed'})

    return jsonify({'success': False, 'message': 'Subscription not found'}), 404
```

## Step 4: Database Model

Add a `PushSubscription` model to your database:

```python
from models import db
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.sql import func

class PushSubscription(db.Model):
    __tablename__ = 'push_subscriptions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    endpoint = Column(String(500), nullable=False, unique=True)
    p256dh_key = Column(String(200), nullable=False)
    auth_key = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        db.Index('idx_user_endpoint', 'user_id', 'endpoint'),
    )
```

Create the migration:

```bash
flask db migrate -m "Add push subscriptions table"
flask db upgrade
```

## Step 5: Send Push Notifications

Use the `pywebpush` library to send notifications when transcription is complete:

```python
from pywebpush import webpush, WebPushException
import json
import os

def send_push_notification(user_id, title, body, data=None):
    """Send push notification to all subscriptions for a user"""
    subscriptions = PushSubscription.query.filter_by(user_id=user_id).all()

    vapid_private_key = os.getenv('VAPID_PRIVATE_KEY')
    vapid_contact = os.getenv('VAPID_CONTACT', 'mailto:admin@example.com')

    notification_data = {
        'title': title,
        'body': body,
        'icon': '/static/img/icon-192x192.png',
        'badge': '/static/img/icon-192x192.png',
        'data': data or {}
    }

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
                vapid_private_key=vapid_private_key,
                vapid_claims={'sub': vapid_contact}
            )
            print(f'Push notification sent to user {user_id}')
        except WebPushException as e:
            print(f'Failed to send push to {subscription.endpoint}: {e}')
            # If subscription is expired, remove it
            if e.response and e.response.status_code in [404, 410]:
                db.session.delete(subscription)
                db.session.commit()
```

## Step 6: Integrate with Transcription

Call the push notification function when transcription is complete:

```python
# In your transcription completion handler
def on_transcription_complete(recording_id):
    recording = AudioFile.query.get(recording_id)

    if recording:
        send_push_notification(
            user_id=recording.user_id,
            title='Transcription Complete',
            body=f'"{recording.display_name or recording.filename}" has been transcribed',
            data={
                'recording_id': recording_id,
                'url': f'/recording/{recording_id}'
            }
        )
```

## Step 7: Environment Variables

Add these environment variables to your `.env` file:

```bash
# VAPID keys for push notifications
VAPID_PRIVATE_KEY=your_private_key_here
VAPID_CONTACT=mailto:admin@yourdomain.com
```

## Testing Push Notifications

1. Open the app in a browser
2. Open Developer Tools > Console
3. Run: `await pwaComposable.subscribeToPushNotifications()`
4. Check database to verify subscription was saved
5. Trigger a test notification from the backend
6. Verify notification appears

## Browser Support

| Browser | Desktop | Mobile |
|---------|---------|--------|
| Chrome  | ✅      | ✅     |
| Edge    | ✅      | ✅     |
| Firefox | ✅      | ✅     |
| Safari  | ✅      | ⚠️ iOS 16.4+ |
| Opera   | ✅      | ✅     |

**Note:** iOS Safari requires iOS 16.4+ and the app must be added to the home screen.

## Troubleshooting

### Subscription fails with "NotAllowedError"
- User denied notification permission
- Ask user to enable notifications in browser settings

### Subscription not saving on server
- Check backend endpoint is accessible
- Verify CSRF token is valid
- Check server logs for errors

### Push notifications not received
- Verify VAPID keys match between client and server
- Check subscription is in database
- Test with browser developer tools
- Ensure service worker is registered

## Security Considerations

1. **Never expose private VAPID key** - Keep it on server only
2. **Validate subscriptions** - Ensure they belong to authenticated users
3. **Rate limit subscriptions** - Prevent abuse
4. **Clean up expired subscriptions** - Remove 404/410 responses
5. **Use HTTPS** - Required for push notifications

## Additional Resources

- [Web Push Protocol](https://datatracker.ietf.org/doc/html/rfc8030)
- [VAPID Specification](https://datatracker.ietf.org/doc/html/rfc8292)
- [pywebpush Documentation](https://github.com/web-push-libs/pywebpush)
- [MDN Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API)
