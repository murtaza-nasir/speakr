#!/usr/bin/env python3
"""
Migration script to rename team tables to group tables.
This handles the refactoring from team-based to group-based terminology.
"""
import sys
import os

# Add the parent directory to the path to import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.app import app, db
from sqlalchemy import text

def migrate_tables():
    """Copy data from team tables to group tables and remove old tables."""
    with app.app_context():
        try:
            # Check if old tables exist
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()

            print("Existing tables:", existing_tables)

            # Check if we need to migrate data
            if 'team' in existing_tables and 'group' in existing_tables:
                # Both tables exist - need to copy data
                print("\nBoth 'team' and 'group' tables exist. Copying data...")

                # Check if there's data in the old table
                result = db.session.execute(text('SELECT COUNT(*) FROM team'))
                old_count = result.scalar()
                print(f"Found {old_count} records in 'team' table")

                if old_count > 0:
                    # Copy data from team to group
                    print("Copying data from 'team' to 'group'...")
                    db.session.execute(text(
                        'INSERT INTO "group" (id, name, description, created_at) '
                        'SELECT id, name, description, created_at FROM team'
                    ))
                    db.session.commit()
                    print(f"✓ Copied {old_count} records to 'group' table")

                # Drop the old team table
                print("Dropping old 'team' table...")
                db.session.execute(text('DROP TABLE team'))
                db.session.commit()
                print("✓ Dropped old 'team' table")

            elif 'team' in existing_tables and 'group' not in existing_tables:
                # Only old table exists - rename it
                print("\nRenaming 'team' table to 'group'...")
                db.session.execute(text('ALTER TABLE team RENAME TO "group"'))
                db.session.commit()
                print("✓ Renamed 'team' to 'group'")
            else:
                print("\n'team' table not found or already migrated")

            # Migrate team_membership
            if 'team_membership' in existing_tables and 'group_membership' in existing_tables:
                # Both tables exist - need to copy data
                print("\nBoth 'team_membership' and 'group_membership' tables exist. Copying data...")

                # Check if there's data in the old table
                result = db.session.execute(text('SELECT COUNT(*) FROM team_membership'))
                old_count = result.scalar()
                print(f"Found {old_count} records in 'team_membership' table")

                if old_count > 0:
                    # Copy data from team_membership to group_membership
                    print("Copying data from 'team_membership' to 'group_membership'...")
                    db.session.execute(text(
                        'INSERT INTO group_membership (id, user_id, group_id, role, joined_at) '
                        'SELECT id, user_id, team_id, role, joined_at FROM team_membership'
                    ))
                    db.session.commit()
                    print(f"✓ Copied {old_count} records to 'group_membership' table")

                # Drop the old team_membership table
                print("Dropping old 'team_membership' table...")
                db.session.execute(text('DROP TABLE team_membership'))
                db.session.commit()
                print("✓ Dropped old 'team_membership' table")

            elif 'team_membership' in existing_tables and 'group_membership' not in existing_tables:
                # Only old table exists - rename it
                print("\nRenaming 'team_membership' table to 'group_membership'...")
                db.session.execute(text('ALTER TABLE team_membership RENAME TO group_membership'))
                db.session.commit()
                print("✓ Renamed 'team_membership' to 'group_membership'")
            else:
                print("\n'team_membership' table not found or already migrated")

            # Migrate team_id to group_id in tags table
            print("\nMigrating tag associations from team_id to group_id...")
            result = db.session.execute(text(
                'UPDATE tag SET group_id = team_id WHERE team_id IS NOT NULL AND group_id IS NULL'
            ))
            db.session.commit()
            print(f"✓ Migrated {result.rowcount} tag associations")

            # Migrate share_with_team_lead to share_with_group_lead in tags
            result = db.session.execute(text(
                'UPDATE tag SET share_with_group_lead = share_with_team_lead WHERE share_with_team_lead IS NOT NULL AND share_with_group_lead IS NULL'
            ))
            db.session.commit()
            print(f"✓ Migrated {result.rowcount} share_with_lead settings")

            print("\n✅ Migration completed successfully!")
            print("\nPlease restart the application for changes to take full effect.")

        except Exception as e:
            print(f"\n❌ Error during migration: {e}")
            db.session.rollback()
            sys.exit(1)

if __name__ == '__main__':
    print("=" * 60)
    print("Team to Group Migration Script")
    print("=" * 60)
    print("\nThis script will rename database tables:")
    print("  - 'team' → 'group'")
    print("  - 'team_membership' → 'group_membership'")

    # Check for --yes flag to skip confirmation
    if '--yes' in sys.argv or '-y' in sys.argv:
        print("\nAuto-confirming migration (--yes flag detected)...\n")
    else:
        print("\nPress Ctrl+C to cancel, or Enter to continue...")
        try:
            input()
        except KeyboardInterrupt:
            print("\n\nMigration cancelled.")
            sys.exit(0)

    migrate_tables()
