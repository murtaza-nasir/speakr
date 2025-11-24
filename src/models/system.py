"""
SystemSetting model for application configuration.

This module defines the SystemSetting model for storing
dynamic system configuration in the database.
"""

from datetime import datetime
from src.database import db


class SystemSetting(db.Model):
    """Stores system-wide configuration settings."""

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    setting_type = db.Column(db.String(50), nullable=False, default='string')  # string, integer, boolean, float
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'setting_type': self.setting_type,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }

    @staticmethod
    def get_setting(key, default_value=None):
        """Get a system setting value by key, with optional default."""
        setting = SystemSetting.query.filter_by(key=key).first()
        if setting:
            # Convert value based on type
            if setting.setting_type == 'integer':
                try:
                    return int(setting.value) if setting.value is not None else default_value
                except (ValueError, TypeError):
                    return default_value
            elif setting.setting_type == 'boolean':
                return setting.value.lower() in ('true', '1', 'yes') if setting.value else default_value
            elif setting.setting_type == 'float':
                try:
                    return float(setting.value) if setting.value is not None else default_value
                except (ValueError, TypeError):
                    return default_value
            else:  # string
                return setting.value if setting.value is not None else default_value
        return default_value

    @staticmethod
    def set_setting(key, value, description=None, setting_type='string'):
        """Set a system setting value."""
        setting = SystemSetting.query.filter_by(key=key).first()
        if setting:
            setting.value = str(value) if value is not None else None
            setting.updated_at = datetime.utcnow()
            if description:
                setting.description = description
            if setting_type:
                setting.setting_type = setting_type
        else:
            setting = SystemSetting(
                key=key,
                value=str(value) if value is not None else None,
                description=description,
                setting_type=setting_type
            )
            db.session.add(setting)
        db.session.commit()
        return setting
