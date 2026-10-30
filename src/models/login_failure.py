"""
Failed password sign-ins per account and client address (discussion #529).

A row counts wrong passwords for one account from one client address. When
the count reaches the configured maximum, password sign-ins for that account
from that address are refused until locked_until. Other addresses are not
affected, so nobody can lock an account for everyone by guessing on purpose.
"""
from datetime import datetime
from src.database import db


class LoginFailure(db.Model):
    __tablename__ = 'login_failure'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'address', name='uq_login_failure_user_address'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    # The client address, or its /64 network for IPv6
    address = db.Column(db.String(64), nullable=False)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('login_failures', lazy=True, cascade='all, delete-orphan'))

    def __repr__(self):
        return f'<LoginFailure user={self.user_id} count={self.failed_count}>'
