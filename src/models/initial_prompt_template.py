"""
InitialPromptTemplate model for user-defined transcription initial prompts.

These are reusable, plain-text ASR "initial prompt" hints (the context line
sent to the transcription engine to steer recognition). They are distinct from
summarization prompts and carry no {{variable}} substitution — they are plain
text, picked at upload time or used to fill tag/folder/account defaults.
"""

from datetime import datetime
from src.database import db


class InitialPromptTemplate(db.Model):
    """Stores user-defined, reusable transcription initial-prompt texts."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    template = db.Column(db.Text, nullable=False)
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
            'description': self.description,
            'is_default': self.is_default,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
