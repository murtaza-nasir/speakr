"""Tests for the drop_not_null() migration helper.

Regression cover for #379, where the previous password-nullable migration
rebuilt the whole user table from hand-written DDL. That approach failed in two
ways at once: it stranded a half-created table when the row copy failed, and on
any database where it did run to completion it silently dropped every column
the frozen DDL had fallen behind on.

The replacement swaps a single column, so the tests below assert on what the
rebuild destroyed: the other columns, the indexes and the row data.

Set TEST_DATABASE_URI to a PostgreSQL URL to also exercise that path, e.g.
    TEST_DATABASE_URI=postgresql://speakr:speakr@localhost:5432/speakr
CI sets it already, so these run there without further configuration.
"""

import os
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

from src.utils.database import drop_not_null


LEGACY_USER_TABLE = """
    CREATE TABLE user (
        id INTEGER NOT NULL,
        username VARCHAR(20) NOT NULL,
        email VARCHAR(120) NOT NULL,
        password VARCHAR(60) NOT NULL,
        is_admin BOOLEAN,
        monthly_token_budget INTEGER,
        speaker_count_mode VARCHAR(10),
        inquire_allow_notes BOOLEAN,
        PRIMARY KEY (id),
        UNIQUE (username),
        UNIQUE (email)
    )
"""


@pytest.fixture
def sqlite_engine(tmp_path):
    """A SQLite database shaped like a pre-SSO user table with later columns bolted on."""
    db_path = tmp_path / "migration.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(LEGACY_USER_TABLE)
    connection.executescript(
        """
        CREATE INDEX ix_user_speaker_count_mode ON user (speaker_count_mode);
        CREATE TABLE recording (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES user(id));
        INSERT INTO user VALUES (1, 'admin', 'a@example.test', 'bcrypt-hash-1', 1, 2000000, 'range', 1);
        INSERT INTO user VALUES (2, 'other', 'b@example.test', 'bcrypt-hash-2', 0, NULL, 'exact', 0);
        INSERT INTO recording VALUES (10, 1);
        """
    )
    connection.commit()
    connection.close()
    return create_engine(f"sqlite:///{db_path}")


def _columns(engine, table="user"):
    return [col["name"] for col in inspect(engine).get_columns(table)]


def _indexes(engine, table="user"):
    return sorted(idx["name"] for idx in inspect(engine).get_indexes(table))


def _is_nullable(engine, table, column):
    return next(
        col["nullable"] for col in inspect(engine).get_columns(table) if col["name"] == column
    )


class TestSQLite:
    def test_makes_column_nullable(self, sqlite_engine):
        assert _is_nullable(sqlite_engine, "user", "password") is False

        assert drop_not_null(sqlite_engine, "user", "password") is True

        assert _is_nullable(sqlite_engine, "user", "password") is True

    def test_preserves_every_other_column(self, sqlite_engine):
        """The frozen-DDL rebuild dropped 27 columns of settings on real databases."""
        before = _columns(sqlite_engine)

        drop_not_null(sqlite_engine, "user", "password")

        assert set(_columns(sqlite_engine)) == set(before)

    def test_moves_the_column_to_the_end(self, sqlite_engine):
        """Documented side effect: SQLite can only append, so the column moves.

        Harmless here because every read goes through the ORM by name; the
        codebase has no positional INSERT or SELECT * against the user table.
        """
        drop_not_null(sqlite_engine, "user", "password")

        assert _columns(sqlite_engine)[-1] == "password"

    def test_preserves_indexes(self, sqlite_engine):
        before = _indexes(sqlite_engine)

        drop_not_null(sqlite_engine, "user", "password")

        assert _indexes(sqlite_engine) == before

    def test_preserves_row_data(self, sqlite_engine):
        query = text("SELECT id, username, password, monthly_token_budget, speaker_count_mode "
                     "FROM user ORDER BY id")
        with sqlite_engine.connect() as conn:
            before = conn.execute(query).fetchall()

        drop_not_null(sqlite_engine, "user", "password")

        with sqlite_engine.connect() as conn:
            assert conn.execute(query).fetchall() == before

    def test_accepts_null_password_afterwards(self, sqlite_engine):
        """The whole point: an SSO user can be provisioned without a password."""
        drop_not_null(sqlite_engine, "user", "password")

        with sqlite_engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO user (username, email, password) VALUES ('sso', 's@example.test', NULL)"
            ))
            conn.commit()
            assert conn.execute(text("SELECT COUNT(*) FROM user WHERE password IS NULL")).scalar() == 1

    def test_leaves_other_not_null_constraints_alone(self, sqlite_engine):
        drop_not_null(sqlite_engine, "user", "password")

        assert _is_nullable(sqlite_engine, "user", "username") is False

    def test_is_idempotent(self, sqlite_engine):
        """The old migration re-ran and re-failed on every single startup."""
        assert drop_not_null(sqlite_engine, "user", "password") is True
        assert drop_not_null(sqlite_engine, "user", "password") is False
        assert drop_not_null(sqlite_engine, "user", "password") is False

    def test_no_op_for_unknown_table_or_column(self, sqlite_engine):
        assert drop_not_null(sqlite_engine, "nonexistent", "password") is False
        assert drop_not_null(sqlite_engine, "user", "nonexistent") is False

    def test_failure_leaves_no_partial_state(self, sqlite_engine):
        """SQLite refuses to drop an indexed column; nothing may survive the attempt.

        This is the property the original migration lacked. Its CREATE TABLE was
        committed outside any transaction, so the failure was permanent.
        """
        with sqlite_engine.connect() as conn:
            conn.execute(text("CREATE INDEX ix_user_password ON user (password)"))
            conn.commit()
        before = _columns(sqlite_engine)

        with pytest.raises(Exception):
            drop_not_null(sqlite_engine, "user", "password")

        assert _columns(sqlite_engine) == before
        assert _is_nullable(sqlite_engine, "user", "password") is False

    def test_resumes_an_interrupted_rename(self, sqlite_engine):
        """Recover a database killed between DROP COLUMN and RENAME COLUMN."""
        with sqlite_engine.connect() as conn:
            conn.execute(text('ALTER TABLE "user" ADD COLUMN "password__nullable_tmp" VARCHAR(60)'))
            conn.execute(text('UPDATE "user" SET "password__nullable_tmp" = "password"'))
            conn.execute(text('ALTER TABLE "user" DROP COLUMN "password"'))
            conn.commit()
        assert "password" not in _columns(sqlite_engine)

        assert drop_not_null(sqlite_engine, "user", "password") is True

        assert "password" in _columns(sqlite_engine)
        assert "password__nullable_tmp" not in _columns(sqlite_engine)
        with sqlite_engine.connect() as conn:
            assert conn.execute(
                text("SELECT password FROM user WHERE id = 1")
            ).scalar() == "bcrypt-hash-1"


