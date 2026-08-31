"""Tests for the migration lock and the one-shot migration ledger.

Both exist because `initialize_database()` runs about five times per container
start, several of them concurrently: from the entrypoint, from the admin-user
script, and once per gunicorn worker.

Set TEST_DATABASE_URI to a PostgreSQL URL to also exercise those paths. CI sets
it already, so these run there without further configuration.
"""

import os
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import create_engine, inspect, text

from src.utils.database import ensure_migration_ledger, migration_lock, run_once


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST_DB_URI = os.environ.get("TEST_DATABASE_URI", "")
POSTGRES_URL = _TEST_DB_URI if _TEST_DB_URI.startswith("postgresql") else None


@pytest.fixture
def sqlite_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'infra.db'}")


class TestRunOnce:
    def test_runs_the_first_time_only(self, sqlite_engine):
        calls = []

        assert run_once(sqlite_engine, "m1", lambda engine: calls.append(1)) is True
        assert run_once(sqlite_engine, "m1", lambda engine: calls.append(1)) is False
        assert run_once(sqlite_engine, "m1", lambda engine: calls.append(1)) is False

        assert calls == [1]

    def test_different_migrations_are_independent(self, sqlite_engine):
        calls = []

        run_once(sqlite_engine, "m1", lambda engine: calls.append("a"))
        run_once(sqlite_engine, "m2", lambda engine: calls.append("b"))

        assert calls == ["a", "b"]

    def test_a_failure_is_not_recorded_and_retries(self, sqlite_engine):
        """An unrecorded failure must come back on the next startup."""
        attempts = []

        def failing(engine):
            attempts.append(1)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            run_once(sqlite_engine, "m1", failing)

        with sqlite_engine.connect() as conn:
            recorded = conn.execute(
                text("SELECT COUNT(*) FROM schema_migrations WHERE migration_id = 'm1'")
            ).scalar()
        assert recorded == 0

        assert run_once(sqlite_engine, "m1", lambda engine: attempts.append(2)) is True
        assert attempts == [1, 2]

    def test_ledger_is_created_on_demand(self, sqlite_engine):
        assert "schema_migrations" not in inspect(sqlite_engine).get_table_names()

        ensure_migration_ledger(sqlite_engine)

        assert "schema_migrations" in inspect(sqlite_engine).get_table_names()

    def test_ledger_creation_is_idempotent(self, sqlite_engine):
        ensure_migration_ledger(sqlite_engine)
        ensure_migration_ledger(sqlite_engine)

        assert "schema_migrations" in inspect(sqlite_engine).get_table_names()

    def test_records_when_it_was_applied(self, sqlite_engine):
        run_once(sqlite_engine, "m1", lambda engine: None)

        with sqlite_engine.connect() as conn:
            applied_at = conn.execute(
                text("SELECT applied_at FROM schema_migrations WHERE migration_id = 'm1'")
            ).scalar()

        assert applied_at is not None


class TestMigrationLock:
    def test_acquires_when_uncontended(self, sqlite_engine):
        with migration_lock(sqlite_engine) as acquired:
            assert acquired is True

    def test_is_reusable_after_release(self, sqlite_engine):
        with migration_lock(sqlite_engine) as first:
            assert first is True
        with migration_lock(sqlite_engine) as second:
            assert second is True

    def test_releases_even_when_the_body_raises(self, sqlite_engine):
        with pytest.raises(RuntimeError):
            with migration_lock(sqlite_engine):
                raise RuntimeError("boom")

        with migration_lock(sqlite_engine) as acquired:
            assert acquired is True

    def test_times_out_and_fails_open_while_another_process_holds_it(self, tmp_path):
        """A held lock must not hang startup: the caller proceeds with False."""
        db_path = tmp_path / "contended.db"
        engine = create_engine(f"sqlite:///{db_path}")

        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {REPO_ROOT!r})
                from sqlalchemy import create_engine
                from src.utils.database import migration_lock
                engine = create_engine("sqlite:///{db_path}")
                with migration_lock(engine) as acquired:
                    print("HELD" if acquired else "FAILED", flush=True)
                    time.sleep(30)
            """)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "PYTHONPATH": REPO_ROOT},
        )
        try:
            assert holder.stdout.readline().strip() == "HELD"

            with migration_lock(engine, timeout=1) as acquired:
                assert acquired is False, "should fail open rather than block forever"
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_lock_is_released_when_the_holder_dies(self, tmp_path):
        """A killed worker must not leave the lock held for the next start."""
        db_path = tmp_path / "orphan.db"
        engine = create_engine(f"sqlite:///{db_path}")

        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {REPO_ROOT!r})
                from sqlalchemy import create_engine
                from src.utils.database import migration_lock
                engine = create_engine("sqlite:///{db_path}")
                with migration_lock(engine):
                    print("HELD", flush=True)
                    time.sleep(30)
            """)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "PYTHONPATH": REPO_ROOT},
        )
        assert holder.stdout.readline().strip() == "HELD"
        holder.kill()
        holder.wait(timeout=10)

        with migration_lock(engine, timeout=5) as acquired:
            assert acquired is True

    def test_separate_databases_do_not_block_each_other(self, tmp_path):
        one = create_engine(f"sqlite:///{tmp_path / 'one.db'}")
        two = create_engine(f"sqlite:///{tmp_path / 'two.db'}")

        with migration_lock(one) as first:
            with migration_lock(two, timeout=2) as second:
                assert first is True
                assert second is True


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_DATABASE_URI is not a PostgreSQL URL")
class TestPostgreSQL:
    @pytest.fixture
    def pg_engine(self):
        engine = create_engine(POSTGRES_URL)
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))
            conn.commit()
        yield engine
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))
            conn.commit()
        engine.dispose()

    def test_run_once_runs_the_first_time_only(self, pg_engine):
        calls = []

        assert run_once(pg_engine, "m1", lambda engine: calls.append(1)) is True
        assert run_once(pg_engine, "m1", lambda engine: calls.append(1)) is False

        assert calls == [1]

    def test_ledger_is_created_on_demand(self, pg_engine):
        ensure_migration_ledger(pg_engine)

        assert "schema_migrations" in inspect(pg_engine).get_table_names()

    def test_advisory_lock_is_exclusive_across_sessions(self, pg_engine):
        """A second connection is a second session, which is what a worker is."""
        other = create_engine(POSTGRES_URL)
        try:
            with migration_lock(pg_engine) as first:
                assert first is True
                with migration_lock(other, timeout=1) as second:
                    assert second is False
            # Released, so the other session can take it now.
            with migration_lock(other, timeout=5) as third:
                assert third is True
        finally:
            other.dispose()
