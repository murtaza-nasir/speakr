"""
Database schema migration utilities.

IMPORTANT: All migrations must be compatible with both SQLite and PostgreSQL.
- Boolean defaults: SQLite uses 0/1, PostgreSQL requires FALSE/TRUE
- Type differences: SQLite DATETIME -> PostgreSQL TIMESTAMP, BLOB -> BYTEA
- Reserved keywords: "user", "order" etc. must be quoted
- The add_column_if_not_exists() function handles these automatically
- Use create_index_if_not_exists() for index creation with proper quoting
"""

import re
from sqlalchemy import inspect, text


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
