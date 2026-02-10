"""
Integration test for database migrations against a real database engine.

Runs initialize_database() and verifies that all tables and critical columns
are created successfully. Works with both SQLite (default, for local runs)
and PostgreSQL (when TEST_DATABASE_URI env var is set).

IMPORTANT: This test uses TEST_DATABASE_URI (not SQLALCHEMY_DATABASE_URI) to
avoid accidentally connecting to and destroying a real application database.

Usage:
    # Local (SQLite in-memory, safe):
    python tests/test_postgres_migrations.py

    # Against PostgreSQL (CI or explicit testing):
    TEST_DATABASE_URI=postgresql://user:pass@localhost:5432/testdb \
        python tests/test_postgres_migrations.py
"""

import os
import sys
import unittest

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import Flask
from src.database import db
# Importing models registers them with SQLAlchemy so create_all() builds all tables
import src.models  # noqa: F401
from src.init_db import initialize_database


def create_test_app():
    """Create a minimal Flask app for testing database operations.

    Uses TEST_DATABASE_URI env var (NOT SQLALCHEMY_DATABASE_URI) to prevent
    accidental connection to production/dev databases.
    """
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'TEST_DATABASE_URI', 'sqlite://'  # in-memory SQLite by default
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['TESTING'] = True
    db.init_app(app)
    return app


class TestDatabaseMigrations(unittest.TestCase):
    """Test that initialize_database() runs cleanly against the configured DB engine."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_test_app()
        with cls.app.app_context():
            initialize_database(cls.app)

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            # Use raw DROP to avoid circular FK dependency errors in SQLAlchemy's
            # drop_all() (user <-> naming_template have mutual foreign keys)
            from sqlalchemy import inspect, text
            tables = inspect(db.engine).get_table_names()
            with db.engine.connect() as conn:
                if db.engine.name == 'postgresql':
                    for table in tables:
                        conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
                else:
                    conn.execute(text('PRAGMA foreign_keys = OFF'))
                    for table in tables:
                        conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
                    conn.execute(text('PRAGMA foreign_keys = ON'))
                conn.commit()

    def _get_table_names(self):
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        return inspector.get_table_names()

    def _get_column_names(self, table):
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        return [col['name'] for col in inspector.get_columns(table)]

    def test_core_tables_exist(self):
        """Verify that all core tables were created."""
        with self.app.app_context():
            tables = self._get_table_names()
            expected_tables = [
                'user', 'recording', 'transcript_chunk', 'tag',
                'folder', 'share', 'internal_share', 'system_setting',
                'speaker', 'processing_job', 'group', 'group_membership',
            ]
            for table in expected_tables:
                self.assertIn(table, tables, f"Missing table: {table}")

    def test_user_migration_columns(self):
        """Verify columns added by migrations exist on the user table."""
        with self.app.app_context():
            columns = self._get_column_names('user')
            expected = [
                'id', 'username', 'email', 'password',
                'transcription_language', 'output_language', 'ui_language',
                'summary_prompt', 'extract_events', 'name', 'job_title',
                'company', 'diarize', 'sso_provider', 'sso_subject',
                'can_share_publicly', 'monthly_token_budget',
                'monthly_transcription_budget', 'email_verified',
                'auto_speaker_labelling', 'auto_summarization',
            ]
            for col in expected:
                self.assertIn(col, columns, f"Missing user column: {col}")

    def test_recording_migration_columns(self):
        """Verify columns added by migrations exist on the recording table."""
        with self.app.app_context():
            columns = self._get_column_names('recording')
            expected = [
                'id', 'is_inbox', 'is_highlighted', 'mime_type',
                'completed_at', 'processing_time_seconds', 'error_message',
                'folder_id', 'audio_deleted_at', 'deletion_exempt',
                'speaker_embeddings',
            ]
            for col in expected:
                self.assertIn(col, columns, f"Missing recording column: {col}")

    def test_tag_migration_columns(self):
        """Verify columns added by migrations exist on the tag table."""
        with self.app.app_context():
            columns = self._get_column_names('tag')
            expected = [
                'id', 'protect_from_deletion', 'group_id',
                'retention_days', 'auto_share_on_apply',
                'naming_template_id', 'export_template_id',
            ]
            for col in expected:
                self.assertIn(col, columns, f"Missing tag column: {col}")

    def test_system_settings_initialized(self):
        """Verify that default system settings were created."""
        with self.app.app_context():
            from src.models import SystemSetting
            expected_keys = [
                'transcript_length_limit', 'max_file_size_mb',
                'asr_timeout_seconds', 'disable_auto_summarization',
                'enable_folders',
            ]
            for key in expected_keys:
                setting = SystemSetting.query.filter_by(key=key).first()
                self.assertIsNotNone(setting, f"Missing system setting: {key}")

    def test_engine_type_matches_expectation(self):
        """Sanity check: confirm we're testing against the expected engine."""
        with self.app.app_context():
            uri = self.app.config['SQLALCHEMY_DATABASE_URI']
            engine_name = db.engine.name
            if uri.startswith('postgresql'):
                self.assertEqual(engine_name, 'postgresql')
            else:
                self.assertEqual(engine_name, 'sqlite')


if __name__ == '__main__':
    unittest.main()
