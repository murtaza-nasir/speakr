"""
Token usage tracking model for monitoring LLM API consumption.
"""

from datetime import datetime, date
from src.database import db


class TokenUsage(db.Model):
    """Daily token usage aggregates per user per operation type."""
    __tablename__ = 'token_usage'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    operation_type = db.Column(db.String(50), nullable=False)

    # Token counts (from API response.usage)
    prompt_tokens = db.Column(db.Integer, default=0)
    completion_tokens = db.Column(db.Integer, default=0)
    total_tokens = db.Column(db.Integer, default=0)

    # Cost tracking (OpenRouter provides this)
    cost = db.Column(db.Float, default=0.0)

    # Request count for this day/operation
    request_count = db.Column(db.Integer, default=0)

    # Model info
    model_name = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('token_usage', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'date', 'operation_type', name='uq_user_date_op'),
        db.Index('idx_token_user_date', 'user_id', 'date'),
    )

    def __repr__(self):
        return f'<TokenUsage {self.user_id} {self.date} {self.operation_type}: {self.total_tokens}>'
