"""
Database schema migration utilities.

IMPORTANT: All migrations must be compatible with both SQLite and PostgreSQL.
- Boolean defaults: SQLite uses 0/1, PostgreSQL requires FALSE/TRUE
- Type differences: SQLite DATETIME -> PostgreSQL TIMESTAMP, BLOB -> BYTEA
- Reserved keywords: "user", "order" etc. must be quoted
- The add_column_if_not_exists() function handles these automatically
- Use create_index_if_not_exists() for index creation with proper quoting
"""

import fcntl
import hashlib
import os
import re
import tempfile
import time
from contextlib import contextmanager

from sqlalchemy import inspect, text

# Arbitrary but fixed key for PostgreSQL advisory locking, derived from a name so
# it cannot collide with an application lock chosen the same way.
_PG_MIGRATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b'speakr.startup_migrations').digest()[:4], 'big'
)


@contextmanager
def migration_lock(engine, logger=None, timeout=120):
    """Serialise startup migrations across every process touching this database.

    `initialize_database()` runs about five times per container start: from the
    entrypoint, from the admin-user script, and once per gunicorn worker, several
    of them simultaneously. Without a lock they interleave, and a migration can
    see a half-applied schema produced by a sibling process.

    Yields True when the lock was taken and False when it timed out. Timing out
    is not fatal and the caller should proceed: migrations are individually
    idempotent, so the lock is protection against wasted work and interleaving
    rather than a correctness requirement. Failing open keeps a stuck lock from
    holding the container down.
    """
    acquired = False
    handle = None
    connection = None

    try:
        if engine.name == 'postgresql':
            connection = engine.connect()
            deadline = time.monotonic() + timeout
            while True:
                acquired = bool(connection.execute(
                    text('SELECT pg_try_advisory_lock(:key)'),
                    {'key': _PG_MIGRATION_LOCK_KEY},
                ).scalar())
                if acquired or time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
            # Close the implicit transaction the SELECT opened. The advisory
            # lock is session-level, so it survives the commit; without this
            # the connection sits idle-in-transaction for the whole migration
            # run and a hardened server's timeout could kill it mid-hold.
            connection.commit()
        else:
            # One lock file per database URL so unrelated databases on the same
            # host do not serialise against each other.
            digest = hashlib.sha256(str(engine.url).encode()).hexdigest()[:16]
            lock_path = os.path.join(tempfile.gettempdir(), f'speakr_migration_{digest}.lock')
            handle = open(lock_path, 'w')
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    acquired = False
                if acquired or time.monotonic() >= deadline:
                    break
                time.sleep(0.5)

        if not acquired and logger:
            logger.warning(
                "Could not acquire the migration lock within %ss; proceeding anyway. "
                "Migrations are idempotent, so this is safe but may duplicate work.",
                timeout,
            )

        yield acquired

    finally:
        try:
            if connection is not None:
                if acquired:
                    connection.execute(
                        text('SELECT pg_advisory_unlock(:key)'),
                        {'key': _PG_MIGRATION_LOCK_KEY},
                    )
                connection.close()
            if handle is not None:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        except Exception:
            # Both lock types are released by the OS or the connection closing,
            # so a failure here must not mask whatever the caller was doing.
            pass


def ensure_migration_ledger(engine):
    """Create the table recording which one-shot migrations have already run."""
    with engine.connect() as conn:
        conn.execute(text(
            'CREATE TABLE IF NOT EXISTS schema_migrations ('
            '  migration_id VARCHAR(255) NOT NULL PRIMARY KEY,'
            '  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
            ')'
        ))
        conn.commit()


