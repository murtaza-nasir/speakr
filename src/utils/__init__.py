"""
Utility functions package for the Speakr application.

This package contains various utility modules for:
- JSON parsing and handling
- Markdown to HTML conversion
- Datetime formatting and timezone handling
- Security utilities
"""

from .json_parser import (
    auto_close_json,
    safe_json_loads,
    preprocess_json_escapes,
    extract_json_object
)

from .markdown import (
    md_to_html,
    sanitize_html
)

from .datetime import (
    local_datetime_filter
)

from .security import (
    password_check,
    is_safe_url
)

from .database import (
    add_column_if_not_exists,
    migrate_column_type
)

__all__ = [
    # JSON parsing
    'auto_close_json',
    'safe_json_loads',
    'preprocess_json_escapes',
    'extract_json_object',
    # Markdown/HTML
    'md_to_html',
    'sanitize_html',
    # Datetime
    'local_datetime_filter',
    # Security
    'password_check',
    'is_safe_url',
    # Database
    'add_column_if_not_exists',
    'migrate_column_type',
]
