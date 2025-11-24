"""
Database initialization module.

This module creates and exports the SQLAlchemy database instance
that is used across all models.
"""

from flask_sqlalchemy import SQLAlchemy

# Create the SQLAlchemy database instance
# This will be initialized with the Flask app using db.init_app(app)
db = SQLAlchemy()