class TestSQLiteIssue379:
    """End-to-end cover for the exact database state reported in #379."""

    def test_stale_user_new_does_not_block_the_migration(self, sqlite_engine):
        with sqlite_engine.connect() as conn:
            conn.execute(text("CREATE TABLE user_new (id INTEGER PRIMARY KEY, username VARCHAR(20))"))
            conn.commit()
        before = _columns(sqlite_engine)

        assert drop_not_null(sqlite_engine, "user", "password") is True

        assert _is_nullable(sqlite_engine, "user", "password") is True
        assert set(_columns(sqlite_engine)) == set(before)

    def test_orphaned_table_cleanup_is_guarded(self, sqlite_engine):
        """Never drop user_new while it holds rows the live table does not."""
        from src.init_db import _remove_orphaned_user_new

        class _App:
            class logger:
                @staticmethod
                def info(*args, **kwargs):
                    pass

                @staticmethod
                def warning(*args, **kwargs):
                    pass

        with sqlite_engine.connect() as conn:
            conn.execute(text("CREATE TABLE user_new (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO user_new VALUES (1), (2), (3)"))
            conn.commit()

        _remove_orphaned_user_new(sqlite_engine, _App)
        assert "user_new" in inspect(sqlite_engine).get_table_names(), (
            "user_new held more rows than user and must be preserved for inspection"
        )

        with sqlite_engine.connect() as conn:
            conn.execute(text("DELETE FROM user_new WHERE id = 3"))
            conn.commit()

        _remove_orphaned_user_new(sqlite_engine, _App)
        assert "user_new" not in inspect(sqlite_engine).get_table_names()


_TEST_DB_URI = os.environ.get("TEST_DATABASE_URI", "")
POSTGRES_URL = _TEST_DB_URI if _TEST_DB_URI.startswith("postgresql") else None


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_DATABASE_URI is not a PostgreSQL URL")
class TestPostgreSQL:
    """The production deployment runs PostgreSQL, so the native path needs cover too."""

    @pytest.fixture
    def pg_engine(self):
        engine = create_engine(POSTGRES_URL)
        with engine.connect() as conn:
            conn.execute(text('DROP TABLE IF EXISTS migration_test_user'))
            conn.execute(text("""
                CREATE TABLE migration_test_user (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(20) NOT NULL UNIQUE,
                    password VARCHAR(60) NOT NULL,
                    monthly_token_budget INTEGER
                )
            """))
            conn.execute(text(
                "INSERT INTO migration_test_user (username, password, monthly_token_budget) "
                "VALUES ('admin', 'bcrypt-hash-1', 2000000)"
            ))
            conn.commit()
        yield engine
        with engine.connect() as conn:
            conn.execute(text('DROP TABLE IF EXISTS migration_test_user'))
            conn.commit()
        engine.dispose()

    def test_makes_column_nullable(self, pg_engine):
        assert _is_nullable(pg_engine, "migration_test_user", "password") is False

        assert drop_not_null(pg_engine, "migration_test_user", "password") is True

        assert _is_nullable(pg_engine, "migration_test_user", "password") is True

    def test_preserves_columns_and_data(self, pg_engine):
        before = _columns(pg_engine, "migration_test_user")

        drop_not_null(pg_engine, "migration_test_user", "password")

        assert _columns(pg_engine, "migration_test_user") == before
        with pg_engine.connect() as conn:
            assert conn.execute(text(
                "SELECT password, monthly_token_budget FROM migration_test_user WHERE username = 'admin'"
            )).fetchone() == ("bcrypt-hash-1", 2000000)

    def test_is_idempotent(self, pg_engine):
        assert drop_not_null(pg_engine, "migration_test_user", "password") is True
        assert drop_not_null(pg_engine, "migration_test_user", "password") is False

    def test_accepts_null_password_afterwards(self, pg_engine):
        drop_not_null(pg_engine, "migration_test_user", "password")

        with pg_engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO migration_test_user (username, password) VALUES ('sso', NULL)"
            ))
            conn.commit()
            assert conn.execute(text(
                "SELECT COUNT(*) FROM migration_test_user WHERE password IS NULL"
            )).scalar() == 1

    def test_leaves_other_not_null_constraints_alone(self, pg_engine):
        drop_not_null(pg_engine, "migration_test_user", "password")

        assert _is_nullable(pg_engine, "migration_test_user", "username") is False
