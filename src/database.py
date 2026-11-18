"""
Database initialization module.

This module creates and exports the SQLAlchemy database instance
that is used across all models.
"""

import sqlite3

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

# Create the SQLAlchemy database instance
# This will be initialized with the Flask app using db.init_app(app)
db = SQLAlchemy()


def _casefold(value):
    return value.casefold() if isinstance(value, str) else value


@event.listens_for(Engine, "connect")
def _register_sqlite_functions(dbapi_connection, connection_record):
    """speakr_casefold(text) on SQLite connections: SQLite's LIKE folds only
    ASCII letters, so a search for "привет" would miss "Привет" (#622).
    PostgreSQL's ILIKE folds Unicode itself and needs nothing."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("speakr_casefold", 1, _casefold, deterministic=True)
