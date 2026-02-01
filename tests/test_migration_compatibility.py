"""
Test suite to ensure database migrations are compatible with both SQLite and PostgreSQL.

These tests scan the init_db.py file for patterns that would break on PostgreSQL,
such as SQLite-only boolean defaults (0/1 instead of FALSE/TRUE) and unquoted
reserved keywords.

Run with: python tests/test_migration_compatibility.py
"""

import re
import unittest
import os


class TestMigrationCompatibility(unittest.TestCase):
    """Tests to ensure init_db.py uses cross-database compatible SQL."""

    @classmethod
    def setUpClass(cls):
        """Load init_db.py content once for all tests."""
        # Find the project root
        test_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(test_dir)
        init_db_path = os.path.join(project_root, 'src', 'init_db.py')

        with open(init_db_path, 'r') as f:
            cls.content = f.read()

    def test_no_raw_boolean_defaults_in_alter_table(self):
        """
        Ensure no raw ALTER TABLE statements use SQLite-only boolean defaults.

        The pattern 'BOOLEAN DEFAULT 0' or 'BOOLEAN DEFAULT 1' in raw SQL
        will fail on PostgreSQL, which requires 'DEFAULT FALSE' or 'DEFAULT TRUE'.

        Using add_column_if_not_exists() handles this conversion automatically.
        """
        # Pattern to find raw SQL with text() that has BOOLEAN DEFAULT 0/1
        # This matches: text('... BOOLEAN DEFAULT 0 ...') or text("...")
        pattern = r"conn\.execute\s*\(\s*text\s*\(['\"]([^'\"]*BOOLEAN\s+DEFAULT\s+[01][^'\"]*)['\"]"

        matches = re.findall(pattern, self.content, re.IGNORECASE)

        # Filter out false positives - we're looking for raw ALTER TABLE statements
        # not UPDATE statements or other SQL that legitimately uses 0/1
        problematic = []
        for match in matches:
            match_upper = match.upper()
            # Only flag if it's an ALTER TABLE with BOOLEAN DEFAULT 0/1
            if 'ALTER TABLE' in match_upper and 'BOOLEAN' in match_upper:
                if 'DEFAULT 0' in match or 'DEFAULT 1' in match:
                    problematic.append(match)

        self.assertEqual(
            len(problematic), 0,
            f"Found SQLite-only boolean defaults in raw ALTER TABLE statements. "
            f"Use add_column_if_not_exists() instead:\n" +
            "\n".join(f"  - {m[:100]}..." if len(m) > 100 else f"  - {m}" for m in problematic)
        )

    def test_reserved_keywords_quoted_in_index_creation(self):
        """
        Ensure reserved keywords like 'user' are properly quoted in index creation.

        Raw SQL like 'CREATE INDEX ... ON user (column)' will fail on some databases
        because 'user' is a reserved keyword. It should be quoted as "user" or use
        the create_index_if_not_exists() utility.
        """
        reserved_keywords = ['user', 'order', 'group', 'table', 'select', 'index']

        problematic = []

        for keyword in reserved_keywords:
            # Pattern to find unquoted reserved keyword after ON in index creation
            # Matches: CREATE INDEX ... ON user ( but not ON "user" or ON `user`
            pattern = rf"CREATE\s+(?:UNIQUE\s+)?INDEX[^;]*\s+ON\s+{keyword}\s*\("

            matches = re.findall(pattern, self.content, re.IGNORECASE)

            for match in matches:
                # Skip if the keyword is already quoted
                if f'"{keyword}"' in match.lower() or f'`{keyword}`' in match.lower():
                    continue
                problematic.append((keyword, match[:80]))

        self.assertEqual(
            len(problematic), 0,
            f"Found unquoted reserved keywords in index creation. "
            f"Use create_index_if_not_exists() or quote the table name:\n" +
            "\n".join(f"  - '{kw}' in: {sql}..." for kw, sql in problematic)
        )

    def test_add_column_uses_utility(self):
        """
        Ensure most ADD COLUMN operations use add_column_if_not_exists().

        Direct ALTER TABLE ADD COLUMN statements should use the utility function
        to ensure cross-database compatibility with boolean defaults and quoting.
        """
        # Count direct ALTER TABLE ADD COLUMN in text() calls
        direct_pattern = r"conn\.execute\s*\(\s*text\s*\(['\"][^'\"]*ALTER\s+TABLE[^'\"]*ADD\s+COLUMN"
        direct_matches = re.findall(direct_pattern, self.content, re.IGNORECASE)

        # Count uses of add_column_if_not_exists
        utility_pattern = r"add_column_if_not_exists\s*\("
        utility_matches = re.findall(utility_pattern, self.content)

        # We expect most ADD COLUMN operations to use the utility
        # Allow some direct usage for special cases (e.g., table recreation)
        # but utility usage should significantly outnumber direct usage
        self.assertGreater(
            len(utility_matches), len(direct_matches),
            f"Found {len(direct_matches)} direct ALTER TABLE ADD COLUMN statements "
            f"vs {len(utility_matches)} add_column_if_not_exists() calls. "
            f"Consider using the utility function for cross-database compatibility."
        )

    def test_create_index_uses_utility_for_user_table(self):
        """
        Ensure index creation on 'user' table uses create_index_if_not_exists().

        The 'user' table name is a reserved keyword that requires special quoting.
        Using create_index_if_not_exists() handles this automatically.
        """
        # Find all index creation on user table
        pattern = r"CREATE\s+(?:UNIQUE\s+)?INDEX[^;]*ON\s+[\"'`]?user[\"'`]?\s*\("

        # Count raw index creation on user table in text() calls
        raw_pattern = r"conn\.execute\s*\(\s*text\s*\(['\"][^'\"]*CREATE\s+(?:UNIQUE\s+)?INDEX[^'\"]*ON\s+[\"'`]?user"
        raw_matches = re.findall(raw_pattern, self.content, re.IGNORECASE)

        # Count uses of create_index_if_not_exists for user table
        utility_pattern = r"create_index_if_not_exists\s*\([^)]*['\"]user['\"]"
        utility_matches = re.findall(utility_pattern, self.content, re.IGNORECASE)

        # All index creation on user table should use the utility
        # (excluding table recreation scenarios which have their own quoting)
        if len(raw_matches) > 0:
            # Check if these are in table recreation blocks (acceptable)
            table_recreation_pattern = r"CREATE\s+TABLE\s+user_new"
            has_table_recreation = re.search(table_recreation_pattern, self.content, re.IGNORECASE)

            if not has_table_recreation or len(raw_matches) > 1:
                self.fail(
                    f"Found {len(raw_matches)} raw CREATE INDEX statements on 'user' table. "
                    f"Use create_index_if_not_exists() for proper quoting of reserved keywords."
                )


if __name__ == '__main__':
    unittest.main()
