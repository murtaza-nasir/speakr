"""
Push Subscription Model
Stores web push notification subscriptions for users
"""
from datetime import datetime
from src.database import db


class PushSubscription(db.Model):
    """Web Push notification subscription"""
    __tablename__ = 'push_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Push subscription endpoint (unique per browser/device)
    endpoint = db.Column(db.String(500), nullable=False, unique=True)

    # Encryption keys for sending push messages
    p256dh_key = db.Column(db.String(200), nullable=False)
    auth_key = db.Column(db.String(100), nullable=False)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<PushSubscription {self.id} user={self.user_id}>'

    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'endpoint': self.endpoint,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
