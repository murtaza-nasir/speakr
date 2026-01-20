"""
NamingTemplate model for user-defined recording title formatting.

This module defines the NamingTemplate model for storing
custom templates for generating recording titles from filenames,
metadata, and AI-generated content.
"""

import json
import re
import os
from datetime import datetime
from src.database import db


class NamingTemplate(db.Model):
    """Stores user-defined templates for recording title generation."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    template = db.Column(db.Text, nullable=False)  # e.g., "{{phone}} - {{date}} {{ai_title}}"
    description = db.Column(db.String(500), nullable=True)
    regex_patterns = db.Column(db.Text, nullable=True)  # JSON: {"phone": "\\d{10}", "caller": "^([^-]+)"}
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('naming_templates', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'name': self.name,
            'template': self.template,
            'description': self.description,
            'regex_patterns': json.loads(self.regex_patterns) if self.regex_patterns else {},
            'is_default': self.is_default,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    def get_regex_patterns(self):
        """Parse and return regex patterns as dictionary."""
        if not self.regex_patterns:
            return {}
        try:
            return json.loads(self.regex_patterns)
        except json.JSONDecodeError:
            return {}

    def needs_ai_title(self):
        """Check if template requires AI-generated title."""
        return '{{ai_title}}' in self.template

    def apply(self, original_filename, meeting_date=None, ai_title=None):
        """
        Apply this template to generate a recording title.

        Args:
            original_filename: The original filename of the recording
            meeting_date: Optional datetime of the recording
            ai_title: Optional AI-generated title

        Returns:
            Generated title string, or None if template produces empty result
        """
        # Start with template
        result = self.template

        # Get filename without extension for {{filename}}
        filename_no_ext = os.path.splitext(original_filename)[0] if original_filename else ''

        # Build built-in variables
        variables = {
            'ai_title': ai_title or '',
            'filename': filename_no_ext,
            'filename_full': original_filename or '',
            'date': meeting_date.strftime('%Y-%m-%d') if meeting_date else '',
            'datetime': meeting_date.strftime('%Y-%m-%d %H:%M') if meeting_date else '',
            'time': meeting_date.strftime('%H:%M') if meeting_date else '',
            'year': meeting_date.strftime('%Y') if meeting_date else '',
            'month': meeting_date.strftime('%m') if meeting_date else '',
            'day': meeting_date.strftime('%d') if meeting_date else '',
        }

        # Extract custom variables from filename using regex patterns
        regex_patterns = self.get_regex_patterns()
        for var_name, pattern in regex_patterns.items():
            try:
                match = re.search(pattern, filename_no_ext)
                if match:
                    # Use first capture group if exists, else full match
                    variables[var_name] = match.group(1) if match.groups() else match.group(0)
                else:
                    variables[var_name] = ''
            except re.error as e:
                # Invalid regex - log and treat as empty
                variables[var_name] = ''

        # Replace all variables in template
        for var_name, value in variables.items():
            result = result.replace('{{' + var_name + '}}', value)

        # Clean up result
        result = result.strip()

        # If result is empty or only whitespace, return None
        if not result:
            return None

        return result
