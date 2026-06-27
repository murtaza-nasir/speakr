"""
InitialPromptTemplate model — user-defined, reusable transcription templates.

A template bundles a plain-text ASR "initial prompt" (the context line sent to
the transcription engine, stored in `template`) and optional comma-separated
`hotwords`. Picked at upload time or used to fill tag/folder/account defaults.
Plain text only — no {{variable}} substitution (distinct from summary prompts).
"""

from datetime import datetime
from src.database import db


class InitialPromptTemplate(db.Model):
    """Stores user-defined, reusable transcription templates (prompt + hotwords)."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    template = db.Column(db.Text, nullable=False)  # the initial prompt ('' if hotwords-only)
    hotwords = db.Column(db.Text, nullable=True)    # comma-separated hotwords (optional)
    description = db.Column(db.String(500), nullable=True)
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('initial_prompt_templates', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'template': self.template,
            'hotwords': self.hotwords or '',
            'description': self.description,
            'is_default': self.is_default,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
