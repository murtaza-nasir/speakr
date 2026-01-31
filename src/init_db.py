"""
Database initialization and migration logic.

This module handles:
- Database schema creation
- Column migrations (adding missing columns to existing tables)
- Default system settings initialization
- Existing recordings migration for inquire mode
"""

import os
import fcntl
import tempfile
from sqlalchemy import text, inspect

from src.database import db
from src.models import Recording, TranscriptChunk, SystemSetting
from src.services.embeddings import process_recording_chunks
from src.utils import add_column_if_not_exists, migrate_column_type

# Configuration
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'


def initialize_database(app):
    """
    Initialize database schema and run migrations.

    This function should be called within an app context.
    """
    db.create_all()

    # Check and add new columns if they don't exist
    engine = db.engine

    # Enable WAL mode for SQLite (better concurrent write performance)
    if engine.name == 'sqlite':
        try:
            with engine.connect() as conn:
                conn.execute(text('PRAGMA journal_mode=WAL'))
                conn.commit()
                app.logger.info("SQLite WAL mode enabled for better concurrency")
        except Exception as e:
            app.logger.warning(f"Could not enable WAL mode: {e}")

    try:
        # Add is_inbox column with default value of 1 (True)
        if add_column_if_not_exists(engine, 'recording', 'is_inbox', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added is_inbox column to recording table")
        
        # Add is_highlighted column with default value of 0 (False)
        if add_column_if_not_exists(engine, 'recording', 'is_highlighted', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added is_highlighted column to recording table")

        # Add language preference columns to User table
        if add_column_if_not_exists(engine, 'user', 'transcription_language', 'VARCHAR(10)'):
            app.logger.info("Added transcription_language column to user table")

        # Add extract_events column to User table
        if add_column_if_not_exists(engine, 'user', 'extract_events', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added extract_events column to user table")
        if add_column_if_not_exists(engine, 'user', 'output_language', 'VARCHAR(50)'):
            app.logger.info("Added output_language column to user table")
        if add_column_if_not_exists(engine, 'user', 'summary_prompt', 'TEXT'):
            app.logger.info("Added summary_prompt column to user table")
        if add_column_if_not_exists(engine, 'user', 'name', 'VARCHAR(100)'):
            app.logger.info("Added name column to user table")
        if add_column_if_not_exists(engine, 'user', 'job_title', 'VARCHAR(100)'):
            app.logger.info("Added job_title column to user table")
        if add_column_if_not_exists(engine, 'user', 'company', 'VARCHAR(100)'):
            app.logger.info("Added company column to user table")
        if add_column_if_not_exists(engine, 'user', 'diarize', 'BOOLEAN'):
            app.logger.info("Added diarize column to user table")
        if add_column_if_not_exists(engine, 'user', 'ui_language', 'VARCHAR(10) DEFAULT "en"'):
            app.logger.info("Added ui_language column to user table")
        if add_column_if_not_exists(engine, 'user', 'sso_provider', 'VARCHAR(100)'):
            app.logger.info("Added sso_provider column to user table")
        if add_column_if_not_exists(engine, 'user', 'sso_subject', 'VARCHAR(255)'):
            app.logger.info("Added sso_subject column to user table")
        
        # Make password column nullable for SSO users
        # SQLite doesn't support ALTER COLUMN, so we need to check and recreate if needed
        try:
            inspector = inspect(engine)
            if 'user' in inspector.get_table_names():
                # Check if password is NOT NULL by querying schema
                with engine.connect() as conn:
                    result = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='user'"))
                    schema = result.scalar()
                    
                    if schema and 'password VARCHAR(60) NOT NULL' in schema:
                        app.logger.info("Migrating user table to make password nullable for SSO support...")
                        
                        # Create new table with nullable password
                        conn.execute(text("""
                            CREATE TABLE user_new (
                                id INTEGER NOT NULL, 
                                username VARCHAR(20) NOT NULL, 
                                email VARCHAR(120) NOT NULL, 
                                password VARCHAR(60), 
                                is_admin BOOLEAN, 
                                can_share_publicly BOOLEAN, 
                                transcription_language VARCHAR(10), 
                                output_language VARCHAR(50), 
                                ui_language VARCHAR(10), 
                                summary_prompt TEXT, 
                                extract_events BOOLEAN, 
                                name VARCHAR(100), 
                                job_title VARCHAR(100), 
                                company VARCHAR(100), 
                                diarize BOOLEAN,
                                sso_provider VARCHAR(100),
                                sso_subject VARCHAR(255),
                                PRIMARY KEY (id), 
                                UNIQUE (username), 
                                UNIQUE (email)
                            )
                        """))
                        
                        # Copy all data from old table to new table
                        conn.execute(text("""
                            INSERT INTO user_new 
                            SELECT id, username, email, password, is_admin, can_share_publicly,
                                   transcription_language, output_language, ui_language, 
                                   summary_prompt, extract_events, name, job_title, company, 
                                   diarize, sso_provider, sso_subject
                            FROM user
                        """))
                        
                        # Drop old table
                        conn.execute(text("DROP TABLE user"))
                        
                        # Rename new table to user
                        conn.execute(text("ALTER TABLE user_new RENAME TO user"))
                        
                        # Recreate the unique index on sso_subject
                        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sso_subject ON user (sso_subject)'))
                        
                        conn.commit()
                        app.logger.info("Successfully made password column nullable for SSO support")
                    else:
                        app.logger.info("Password column is already nullable, skipping migration")
        except Exception as e:
            app.logger.warning(f"Could not migrate password column to nullable (may cause issues with SSO): {e}")
        
        if add_column_if_not_exists(engine, 'recording', 'mime_type', 'VARCHAR(100)'):
            app.logger.info("Added mime_type column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'completed_at', 'DATETIME'):
            app.logger.info("Added completed_at column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'processing_time_seconds', 'INTEGER'):
            app.logger.info("Added processing_time_seconds column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'transcription_duration_seconds', 'INTEGER'):
            app.logger.info("Added transcription_duration_seconds column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'summarization_duration_seconds', 'INTEGER'):
            app.logger.info("Added summarization_duration_seconds column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'processing_source', 'VARCHAR(50) DEFAULT "upload"'):
            app.logger.info("Added processing_source column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'error_message', 'TEXT'):
            app.logger.info("Added error_message column to recording table")
            
        # Add columns to recording_tags for order tracking
        if add_column_if_not_exists(engine, 'recording_tags', 'added_at', 'DATETIME'):
            app.logger.info("Added added_at column to recording_tags table")
        if add_column_if_not_exists(engine, 'recording_tags', 'order', '"order" INTEGER DEFAULT 0'):
            app.logger.info("Added order column to recording_tags table")

        # Add auto-deletion and retention columns
        if add_column_if_not_exists(engine, 'recording', 'audio_deleted_at', 'DATETIME'):
            app.logger.info("Added audio_deleted_at column to recording table")
        if add_column_if_not_exists(engine, 'recording', 'deletion_exempt', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added deletion_exempt column to recording table")
        if add_column_if_not_exists(engine, 'tag', 'protect_from_deletion', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added protect_from_deletion column to tag table")

        # Add speaker embeddings column for storing voice embeddings from diarization
        if add_column_if_not_exists(engine, 'recording', 'speaker_embeddings', 'JSON'):
            app.logger.info("Added speaker_embeddings column to recording table")

        # Add speaker voice profile embedding fields
        if add_column_if_not_exists(engine, 'speaker', 'average_embedding', 'BLOB'):
            app.logger.info("Added average_embedding column to speaker table")
        if add_column_if_not_exists(engine, 'speaker', 'embeddings_history', 'JSON'):
            app.logger.info("Added embeddings_history column to speaker table")
        if add_column_if_not_exists(engine, 'speaker', 'embedding_count', 'INTEGER DEFAULT 0'):
            app.logger.info("Added embedding_count column to speaker table")
        if add_column_if_not_exists(engine, 'speaker', 'confidence_score', 'REAL'):
            app.logger.info("Added confidence_score column to speaker table")

        # Add is_new_upload column to processing_job table for tracking upload vs reprocessing jobs
        if add_column_if_not_exists(engine, 'processing_job', 'is_new_upload', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added is_new_upload column to processing_job table")

        if add_column_if_not_exists(engine, 'tag', 'group_id', 'INTEGER'):
            app.logger.info("Added group_id column to tag table")

        if add_column_if_not_exists(engine, 'tag', 'retention_days', 'INTEGER'):
            app.logger.info("Added retention_days column to tag table")

        # Migrate existing protected tags to use retention_days = -1 for consistency
        # This standardizes the protection mechanism: retention_days = -1 means protected/infinite retention
        try:
            with engine.connect() as conn:
                # Find tags with protect_from_deletion=True but retention_days != -1
                result = conn.execute(text("""
                    SELECT COUNT(*) FROM tag
                    WHERE protect_from_deletion = 1
                    AND (retention_days IS NULL OR retention_days != -1)
                """))
                count = result.scalar()

                if count and count > 0:
                    # Migrate these tags to use retention_days = -1
                    conn.execute(text("""
                        UPDATE tag
                        SET retention_days = -1
                        WHERE protect_from_deletion = 1
                        AND (retention_days IS NULL OR retention_days != -1)
                    """))
                    conn.commit()
                    app.logger.info(f"Migrated {count} protected tags to use retention_days=-1 (standardized protection format)")
        except Exception as e:
            app.logger.warning(f"Could not migrate protected tags to retention_days=-1: {e}")

        if add_column_if_not_exists(engine, 'tag', 'auto_share_on_apply', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added auto_share_on_apply column to tag table")

        if add_column_if_not_exists(engine, 'tag', 'share_with_group_lead', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added share_with_group_lead column to tag table")

        if add_column_if_not_exists(engine, 'user', 'can_share_publicly', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added can_share_publicly column to user table")

        # Token budget for rate limiting
        if add_column_if_not_exists(engine, 'user', 'monthly_token_budget', 'INTEGER'):
            app.logger.info("Added monthly_token_budget column to user table")

        # Transcription budget for rate limiting (in seconds)
        if add_column_if_not_exists(engine, 'user', 'monthly_transcription_budget', 'INTEGER'):
            app.logger.info("Added monthly_transcription_budget column to user table")

        # Naming templates feature
        if add_column_if_not_exists(engine, 'user', 'default_naming_template_id', 'INTEGER'):
            app.logger.info("Added default_naming_template_id column to user table")

        # Email verification fields
        email_verified_added = add_column_if_not_exists(engine, 'user', 'email_verified', 'BOOLEAN DEFAULT 0')
        if email_verified_added:
            app.logger.info("Added email_verified column to user table")
            # Set all existing users to email_verified=True (grandfathered)
            try:
                with engine.connect() as conn:
                    conn.execute(text('UPDATE user SET email_verified = 1 WHERE email_verified = 0 OR email_verified IS NULL'))
                    conn.commit()
                    app.logger.info("Set email_verified=True for all existing users (grandfathered)")
            except Exception as e:
                app.logger.warning(f"Could not update existing users email_verified status: {e}")

        if add_column_if_not_exists(engine, 'user', 'email_verification_token', 'VARCHAR(200)'):
            app.logger.info("Added email_verification_token column to user table")
        if add_column_if_not_exists(engine, 'user', 'email_verification_sent_at', 'DATETIME'):
            app.logger.info("Added email_verification_sent_at column to user table")
        if add_column_if_not_exists(engine, 'user', 'password_reset_token', 'VARCHAR(200)'):
            app.logger.info("Added password_reset_token column to user table")
        if add_column_if_not_exists(engine, 'user', 'password_reset_sent_at', 'DATETIME'):
            app.logger.info("Added password_reset_sent_at column to user table")

        # Auto speaker labelling settings
        if add_column_if_not_exists(engine, 'user', 'auto_speaker_labelling', 'BOOLEAN DEFAULT 0'):
            app.logger.info("Added auto_speaker_labelling column to user table")
        if add_column_if_not_exists(engine, 'user', 'auto_speaker_labelling_threshold', 'VARCHAR(10) DEFAULT "medium"'):
            app.logger.info("Added auto_speaker_labelling_threshold column to user table")

        # Auto summarization setting (per-user, default enabled)
        if add_column_if_not_exists(engine, 'user', 'auto_summarization', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added auto_summarization column to user table")

        # Create indexes for token lookups (for faster token verification)
        try:
            inspector = inspect(engine)
            if 'user' in inspector.get_table_names():
                existing_indexes = [idx['name'] for idx in inspector.get_indexes('user')]
                if 'ix_user_email_verification_token' not in existing_indexes:
                    with engine.connect() as conn:
                        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_user_email_verification_token ON user (email_verification_token)'))
                        conn.commit()
                        app.logger.info("Created index ix_user_email_verification_token on user.email_verification_token")
                if 'ix_user_password_reset_token' not in existing_indexes:
                    with engine.connect() as conn:
                        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_user_password_reset_token ON user (password_reset_token)'))
                        conn.commit()
                        app.logger.info("Created index ix_user_password_reset_token on user.password_reset_token")
        except Exception as e:
            app.logger.warning(f"Could not create token indexes: {e}")
        if add_column_if_not_exists(engine, 'tag', 'naming_template_id', 'INTEGER'):
            app.logger.info("Added naming_template_id column to tag table")

        # Add source tracking columns to internal_share table
        if add_column_if_not_exists(engine, 'internal_share', 'source_type', 'VARCHAR(20) DEFAULT "manual"'):
            app.logger.info("Added source_type column to internal_share table")

        if add_column_if_not_exists(engine, 'internal_share', 'source_tag_id', 'INTEGER'):
            app.logger.info("Added source_tag_id column to internal_share table")

            # Migrate existing shares: infer source based on group tag presence
            try:
                with engine.connect() as conn:
                    # For each existing share, check if it was likely created by a group tag
                    # by looking for group tags on the recording where the shared user is a group member
                    result = conn.execute(text('''
                        UPDATE internal_share
                        SET source_type = 'group_tag',
                            source_tag_id = (
                                SELECT t.id FROM tag t
                                INNER JOIN recording_tags rt ON rt.tag_id = t.id
                                INNER JOIN group_membership gm ON gm.group_id = t.group_id
                                WHERE rt.recording_id = internal_share.recording_id
                                AND gm.user_id = internal_share.shared_with_user_id
                                AND t.group_id IS NOT NULL
                                AND (t.auto_share_on_apply = 1 OR t.share_with_group_lead = 1)
                                LIMIT 1
                            )
                        WHERE source_type = 'manual'
                        AND EXISTS (
                            SELECT 1 FROM tag t
                            INNER JOIN recording_tags rt ON rt.tag_id = t.id
                            INNER JOIN group_membership gm ON gm.group_id = t.group_id
                            WHERE rt.recording_id = internal_share.recording_id
                            AND gm.user_id = internal_share.shared_with_user_id
                            AND t.group_id IS NOT NULL
                            AND (t.auto_share_on_apply = 1 OR t.share_with_group_lead = 1)
                        )
                    '''))
                    conn.commit()
                    app.logger.info("Inferred source tracking for existing shares based on group tag presence")
            except Exception as e:
                app.logger.warning(f"Could not infer source tracking for existing shares: {e}")

            # Update existing records to have proper order values (approximate by tag_id)
            try:
                with engine.connect() as conn:
                    # Get existing associations without order values and assign them
                    existing_associations = conn.execute(text('''
                        SELECT recording_id, tag_id, 
                               ROW_NUMBER() OVER (PARTITION BY recording_id ORDER BY tag_id) as row_num
                        FROM recording_tags 
                        WHERE "order" = 0
                    ''')).fetchall()
                    
                    for assoc in existing_associations:
                        conn.execute(text('''
                            UPDATE recording_tags 
                            SET "order" = :order_num 
                            WHERE recording_id = :rec_id AND tag_id = :tag_id
                        '''), {"order_num": assoc.row_num, "rec_id": assoc.recording_id, "tag_id": assoc.tag_id})
                    
                    conn.commit()
                    app.logger.info(f"Updated order values for {len(existing_associations)} existing tag associations")
            except Exception as e:
                app.logger.warning(f"Could not update existing tag order values: {e}")

        # Add per-user status columns to shared_recording_state table
        if add_column_if_not_exists(engine, 'shared_recording_state', 'is_inbox', 'BOOLEAN DEFAULT 1'):
            app.logger.info("Added is_inbox column to shared_recording_state table")

        # Handle is_starred -> is_highlighted migration
        inspector = inspect(engine)
        if 'shared_recording_state' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('shared_recording_state')]
            has_is_starred = 'is_starred' in columns
            has_is_highlighted = 'is_highlighted' in columns

            if has_is_starred and not has_is_highlighted:
                # Rename is_starred to is_highlighted by copying data
                try:
                    with engine.connect() as conn:
                        # Add is_highlighted column (PostgreSQL needs FALSE, SQLite uses 0)
                        default_val = 'FALSE' if engine.name == 'postgresql' else '0'
                        conn.execute(text(f'ALTER TABLE shared_recording_state ADD COLUMN is_highlighted BOOLEAN DEFAULT {default_val}'))
                        # Copy data from is_starred to is_highlighted
                        conn.execute(text('UPDATE shared_recording_state SET is_highlighted = is_starred'))
                        conn.commit()
                        app.logger.info("Migrated is_starred to is_highlighted in shared_recording_state table")
                        # Note: We keep is_starred for now to avoid breaking existing code during transition
                except Exception as e:
                    app.logger.warning(f"Could not migrate is_starred to is_highlighted: {e}")
            elif not has_is_highlighted:
                # Neither column exists, add is_highlighted
                if add_column_if_not_exists(engine, 'shared_recording_state', 'is_highlighted', 'BOOLEAN DEFAULT 0'):
                    app.logger.info("Added is_highlighted column to shared_recording_state table")

        # Migrate meeting_date from DATE to DATETIME format
        # This migration handles both:
        # 1. Converting existing DATE columns to DATETIME (for fresh pulls)
        # 2. Restoring NULL dates from created_at (for failed migrations)
        try:
            inspector = inspect(engine)
            columns_info = {col['name']: col for col in inspector.get_columns('recording')}

            if 'meeting_date' in columns_info:
                col_type = str(columns_info['meeting_date']['type']).upper()

                # Check if column needs migration from DATE to DATETIME
                needs_migration = False

                # For SQLite: Both DATE and DATETIME are TEXT, check data format
                if engine.name == 'sqlite':
                    with engine.connect() as conn:
                        # Check if we have date-only format (no time component)
                        result = conn.execute(text("""
                            SELECT meeting_date FROM recording
                            WHERE meeting_date IS NOT NULL
                            AND meeting_date NOT LIKE '%:%'
                            LIMIT 1
                        """))
                        has_date_only = result.fetchone() is not None
                        needs_migration = has_date_only

                # For PostgreSQL/MySQL: Check actual column type
                elif 'DATE' in col_type and 'DATETIME' not in col_type and 'TIMESTAMP' not in col_type:
                    needs_migration = True

                if needs_migration:
                    app.logger.info(f"Migrating meeting_date from DATE to DATETIME format (engine: {engine.name})")

                    with engine.connect() as conn:
                        if engine.name == 'sqlite':
                            # SQLite: Add time component to date-only values
                            conn.execute(text("""
                                UPDATE recording
                                SET meeting_date = datetime(date(meeting_date) || ' 12:00:00')
                                WHERE meeting_date IS NOT NULL
                                AND meeting_date NOT LIKE '%:%'
                            """))
                            conn.commit()
                            app.logger.info("Migrated SQLite meeting_date to include time")

                        elif engine.name == 'postgresql':
                            # PostgreSQL: Change column type
                            conn.execute(text("""
                                ALTER TABLE recording
                                ALTER COLUMN meeting_date TYPE TIMESTAMP
                                USING (meeting_date + TIME '12:00:00')
                            """))
                            conn.commit()
                            app.logger.info("Migrated PostgreSQL meeting_date to TIMESTAMP")

                        elif engine.name == 'mysql':
                            # MySQL: Change column type
                            conn.execute(text("""
                                ALTER TABLE recording
                                MODIFY COLUMN meeting_date DATETIME
                            """))
                            # Add time component to existing date values
                            conn.execute(text("""
                                UPDATE recording
                                SET meeting_date = TIMESTAMP(meeting_date, '12:00:00')
                                WHERE meeting_date IS NOT NULL
                            """))
                            conn.commit()
                            app.logger.info("Migrated MySQL meeting_date to DATETIME")
                else:
                    app.logger.info("meeting_date already in DATETIME format, skipping migration")

                # Safety net: Restore any NULL meeting_dates from created_at
                with engine.connect() as conn:
                    result = conn.execute(text("""
                        SELECT COUNT(*) FROM recording
                        WHERE meeting_date IS NULL AND created_at IS NOT NULL
                    """))
                    null_count = result.scalar()

                    if null_count and null_count > 0:
                        conn.execute(text("""
                            UPDATE recording
                            SET meeting_date = created_at
                            WHERE meeting_date IS NULL AND created_at IS NOT NULL
                        """))
                        conn.commit()
                        app.logger.info(f"Restored {null_count} NULL meeting dates from created_at")

        except Exception as e:
            app.logger.warning(f"Error during meeting_date migration: {e}")
            app.logger.warning("New recordings will work correctly, but existing dates may need manual migration")

        # Add index on TranscriptChunk.speaker_name for performance
        # This improves speaker rename operations which update all chunks
        try:
            inspector = inspect(engine)
            if 'transcript_chunk' in inspector.get_table_names():
                existing_indexes = [idx['name'] for idx in inspector.get_indexes('transcript_chunk')]

                # Create composite index on (user_id, speaker_name) if it doesn't exist
                if 'idx_user_speaker_name' not in existing_indexes:
                    with engine.connect() as conn:
                        conn.execute(text(
                            'CREATE INDEX IF NOT EXISTS idx_user_speaker_name ON transcript_chunk (user_id, speaker_name)'
                        ))
                        conn.commit()
                        app.logger.info("Created index idx_user_speaker_name on transcript_chunk (user_id, speaker_name) for speaker rename performance")

                # Create single-column index on speaker_name if it doesn't exist
                if 'ix_transcript_chunk_speaker_name' not in existing_indexes:
                    with engine.connect() as conn:
                        conn.execute(text(
                            'CREATE INDEX IF NOT EXISTS ix_transcript_chunk_speaker_name ON transcript_chunk (speaker_name)'
                        ))
                        conn.commit()
                        app.logger.info("Created index ix_transcript_chunk_speaker_name on transcript_chunk (speaker_name)")
        except Exception as e:
            app.logger.warning(f"Could not create speaker_name indexes: {e}")

        # Add unique index for SSO subject to prevent duplicate linking
        try:
            inspector = inspect(engine)
            if 'user' in inspector.get_table_names():
                existing_indexes = [idx['name'] for idx in inspector.get_indexes('user')]
                if 'ix_user_sso_subject' not in existing_indexes:
                    with engine.connect() as conn:
                        if engine.name == 'mysql':
                            conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sso_subject ON `user` (sso_subject)'))
                        else:
                            conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sso_subject ON "user" (sso_subject)'))
                        conn.commit()
                        app.logger.info("Created unique index ix_user_sso_subject on user.sso_subject")
        except Exception as e:
            app.logger.warning(f"Could not create unique index on user.sso_subject: {e}")

        # Add folder_id column to recording table for folders feature
        if add_column_if_not_exists(engine, 'recording', 'folder_id', 'INTEGER'):
            app.logger.info("Added folder_id column to recording table")
            # Create index for folder_id
            try:
                with engine.connect() as conn:
                    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_recording_folder_id ON recording (folder_id)'))
                    conn.commit()
                    app.logger.info("Created index ix_recording_folder_id on recording.folder_id")
            except Exception as e:
                app.logger.warning(f"Could not create index on recording.folder_id: {e}")

        # Initialize default system settings
        if not SystemSetting.query.filter_by(key='transcript_length_limit').first():
            SystemSetting.set_setting(
                key='transcript_length_limit',
                value='30000',
                description='Maximum number of characters to send from transcript to LLM for summarization and chat. Use -1 for no limit.',
                setting_type='integer'
            )
            app.logger.info("Initialized default transcript_length_limit setting")
            
        if not SystemSetting.query.filter_by(key='max_file_size_mb').first():
            SystemSetting.set_setting(
                key='max_file_size_mb',
                value='250',
                description='Maximum file size allowed for audio uploads in megabytes (MB).',
                setting_type='integer'
            )
            app.logger.info("Initialized default max_file_size_mb setting")
        
        if not SystemSetting.query.filter_by(key='asr_timeout_seconds').first():
            SystemSetting.set_setting(
                key='asr_timeout_seconds',
                value='1800',
                description='Maximum time in seconds to wait for ASR transcription to complete. Default is 1800 seconds (30 minutes).',
                setting_type='integer'
            )
            app.logger.info("Initialized default asr_timeout_seconds setting")
        
        if not SystemSetting.query.filter_by(key='admin_default_summary_prompt').first():
            default_prompt = """Generate a comprehensive summary that includes the following sections:
- **Key Issues Discussed**: A bulleted list of the main topics
- **Key Decisions Made**: A bulleted list of any decisions reached
- **Action Items**: A bulleted list of tasks assigned, including who is responsible if mentioned"""
            SystemSetting.set_setting(
                key='admin_default_summary_prompt',
                value=default_prompt,
                description='Default summarization prompt used when users have not set their own prompt. This serves as the base prompt for all users.',
                setting_type='string'
            )
            app.logger.info("Initialized admin_default_summary_prompt setting")
        
        if not SystemSetting.query.filter_by(key='recording_disclaimer').first():
            SystemSetting.set_setting(
                key='recording_disclaimer',
                value='',
                description='Legal disclaimer shown to users before recording starts. Supports Markdown formatting. Leave empty to disable.',
                setting_type='string'
            )
            app.logger.info("Initialized recording_disclaimer setting")

        if not SystemSetting.query.filter_by(key='disable_auto_summarization').first():
            SystemSetting.set_setting(
                key='disable_auto_summarization',
                value='false',
                description='Disable automatic summarization after transcription completes. When enabled, recordings will only be transcribed and users must manually trigger summarization.',
                setting_type='boolean'
            )
            app.logger.info("Initialized disable_auto_summarization setting")

        if not SystemSetting.query.filter_by(key='enable_folders').first():
            SystemSetting.set_setting(
                key='enable_folders',
                value='false',
                description='Enable the Folders feature, allowing users to organize recordings into folders with custom prompts and ASR settings.',
                setting_type='boolean'
            )
            app.logger.info("Initialized enable_folders setting")

        # Process existing recordings for inquire mode (chunk and embed them)
        # Only run if inquire mode is enabled
        if ENABLE_INQUIRE_MODE:
            # Use a file lock to prevent multiple workers from running this simultaneously
            lock_file_path = os.path.join(tempfile.gettempdir(), 'inquire_migration.lock')
            
            try:
                with open(lock_file_path, 'w') as lock_file:
                    # Try to acquire exclusive lock (non-blocking)
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        app.logger.info("Acquired migration lock, checking for existing recordings that need chunking for inquire mode...")
                        
                        completed_recordings = Recording.query.filter_by(status='COMPLETED').all()
                        recordings_needing_processing = []
                        
                        for recording in completed_recordings:
                            if recording.transcription:  # Has transcription
                                chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                                if chunk_count == 0:  # No chunks yet
                                    recordings_needing_processing.append(recording)
                        
                        if recordings_needing_processing:
                            app.logger.info(f"Found {len(recordings_needing_processing)} recordings that need chunking for inquire mode")
                            app.logger.info("Processing first 10 recordings automatically. Use admin API or migration script for remaining recordings.")
                            
                            # Process first 10 recordings automatically to avoid long startup times
                            batch_size = min(10, len(recordings_needing_processing))
                            processed = 0
                            
                            for i in range(batch_size):
                                recording = recordings_needing_processing[i]
                                try:
                                    success = process_recording_chunks(recording.id)
                                    if success:
                                        processed += 1
                                        app.logger.info(f"Processed chunks for recording: {recording.title} ({recording.id})")
                                except Exception as e:
                                    app.logger.warning(f"Failed to process chunks for recording {recording.id}: {e}")
                            
                            remaining = len(recordings_needing_processing) - processed
                            if remaining > 0:
                                app.logger.info(f"Successfully processed {processed} recordings. {remaining} recordings remaining.")
                                app.logger.info("Use the admin migration API or run 'python migrate_existing_recordings.py' to process remaining recordings.")
                            else:
                                app.logger.info(f"Successfully processed all {processed} recordings for inquire mode.")
                        else:
                            app.logger.info("All existing recordings are already processed for inquire mode.")
                        
                    except BlockingIOError:
                        app.logger.info("Migration already running in another worker, skipping...")
                    
            except Exception as e:
                app.logger.warning(f"Error during existing recordings migration: {e}")
                app.logger.info("Existing recordings can be migrated later using the admin API or migration script.")
            
    except Exception as e:
        app.logger.error(f"Error during database migration: {e}")


if __name__ == '__main__':
    # For standalone migration script
    from src.app import app
    with app.app_context():
        initialize_database(app)
