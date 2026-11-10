"""
SystemSetting model for application configuration.

This module defines the SystemSetting model for storing
dynamic system configuration in the database.
"""

from datetime import datetime
from flask import g, has_app_context
from src.database import db

# Global generation counter for SystemSetting read cache invalidation.
_SYSTEM_SETTING_CACHE_GENERATION = 0


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
            # ISO 8601 strings (naive UTC) so the frontend parses them the same
            # way as every other timestamp. Returning raw datetimes makes Flask
            # emit RFC-1123 HTTP-date strings, which the client's parser cannot
            # read (it rendered as "Invalid Date").
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    @staticmethod
    def get_setting(key, default_value=None):
        """Get a system setting value by key, with optional default."""
        # Per-request cache keyed by (global generation, value) so hot-applied
        # changes invalidate stale entries even when the app context is reused
        # (e.g. in tests or nested contexts).
        if has_app_context():
            cache = getattr(g, '_system_setting_cache', None)
            if cache is None:
                cache = {}
                setattr(g, '_system_setting_cache', cache)
            entry = cache.get(key)
            if entry is not None and entry[0] == _SYSTEM_SETTING_CACHE_GENERATION:
                return entry[1]

        setting = SystemSetting.query.filter_by(key=key).first()
        if setting:
            # Convert value based on type
            if setting.setting_type == 'integer':
                try:
                    value = int(setting.value) if setting.value is not None else default_value
                except (ValueError, TypeError):
                    value = default_value
            elif setting.setting_type == 'boolean':
                value = setting.value.lower() in ('true', '1', 'yes') if setting.value else default_value
            elif setting.setting_type == 'float':
                try:
                    value = float(setting.value) if setting.value is not None else default_value
                except (ValueError, TypeError):
                    value = default_value
            else:  # string
                value = setting.value if setting.value is not None else default_value
        else:
            value = default_value

        if has_app_context():
            cache = getattr(g, '_system_setting_cache', None)
            if cache is None:
                cache = {}
                setattr(g, '_system_setting_cache', cache)
            cache[key] = (_SYSTEM_SETTING_CACHE_GENERATION, value)
        return value

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
        global _SYSTEM_SETTING_CACHE_GENERATION
        _SYSTEM_SETTING_CACHE_GENERATION += 1
        return setting


# Automatic cache invalidation for any SQL write to system_setting, including
# bulk deletes/updates that bypass ORM instance events.
_SYSTEM_SETTING_CACHE_INVALIDATION_REGISTERED = False


def register_system_setting_cache_invalidation():
    """Register an engine-level listener that bumps the read-cache generation
    whenever ``system_setting`` is written to. Safe to call repeatedly."""
    global _SYSTEM_SETTING_CACHE_INVALIDATION_REGISTERED
    if _SYSTEM_SETTING_CACHE_INVALIDATION_REGISTERED:
        return
    from src.database import db
    from sqlalchemy import event

    def _listener(conn, cursor, statement, parameters, context, executemany):
        global _SYSTEM_SETTING_CACHE_GENERATION
        stripped = statement.lstrip().upper()
        if stripped.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) \
                and 'system_setting' in statement.lower():
            _SYSTEM_SETTING_CACHE_GENERATION += 1

    event.listen(db.engine, 'before_cursor_execute', _listener)
    _SYSTEM_SETTING_CACHE_INVALIDATION_REGISTERED = True