def run_once(engine, migration_id, migration, logger=None):
    """Run a one-shot migration the first time only, then never again.

    Most migrations here detect their own state, which works but means every
    startup pays to re-check, and the detection is itself a source of bugs when
    the schema drifts underneath it. A migration that cannot cheaply tell
    whether it has run, or that would otherwise scan a whole table on every
    boot, should be registered here instead.

    The callable is passed the engine and runs before the id is recorded, so a
    failure leaves the migration unrecorded and it is retried next startup.

    Returns True if the migration ran on this call, False if it had already run.
    """
    ensure_migration_ledger(engine)

    with engine.connect() as conn:
        already_applied = conn.execute(
            text('SELECT 1 FROM schema_migrations WHERE migration_id = :id'),
            {'id': migration_id},
        ).scalar()

    if already_applied:
        return False

    migration(engine)

    with engine.connect() as conn:
        conn.execute(
            text('INSERT INTO schema_migrations (migration_id) VALUES (:id)'),
            {'id': migration_id},
        )
        conn.commit()

    if logger:
        logger.info("Applied one-shot migration '%s'", migration_id)
    return True


def add_column_if_not_exists(engine, table_name, column_name, column_type):
    """
    Add a column to a table if it doesn't already exist.

    Args:
        engine: SQLAlchemy engine
        table_name: Name of the table
        column_name: Name of the column to add
        column_type: SQL type definition for the column

    Returns:
        bool: True if column was added, False if it already existed
    """
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]

    if column_name not in columns:
        if engine.name == 'postgresql':
            # PostgreSQL requires TRUE/FALSE for boolean defaults, not 0/1
            if 'BOOLEAN' in column_type.upper():
                column_type = column_type.replace('DEFAULT 0', 'DEFAULT FALSE')
                column_type = column_type.replace('DEFAULT 1', 'DEFAULT TRUE')

            # PostgreSQL uses TIMESTAMP, not DATETIME
            column_type = re.sub(r'\bDATETIME\b', 'TIMESTAMP', column_type, flags=re.IGNORECASE)

            # PostgreSQL uses BYTEA, not BLOB
            column_type = re.sub(r'\bBLOB\b', 'BYTEA', column_type, flags=re.IGNORECASE)

            # PostgreSQL interprets double-quoted strings as identifiers, not literals
            # Convert DEFAULT "value" to DEFAULT 'value'
            column_type = re.sub(r'''DEFAULT\s+"([^"]*)"''', r"DEFAULT '\1'", column_type, flags=re.IGNORECASE)

        with engine.connect() as conn:
            # Quote identifiers to handle reserved keywords (e.g., "user" in PostgreSQL)
            # MySQL uses backticks, PostgreSQL/SQLite use double quotes
            # Handle special case where column_type includes the column name
            if column_name in column_type:
                if engine.name == 'mysql':
                    conn.execute(text(f'ALTER TABLE `{table_name}` ADD COLUMN {column_type}'))
                else:
                    conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {column_type}'))
            else:
                if engine.name == 'mysql':
                    conn.execute(text(f'ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_type}'))
                else:
                    conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}'))
            conn.commit()
        return True
    return False


def create_index_if_not_exists(engine, index_name, table_name, columns, unique=False):
    """
    Create an index on a table if it doesn't already exist.

    Handles cross-database compatibility by properly quoting table names,
    especially important for reserved keywords like 'user', 'order', etc.

    Args:
        engine: SQLAlchemy engine
        index_name: Name of the index to create
        table_name: Name of the table
        columns: Column(s) to index (string, can be comma-separated for composite)
        unique: Whether to create a unique index (default False)

    Returns:
        bool: True if index was created, False if it already existed or table doesn't exist
    """
    inspector = inspect(engine)

    # Check if table exists
    if table_name not in inspector.get_table_names():
        return False

    # Check if index already exists
    existing_indexes = [idx['name'] for idx in inspector.get_indexes(table_name)]
    if index_name in existing_indexes:
        return False

    unique_clause = 'UNIQUE ' if unique else ''

    with engine.connect() as conn:
        # Quote table name to handle reserved keywords (e.g., "user" in PostgreSQL)
        # MySQL uses backticks, PostgreSQL/SQLite use double quotes
        if engine.name == 'mysql':
            quoted_table = f'`{table_name}`'
        else:
            quoted_table = f'"{table_name}"'

        # Note: IF NOT EXISTS may not be supported on all databases, but we already
        # checked for existence above, so it's just a safety net
        try:
            conn.execute(text(
                f'CREATE {unique_clause}INDEX IF NOT EXISTS {index_name} ON {quoted_table} ({columns})'
            ))
        except Exception:
            # Some databases don't support IF NOT EXISTS, try without
            conn.execute(text(
                f'CREATE {unique_clause}INDEX {index_name} ON {quoted_table} ({columns})'
            ))
        conn.commit()
    return True


def drop_not_null(engine, table_name, column_name):
    """
    Make an existing NOT NULL column nullable.

    PostgreSQL and MySQL alter the constraint in place. SQLite cannot alter a
    column constraint at all, so it swaps in a fresh column: ADD COLUMN always
    produces a nullable column, so copying the values across and renaming
    reaches the same end state.

    The SQLite path deliberately does NOT rebuild the table. A rebuild has to
    restate the whole schema in hand-written DDL, which silently drops every
    column, index and constraint the DDL has fallen behind on; that is exactly
    how the previous version of this migration came to destroy 27 columns of
    user settings on any database it managed to run to completion (#379).

    The SQLite statements run inside one explicit transaction because pysqlite
    does not open one for DDL by itself. Without it a failure partway through
    leaves the half-built column committed and the migration permanently
    wedged, which is the other half of #379. BEGIN IMMEDIATE takes the write
    lock up front so the several worker processes that run migrations at
    startup cannot race, and the constraint is re-checked under that lock.

    Note: SQLite refuses to drop a column that is indexed, unique or part of
    the primary key, so this helper cannot be used on such a column.

    Args:
        engine: SQLAlchemy engine
        table_name: Name of the table
        column_name: Name of the column to make nullable

    Returns:
        bool: True if the column was made nullable, False if there was nothing
              to do (table or column missing, or already nullable).
    """
    inspector = inspect(engine)

    if table_name not in inspector.get_table_names():
        return False

    columns = {col['name']: col for col in inspector.get_columns(table_name)}
    temp_col = f"{column_name}__nullable_tmp"

    # Resume an attempt that died between DROP COLUMN and RENAME COLUMN: the
    # data is all in the temporary column, so completing the rename is safe.
    if engine.name == 'sqlite' and column_name not in columns and temp_col in columns:
        with engine.connect() as conn:
            conn.execute(text(
                f'ALTER TABLE "{table_name}" RENAME COLUMN "{temp_col}" TO "{column_name}"'
            ))
            conn.commit()
        return True

    column = columns.get(column_name)
    if column is None or column.get('nullable', True):
        return False

    column_type = column['type'].compile(engine.dialect)

    if engine.name == 'postgresql':
        with engine.connect() as conn:
            conn.execute(text(
                f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" DROP NOT NULL'
            ))
            conn.commit()
        return True

    if engine.name == 'mysql':
        with engine.connect() as conn:
            conn.execute(text(
                f'ALTER TABLE `{table_name}` MODIFY COLUMN `{column_name}` {column_type} NULL'
            ))
            conn.commit()
        return True

    if engine.name != 'sqlite':
        raise NotImplementedError(
            f"drop_not_null() does not support the '{engine.name}' dialect"
        )

    raw_connection = engine.raw_connection()
    try:
        dbapi_connection = raw_connection.driver_connection
        previous_isolation = dbapi_connection.isolation_level
        # Hand transaction control to us so that BEGIN covers the DDL too.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('BEGIN IMMEDIATE')

            # Re-check under the write lock; a concurrent worker may have
            # completed the migration between our inspection and this point.
            still_not_null = any(
                row[1] == column_name and row[3] == 1
                for row in cursor.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            )
            if not still_not_null:
                cursor.execute('ROLLBACK')
                return False

            if temp_col in columns:
                cursor.execute(f'ALTER TABLE "{table_name}" DROP COLUMN "{temp_col}"')

            cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{temp_col}" {column_type}')
            cursor.execute(f'UPDATE "{table_name}" SET "{temp_col}" = "{column_name}"')
            cursor.execute(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')
            cursor.execute(
                f'ALTER TABLE "{table_name}" RENAME COLUMN "{temp_col}" TO "{column_name}"'
            )
            cursor.execute('COMMIT')
        except Exception:
            # If BEGIN itself failed there is no transaction to roll back, and
            # that secondary error must not mask the one we are re-raising.
            try:
                cursor.execute('ROLLBACK')
            except Exception:
                pass
            raise
        finally:
            cursor.close()
            dbapi_connection.isolation_level = previous_isolation
    finally:
        raw_connection.close()

    return True


def migrate_column_type(engine, table_name, column_name, new_type, transform_sql=None):
    """
    Migrate a column to a new type if it exists.

    For SQLite, this uses a temporary column approach since SQLite doesn't support ALTER COLUMN.

    Args:
        engine: SQLAlchemy engine
        table_name: Name of the table
        column_name: Name of the column to modify
        new_type: New SQL type for the column
        transform_sql: Optional SQL expression to transform existing data (e.g., "datetime(meeting_date || ' 12:00:00')")
                       If None, data is copied as-is

    Returns:
        bool: True if column was migrated, False if it didn't exist or migration wasn't needed
    """
    inspector = inspect(engine)

    # Check if table exists
    if table_name not in inspector.get_table_names():
        return False

    columns = {col['name']: col for col in inspector.get_columns(table_name)}

    if column_name not in columns:
        return False

    engine_name = engine.name

    with engine.connect() as conn:
        if engine_name == 'sqlite':
            # SQLite approach: use temporary column
            temp_col = f"{column_name}_new"

            # Check if temp column already exists (migration interrupted?)
            if temp_col in columns:
                try:
                    # Try to drop it and start over
                    conn.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{temp_col}"'))
                    conn.commit()
                except Exception:
                    # If we can't drop it, the migration may have partially completed
                    # Check if old column still exists
                    if column_name not in columns:
                        # Old column is gone, temp exists - just rename temp to complete migration
                        try:
                            conn.execute(text(f'ALTER TABLE "{table_name}" RENAME COLUMN "{temp_col}" TO "{column_name}"'))
                            conn.commit()
                            return True
                        except Exception as e:
                            # Can't complete, leave as-is
                            return False
                    # Both columns exist - abort to avoid data issues
                    return False

            # Add temporary column with new type
            conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{temp_col}" {new_type}'))

            # Copy data with optional transformation
            if transform_sql:
                conn.execute(text(f'UPDATE "{table_name}" SET "{temp_col}" = {transform_sql} WHERE "{column_name}" IS NOT NULL'))
            else:
                conn.execute(text(f'UPDATE "{table_name}" SET "{temp_col}" = "{column_name}"'))

            # Drop old column (SQLite 3.35.0+ only)
            try:
                conn.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'))
                # Drop succeeded, now rename temp to original name
                conn.execute(text(f'ALTER TABLE "{table_name}" RENAME COLUMN "{temp_col}" TO "{column_name}"'))
                conn.commit()
            except Exception:
                # Older SQLite - can't drop columns
                # Rename temp column to original name (this will fail if original still exists)
                try:
                    conn.execute(text(f'ALTER TABLE "{table_name}" RENAME COLUMN "{temp_col}" TO "{column_name}"'))
                    conn.commit()
                except Exception:
                    # Can't rename because old column exists - this is OK for SQLite
                    # Just keep the new column and let the app use the old one
                    # The data in the old column is still valid
                    conn.rollback()
                    # Actually, let's just commit the temp column addition
                    # The model will use column_name which still exists with old data
                    # This is safe - new records will use the new model definition
                    return False

        elif engine_name == 'postgresql':
            # PostgreSQL can alter column type directly
            if transform_sql:
                conn.execute(text(f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" TYPE {new_type} USING {transform_sql}'))
            else:
                conn.execute(text(f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" TYPE {new_type}'))
            conn.commit()

        elif engine_name == 'mysql':
            # MySQL can modify column type
            conn.execute(text(f'ALTER TABLE `{table_name}` MODIFY COLUMN `{column_name}` {new_type}'))

            # Apply transformation if provided
            if transform_sql:
                conn.execute(text(f'UPDATE `{table_name}` SET `{column_name}` = {transform_sql} WHERE `{column_name}` IS NOT NULL'))
            conn.commit()

        return True

    return False
