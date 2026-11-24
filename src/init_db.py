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
from contextlib import contextmanager
from functools import partial

from sqlalchemy import text, inspect

from src.database import db
from src.models import Recording, TranscriptChunk, SystemSetting
from src.services.embeddings import process_recording_chunks
from src.utils import (
    add_column_if_not_exists,
    create_index_if_not_exists,
    drop_not_null,
    migration_lock,
    run_once,
)
from src.utils.database import (drop_index_if_exists, ensure_migration_ledger,
                                remove_world_access_to_sqlite_files)

# Configuration
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'


def classify_embedding_identifier_state(current_identifier, stored_identifier, legacy_identifier):
    """Decide the embedding-identifier compatibility outcome at startup.

    Pure, side-effect-free decision logic extracted from initialize_database.
    Pre-v0.8.16-alpha instances stored only the bare model name under the
    legacy 'embedding_model_name' key; such a value is by definition a local
    sentence-transformers model, so it is wrapped as 'local::<name>' before
    comparison to avoid a false-positive mismatch warning on first upgrade.

    Returns a 3-tuple (effective_stored, migrated_from_legacy, outcome) where
    outcome is one of:
        'first-run'        - nothing stored yet; record the current identifier.
        'silent-migration' - legacy value matches current; promote to new key.
        'no-change'        - stored identifier already matches current.
        'warn-mismatch'    - stored identifier differs from current.
    """
    migrated_from_legacy = False
    if stored_identifier is None and legacy_identifier:
        stored_identifier = f"local::{legacy_identifier}"
        migrated_from_legacy = True

    if stored_identifier is None:
        outcome = 'first-run'
    elif stored_identifier == current_identifier:
        outcome = 'silent-migration' if migrated_from_legacy else 'no-change'
    else:
        outcome = 'warn-mismatch'

    return stored_identifier, migrated_from_legacy, outcome


def _remove_orphaned_user_new(engine, app):
    """Drop the 'user_new' table stranded by the pre-0.10.5 password migration.

    That migration rebuilt the user table from hand-written DDL. pysqlite does
    not wrap DDL in a transaction, so when the row copy failed the CREATE had
    already been committed; every later startup then aborted on 'table
    user_new already exists' and the migration could never complete (#379).

    The table is only ever dropped once the real one is present and holds at
    least as many rows, so a database left mid-rebuild keeps its only copy of
    the data and gets a warning instead.
    """
    if engine.name != 'sqlite':
        return

    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        if 'user_new' not in tables:
            return

        with engine.connect() as conn:
            orphaned_rows = conn.execute(text('SELECT COUNT(*) FROM user_new')).scalar()
            live_rows = (
                conn.execute(text('SELECT COUNT(*) FROM "user"')).scalar()
                if 'user' in tables else 0
            )

            if live_rows and live_rows >= orphaned_rows:
                conn.execute(text('DROP TABLE user_new'))
                conn.commit()
                app.logger.info("Removed orphaned user_new table left by an interrupted migration")
            else:
                app.logger.warning(
                    "Leaving orphaned user_new table in place: it holds %d row(s) but the "
                    "live user table holds %d. Inspect the database before removing it.",
                    orphaned_rows, live_rows
                )
    except Exception as e:
        app.logger.warning(f"Could not clean up orphaned user_new table: {e}")


def _remove_orphaned_speaker_rows(engine, app):
    """Delete speaker snippets and voice samples whose speaker or recording is gone.

    SQLite does not enforce ON DELETE CASCADE, and before #475 "delete all
    speakers" did not remove these rows itself.
    """
    with engine.begin() as conn:
        snippets = conn.execute(text(
            'DELETE FROM speaker_snippet WHERE speaker_id NOT IN (SELECT id FROM speaker) '
            'OR recording_id NOT IN (SELECT id FROM recording)')).rowcount
        samples = conn.execute(text(
            'DELETE FROM speaker_voice_sample '
            'WHERE speaker_id NOT IN (SELECT id FROM speaker)')).rowcount
    if snippets or samples:
        app.logger.info(f"Removed {snippets} orphaned speaker snippet(s) "
                        f"and {samples} orphaned voice sample(s)")


def _clear_plaintext_account_tokens(engine, app):
    """Remove email verification and password reset tokens stored in plaintext.

    Only the SHA-256 hash of such a token is stored since #693. A token that an
    earlier version stored is the signed itsdangerous token of the emailed
    link, which has dots; a hash has none. Such a token is removed rather than
    hashed in place: a copy of the database made before the upgrade then holds
    no link that still works. The link in the email stops working, and a new
    one can be requested.
    """
    with engine.begin() as conn:
        counts = {column: conn.execute(text(
            f'UPDATE "user" SET {column} = NULL WHERE {column} LIKE :dotted'),
            {'dotted': '%.%'}).rowcount
            for column in ('email_verification_token', 'password_reset_token')}
    if any(counts.values()):
        app.logger.info(f"Removed {counts['email_verification_token']} email verification and "
                        f"{counts['password_reset_token']} password reset token(s) stored in plaintext")


def _clear_ended_session_text(engine, app):
    """Clear the text of the user in recording sessions that have ended.

    finalize_metadata holds the title and notes typed when a browser
    recording was finished, upload_filename the file name of an upload in
    slices. Both stayed in the row after the session ended and after its
    recording was deleted. Cleared here: sessions that are finalized, aborted
    or expired, failed uploads in slices, and failed sessions whose recording
    no longer exists. A failed recorder session whose recording exists keeps
    its metadata, which a retry of the stitch job reads. One statement, and
    a second run finds nothing to change.
    """
    with engine.begin() as conn:
        cleared = conn.execute(text(
            "UPDATE recording_session SET finalize_metadata = NULL, upload_filename = NULL "
            "WHERE (finalize_metadata IS NOT NULL OR upload_filename IS NOT NULL) "
            "AND (status IN ('finalized', 'aborted', 'expired') "
            "OR (status = 'failed' AND (kind = 'sliced_upload' OR finalized_recording_id IS NULL "
            "OR finalized_recording_id NOT IN (SELECT id FROM recording))))")).rowcount
    if cleared:
        app.logger.info(f"Cleared the title, notes or file name in {cleared} ended recording session(s)")


def _clear_stale_session_links(engine, app):
    """Clear recording-session links to recordings that no longer exist.

    Deleting a recording did not clear the link on SQLite, which does not
    apply ON DELETE SET NULL (#505). PostgreSQL already cleared it.
    """
    with engine.begin() as conn:
        cleared = conn.execute(text(
            'UPDATE recording_session SET finalized_recording_id = NULL '
            'WHERE finalized_recording_id IS NOT NULL '
            'AND finalized_recording_id NOT IN (SELECT id FROM recording)')).rowcount
    if cleared:
        app.logger.info(f"Cleared {cleared} recording-session link(s) to deleted recordings")


def _add_folder_group_name_index(engine, app):
    """Make a group folder name unique within its group (#513).

    The table constraint is (name, user_id), so two admins of one group could
    each create a folder with the same name when their requests overlapped.
    The index is added next to that constraint; the table is not rebuilt.
    Folders that already share a name in a group keep the oldest name, and
    the others get a " (2)", " (3)", ... suffix so the index can be created.
    """
    from src.models.organization import Folder
    max_len = Folder.__table__.c.name.type.length
    with engine.begin() as conn:
        rows = conn.execute(text(
            'SELECT id, name, user_id, group_id FROM folder')).fetchall()
        group_names = {(r.name, r.group_id) for r in rows if r.group_id is not None}
        user_names = {(r.name, r.user_id) for r in rows}
        kept = set()
        for row in sorted((r for r in rows if r.group_id is not None), key=lambda r: r.id):
            if (row.name, row.group_id) not in kept:
                kept.add((row.name, row.group_id))
                continue
            n = 2
            while True:
                suffix = f' ({n})'
                candidate = row.name[:max_len - len(suffix)] + suffix
                if (candidate, row.group_id) not in group_names and (candidate, row.user_id) not in user_names:
                    break
                n += 1
            conn.execute(text('UPDATE folder SET name = :name WHERE id = :id'),
                         {'name': candidate, 'id': row.id})
            group_names.add((candidate, row.group_id))
            user_names.add((candidate, row.user_id))
            kept.add((candidate, row.group_id))
            app.logger.warning(
                f"Renamed folder {row.id} in group {row.group_id} to '{candidate}': "
                f"another folder in that group already had its name")
        conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_folder_group_name '
            'ON folder (name, group_id) WHERE group_id IS NOT NULL'))


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
        remove_world_access_to_sqlite_files(engine.url.database, logger=app.logger)

    # Only one process at a time, so the several startup processes cannot
    # interleave and see each other's half-applied schema. Failing to take the
    # lock is not fatal; every migration below is independently idempotent.
    with migration_lock(engine, logger=app.logger):
        try:
            ensure_migration_ledger(engine)
        except Exception as e:
            app.logger.warning(f"Could not create the schema_migrations ledger: {e}")

        _run_migrations(app, engine)


def clear_text_user_limits(engine, logger):
    """Clear a budget or quota that SQLite holds as text (#651); see _run_migrations."""
    if engine.dialect.name == 'sqlite':
        with engine.begin() as conn:
            for column in ('monthly_token_budget', 'monthly_transcription_budget', 'storage_quota_mb'):
                rows = conn.execute(text(
                    f'SELECT id FROM "user" WHERE {column} IS NOT NULL '
                    f"AND typeof({column}) NOT IN ('integer', 'real')"
                )).fetchall()
                if rows:
                    conn.execute(text(
                        f'UPDATE "user" SET {column} = NULL WHERE {column} IS NOT NULL '
                        f"AND typeof({column}) NOT IN ('integer', 'real')"
                    ))
                    logger.warning(
                        "Cleared a %s that was not a number for user(s) %s; it is now no limit",
                        column, ", ".join(str(r[0]) for r in rows))


@contextmanager
def _migration_section(app, failures, name):
    """Contain a failure to one group of migrations instead of all of them.

    The sections in _run_migrations used to share a single try block, so the
    first unguarded failure silently skipped every migration after it and the
    app then served a part-upgraded schema. Each section now fails alone: the
    error is logged with its traceback, recorded for the summary banner, and
    the remaining sections still run.

    Later sections may assume earlier ones succeeded (a column addition, most
    commonly), so a failure can still cascade into further section failures.
    That is acceptable: each is reported, and every section is retried on the
    next startup because migrations are idempotent.
    """
    try:
        yield
    except Exception as e:
        failures.append((name, e))
        app.logger.error("Migration section '%s' failed: %s", name, e)
        app.logger.exception("Section '%s' traceback", name)
        # Discard whatever the failed section left pending in the ORM session.
        # Without this its open transaction keeps SQLite's write lock, so the
        # following sections fail with "database is locked", and its half-built
        # rows get flushed to disk by the next section that commits.
        try:
            db.session.rollback()
        except Exception as rollback_error:
            app.logger.warning(
                "Could not roll back the session after section '%s': %s", name, rollback_error
            )


def unique_upload_idempotency_key(app, engine):
    """One recording per upload idempotency key (#476).

    The first index was not unique, so overlapping retries could store a key
    twice: older duplicates lose their key (the newest keeps replaying), then
    the unique index replaces the old one. NULLs are distinct, so unkeyed
    recordings never clash.
    """
    if 'ux_recording_user_idem' not in {i['name'] for i in inspect(engine).get_indexes('recording')}:
        with engine.begin() as conn:
            dupes = conn.execute(text(
                'SELECT user_id, upload_idempotency_key, MAX(id) FROM recording '
                'WHERE upload_idempotency_key IS NOT NULL '
                'GROUP BY user_id, upload_idempotency_key HAVING COUNT(*) > 1')).fetchall()
            for user_id, key, keep in dupes:
                conn.execute(text(
                    'UPDATE recording SET upload_idempotency_key = NULL '
                    'WHERE user_id = :u AND upload_idempotency_key = :k AND id < :keep'),
                    {'u': user_id, 'k': key, 'keep': keep})
        if dupes:
            app.logger.info(f"Cleared {len(dupes)} duplicated upload idempotency keys")
        create_index_if_not_exists(engine, 'ux_recording_user_idem', 'recording',
                                   'user_id, upload_idempotency_key', unique=True)
    drop_index_if_exists(engine, 'ix_recording_user_idem', 'recording')


def _run_migrations(app, engine):
    """Apply every schema migration. Assumes the migration lock is held."""
    failures = []

    # Every column migration logs itself; binding the engine and the
    # application logger here keeps the 140-odd call sites one line each
    # and keeps the log records under the same logger as before.
    add_column = partial(add_column_if_not_exists, engine, logger=app.logger)

    with _migration_section(app, failures, "core recording and user columns"):
        # Add is_inbox column with default value of 1 (True)
        add_column('recording', 'is_inbox', 'BOOLEAN DEFAULT 1')
        
        # Add is_highlighted column with default value of 0 (False)
        add_column('recording', 'is_highlighted', 'BOOLEAN DEFAULT 0')

        # When the daily ownerless-recording cleanup first found the row
        # with no owner (#572). NULL for every owned recording; a later
        # run deletes the row only once this is old enough.
        add_column('recording', 'ownerless_since', 'DATETIME')

        # Add language preference columns to User table
        add_column('user', 'transcription_language', 'VARCHAR(10)')

        # Add extract_events column to User table
        add_column('user', 'extract_events', 'BOOLEAN DEFAULT 0')
        add_column('user', 'output_language', 'VARCHAR(50)')
        # Per-user timezone for generated text (#412)
        add_column('user', 'timezone', 'VARCHAR(64)')
        add_column('user', 'timezone_mode', "VARCHAR(8) DEFAULT 'auto'")
        add_column('user', 'summary_prompt', 'TEXT')
        add_column('user', 'name', 'VARCHAR(100)')
        add_column('user', 'job_title', 'VARCHAR(100)')
        add_column('user', 'company', 'VARCHAR(100)')
        add_column('user', 'diarize', 'BOOLEAN')
        add_column('user', 'ui_language', "VARCHAR(10) DEFAULT 'en'")
        add_column('user', 'sso_provider', 'VARCHAR(100)')
        add_column('user', 'sso_subject', 'VARCHAR(255)')
        # Time of the last use of the account, for the admin "Last active"
        # column (src/services/last_active.py). NULL for an account that was
        # not used since the upgrade; there is nothing to backfill it from.
        add_column('user', 'last_seen_at', 'DATETIME')
        
        # SSO users authenticate against their provider and never hold a local
        # password, so the column has to accept NULL.
        try:
            if drop_not_null(engine, 'user', 'password'):
                app.logger.info("Made password column nullable for SSO support")
        except Exception as e:
            app.logger.warning(f"Could not migrate password column to nullable (may cause issues with SSO): {e}")

        _remove_orphaned_user_new(engine, app)


    with _migration_section(app, failures, "recording, tag, speaker and processing columns"):
        add_column('recording', 'mime_type', 'VARCHAR(100)')
        # Detected transcription language (#386). Existing rows stay NULL:
        # the audio would have to be re-transcribed to learn it, and the
        # webhook simply omits the field when it is not known.
        add_column('recording', 'transcription_language', 'VARCHAR(20)')
        # Language the user explicitly requested when transcribing (#translation).
        # NULL means auto-detect; existing rows stay NULL because the original
        # request is not recoverable from the reported language.
        add_column('recording', 'transcription_requested_language', 'VARCHAR(20)')
        add_column('recording', 'transcription_translation', 'TEXT')
        add_column('recording', 'transcription_translation_language', 'VARCHAR(50)')
        add_column('recording', 'audio_duration_seconds', 'FLOAT')
        add_column('recording', 'completed_at', 'DATETIME')
        # Change tracking for API clients (mailr spec G2). Filled from
        # completed_at or created_at once; the feed reads NULL as created_at,
        # so a failed backfill is harmless and the next start retries it.
        add_column('recording', 'updated_at', 'DATETIME')
        create_index_if_not_exists(engine, 'ix_recording_updated_at', 'recording', 'updated_at')
        create_index_if_not_exists(engine, 'ix_recording_user_updated', 'recording', 'user_id, updated_at')
        # Admin status counts filter on status alone (#507). Without an index
        # each count scanned the table, transcripts included.
        create_index_if_not_exists(engine, 'ix_recording_status', 'recording', 'status')

        def _backfill_recording_updated_at(engine):
            from datetime import datetime as _dt
            from src.services.recording_changes import FEED_SINCE_KEY, iso_z
            with engine.begin() as conn:
                conn.execute(text(
                    'UPDATE recording SET updated_at = COALESCE(completed_at, created_at) '
                    'WHERE updated_at IS NULL'))
            SystemSetting.set_setting(FEED_SINCE_KEY, iso_z(_dt.utcnow()),
                                      'Start of change tracking (changes feed)')

        run_once(engine, '0002_backfill_recording_updated_at', _backfill_recording_updated_at,
                 logger=app.logger)
        # Public share link expiry (mailr spec G10).
        add_column('share', 'expires_at', 'DATETIME')
        # Speaker contact details (mailr spec G6).
        add_column('speaker', 'email', 'VARCHAR(320)')
        add_column('speaker', 'aliases', 'TEXT')
        add_column('speaker', 'updated_at', 'DATETIME')
        create_index_if_not_exists(engine, 'ix_speaker_user_email', 'speaker', 'user_id, email')
        # Idempotent API uploads (mailr spec G8).
        add_column('recording', 'upload_idempotency_key', 'VARCHAR(100)')
        unique_upload_idempotency_key(app, engine)
        # The request that stores a browser upload claims its row (#626).
        add_column('client_upload', 'claimed_at', 'DATETIME')
        # Chunks are read and deleted by recording, in order (D#441)
        create_index_if_not_exists(engine, 'ix_transcript_chunk_recording', 'transcript_chunk',
                                   'recording_id, chunk_index')
        # The recording lists and the Shared Transcripts list read shares,
        # tag links and per-user states by these columns (D#483)
        for index_name, table_name, columns in (
            ('ix_share_recording_id', 'share', 'recording_id'),
            ('ix_share_user_id', 'share', 'user_id'),
            ('ix_internal_share_shared_with_user_id', 'internal_share', 'shared_with_user_id'),
            ('ix_internal_share_owner_id', 'internal_share', 'owner_id'),
            ('ix_shared_recording_state_user_id', 'shared_recording_state', 'user_id'),
            ('ix_recording_tags_tag_id', 'recording_tags', 'tag_id'),
        ):
            create_index_if_not_exists(engine, index_name, table_name, columns)
        add_column('recording', 'processing_time_seconds', 'INTEGER')
        add_column('recording', 'transcription_duration_seconds', 'INTEGER')
        add_column('recording', 'audio_duration_seconds', 'FLOAT')
        add_column('recording', 'summarization_duration_seconds', 'INTEGER')
        add_column('recording', 'translation_duration_seconds', 'INTEGER')
        add_column('recording', 'processing_source', "VARCHAR(50) DEFAULT 'upload'")
        add_column('recording', 'error_message', 'TEXT')
        add_column('recording', 'keep_audio_only', 'BOOLEAN DEFAULT 0')
            
        # Add columns to recording_tags for order tracking
        add_column('recording_tags', 'added_at', 'DATETIME')
        add_column('recording_tags', 'order', '"order" INTEGER DEFAULT 0')

        # Add auto-deletion and retention columns
        add_column('recording', 'audio_deleted_at', 'DATETIME')
        add_column('recording', 'deletion_exempt', 'BOOLEAN DEFAULT 0')
        add_column('tag', 'protect_from_deletion', 'BOOLEAN DEFAULT 0')

        # Add speaker embeddings column for storing voice embeddings from diarization
        add_column('recording', 'speaker_embeddings', 'JSON')

        # Per-recording prompt-template variables (e.g. {"agenda": "...", "attendees": "..."})
        add_column('recording', 'prompt_variables', 'JSON')

        # Effective transcription hints (hotwords / initial prompt) actually used
        # for a recording, captured at transcription time (issue #309).
        add_column('recording', 'resolved_hotwords', 'TEXT')
        add_column('recording', 'resolved_initial_prompt', 'TEXT')

        # The prompt that produced the current summary, its source and whose
        # run it was (#584). No backfill: older summaries have none.
        for column, column_type in (('summary_prompt_used', 'TEXT'),
                                    ('summary_prompt_source', 'VARCHAR(16)'),
                                    ('summary_prompt_source_name', 'TEXT'),
                                    ('summary_prompt_user_id', 'INTEGER')):
            add_column('recording', column, column_type)

        # Transcription templates now bundle hotwords alongside the prompt (#309)
        add_column('initial_prompt_template', 'hotwords', 'TEXT')

        # Prompt-cache token tracking: cached_tokens (prefix-cache reads) and
        # cache_write_tokens (tokens written to the cache) per usage row.
        add_column('token_usage', 'cached_tokens', 'INTEGER DEFAULT 0')
        add_column('token_usage', 'cache_write_tokens', 'INTEGER DEFAULT 0')

        # Add speaker voice profile embedding fields
        add_column('speaker', 'average_embedding', 'BLOB')
        add_column('speaker', 'embeddings_history', 'JSON')
        add_column('speaker', 'embedding_count', 'INTEGER DEFAULT 0')
        add_column('speaker', 'confidence_score', 'REAL')

        # Add is_new_upload column to processing_job table for tracking upload vs reprocessing jobs
        add_column('processing_job', 'is_new_upload', 'BOOLEAN DEFAULT 0')
        add_column('processing_job', 'provider_task', 'TEXT')
        add_column('processing_job', 'progress_done', 'INTEGER')
        add_column('processing_job', 'progress_total', 'INTEGER')
        # Progress unit, pipeline step, cancel mark and the state a cancel
        # puts back (the processing queue redesign).
        for column, column_type in (('progress_kind', 'VARCHAR(20)'), ('stage', 'VARCHAR(30)'),
                                    ('stage_detail', 'VARCHAR(30)'),
                                    ('cancel_requested_at', 'DATETIME'), ('restore_state', 'TEXT')):
            add_column('processing_job', column, column_type)

        add_column('tag', 'group_id', 'INTEGER')

        add_column('tag', 'retention_days', 'INTEGER')

        # Migrate existing protected tags to use retention_days = -1 for consistency
        # This standardizes the protection mechanism: retention_days = -1 means protected/infinite retention
        try:
            with engine.connect() as conn:
                # Find tags with protect_from_deletion=True but retention_days != -1
                result = conn.execute(text("""
                    SELECT COUNT(*) FROM tag
                    WHERE protect_from_deletion = TRUE
                    AND (retention_days IS NULL OR retention_days != -1)
                """))
                count = result.scalar()

                if count and count > 0:
                    # Migrate these tags to use retention_days = -1
                    conn.execute(text("""
                        UPDATE tag
                        SET retention_days = -1
                        WHERE protect_from_deletion = TRUE
                        AND (retention_days IS NULL OR retention_days != -1)
                    """))
                    conn.commit()
                    app.logger.info(f"Migrated {count} protected tags to use retention_days=-1 (standardized protection format)")
        except Exception as e:
            app.logger.warning(f"Could not migrate protected tags to retention_days=-1: {e}")

        # Auto-process watch folder columns for tags
        add_column('tag', 'is_auto_process', 'BOOLEAN DEFAULT 0')
        add_column('tag', 'auto_process_folder_name', 'VARCHAR(100)')

        add_column('tag', 'auto_share_on_apply', 'BOOLEAN DEFAULT 1')

        add_column('tag', 'share_with_group_lead', 'BOOLEAN DEFAULT 1')

        add_column('user', 'can_share_publicly', 'BOOLEAN DEFAULT 1')

    with _migration_section(app, failures, "user settings columns"):
        # Token budget for rate limiting
        add_column('user', 'monthly_token_budget', 'INTEGER')

        # Transcription budget for rate limiting (in seconds)
        add_column('user', 'monthly_transcription_budget', 'INTEGER')

        # Light/dark mode and color scheme saved with the account (D#419);
        # null until the user changes them
        add_column('user', 'theme_dark_mode', 'BOOLEAN')
        add_column('user', 'theme_color_scheme', 'VARCHAR(16)')
        # UI theme saved with the account (src/config/ui_themes.py); null
        # means the default theme
        add_column('user', 'ui_theme', 'VARCHAR(32)')
        # The assistant's name (src/utils/assistant_name.py); null means
        # the default, Ambrose
        add_column('user', 'assistant_name', 'VARCHAR(40)')

        # Release whose "What's new" notes the user has seen; NULL (existing
        # accounts) shows the current notes once
        add_column('user', 'whats_new_seen', 'VARCHAR(32)')

        # Raised when the password is set, to end the account's sign-ins (#437)
        add_column('user', 'session_version', 'INTEGER')

        # Storage quota in megabytes (#413)
        add_column('user', 'storage_quota_mb', 'INTEGER')

        # Naming templates feature
        add_column('user', 'default_naming_template_id', 'INTEGER')

        # Email notification preference (#386). Defaults off, so an upgrade
        # never starts mailing anyone who did not ask for it.
        add_column('user', 'notify_email_on_completion', 'BOOLEAN DEFAULT 0')

        # Email verification fields
        email_verified_added = add_column('user', 'email_verified', 'BOOLEAN DEFAULT 0')
        if email_verified_added:
            # Set all existing users to email_verified=True (grandfathered)
            try:
                with engine.connect() as conn:
                    conn.execute(text('UPDATE "user" SET email_verified = TRUE WHERE email_verified = FALSE OR email_verified IS NULL'))
                    conn.commit()
                    app.logger.info("Set email_verified=True for all existing users (grandfathered)")
            except Exception as e:
                app.logger.warning(f"Could not update existing users email_verified status: {e}")

        add_column('user', 'email_verification_token', 'VARCHAR(200)')
        add_column('user', 'email_verification_sent_at', 'DATETIME')
        add_column('user', 'password_reset_token', 'VARCHAR(200)')
        add_column('user', 'password_reset_sent_at', 'DATETIME')

        # Auto speaker labelling settings
        add_column('user', 'auto_speaker_labelling', 'BOOLEAN DEFAULT 0')
        add_column('user', 'auto_speaker_labelling_threshold', "VARCHAR(10) DEFAULT 'medium'")

        # Auto summarization setting (per-user, default enabled)
        add_column('user', 'auto_summarization', 'BOOLEAN DEFAULT 1')

        # Auto-translation preference (per-user, default disabled) (#translation)
        add_column('user', 'auto_translation', 'BOOLEAN DEFAULT 0')

        # Transcription hints (hotwords and initial prompt for improving ASR accuracy)
        add_column('user', 'transcription_hotwords', 'TEXT')
        add_column('user', 'transcription_initial_prompt', 'TEXT')

        # Per-feature transcript timestamp settings (#304)
        add_column('user', 'summary_include_timestamps', 'BOOLEAN DEFAULT 0')
        # Notes given to the summarizer (#440), off by default
        add_column('user', 'summary_include_notes', 'BOOLEAN DEFAULT 0')
        add_column('user', 'summary_timestamp_template_id', 'INTEGER')
        add_column('user', 'chat_include_timestamps', 'BOOLEAN DEFAULT 0')
        add_column('user', 'chat_timestamp_template_id', 'INTEGER')

        # UI/display preferences
        add_column('user', 'show_timestamps_simple_view', 'BOOLEAN DEFAULT 0')
        add_column('user', 'editor_autosave', 'BOOLEAN DEFAULT 0')
        add_column('user', 'keyboard_shortcuts', 'BOOLEAN DEFAULT 1')
        add_column('user', 'speaker_count_mode', 'VARCHAR(10)')
        add_column('user', 'audio_player_position', "VARCHAR(10) DEFAULT 'bottom'")
        add_column('user', 'chosen_settings', "TEXT")
        add_column('user', 'default_recording_sort', 'VARCHAR(16)')

        # Filename date parsing (#342)
        add_column('user', 'parse_filename_dates', 'BOOLEAN DEFAULT 0')
        add_column('user', 'filename_date_pattern', "VARCHAR(50) DEFAULT 'auto'")
        add_column('user', 'filename_date_regex', 'VARCHAR(500)')
        add_column('tag', 'default_hotwords', 'TEXT')
        add_column('tag', 'default_initial_prompt', 'TEXT')
        add_column('folder', 'default_hotwords', 'TEXT')
        add_column('folder', 'default_initial_prompt', 'TEXT')

        # Voice matching: embedding space of a recording's embeddings, and the
        # label -> name map that lets renames reach the voice samples. The
        # voice_embedding_space and speaker_voice_sample tables come from
        # create_all().
        add_column('recording', 'speaker_embeddings_space_id', 'INTEGER')
        add_column('recording', 'speaker_label_map', 'JSON')

        # Archive hides a recording from the main list without deleting
        # anything. Per user, like inbox and star (#394).
        add_column('recording', 'is_archived', 'BOOLEAN DEFAULT 0')
        add_column('shared_recording_state', 'is_archived', 'BOOLEAN DEFAULT 0')

        # Configurable AI title instructions at every level (#400)
        for table in ('user', 'tag', 'folder'):
            add_column(table, 'title_prompt', 'TEXT')

    with _migration_section(app, failures, "token indexes, export templates and sharing columns"):
        # Create indexes for token lookups (for faster token verification)
        try:
            if create_index_if_not_exists(engine, 'ix_user_email_verification_token', 'user', 'email_verification_token'):
                app.logger.info("Created index ix_user_email_verification_token on user.email_verification_token")
            if create_index_if_not_exists(engine, 'ix_user_password_reset_token', 'user', 'password_reset_token'):
                app.logger.info("Created index ix_user_password_reset_token on user.password_reset_token")
        except Exception as e:
            app.logger.warning(f"Could not create token indexes: {e}")
        add_column('tag', 'naming_template_id', 'INTEGER')

        # Export template assignments for tags and folders
        add_column('tag', 'export_template_id', 'INTEGER')
        add_column('folder', 'export_template_id', 'INTEGER')

        # Optional transcript template header. NULL renders as before.
        add_column('transcript_template', 'header', 'TEXT')

        # Configurable auto-export filenames (#348)
        add_column('user', 'export_filename_template', 'VARCHAR(500)')

        # Inquire content availability toggles (agentic inquire). Nullable on
        # purpose: NULL means "use the admin env default".
        add_column('user', 'inquire_allow_summaries', 'BOOLEAN')
        add_column('user', 'inquire_allow_notes', 'BOOLEAN')
        add_column('recording', 'export_filename', 'VARCHAR(500)')

        # Add source tracking columns to internal_share table
        add_column('internal_share', 'source_type', "VARCHAR(20) DEFAULT 'manual'")

        add_column('internal_share', 'source_folder_id', 'INTEGER')

        if add_column('internal_share', 'source_tag_id', 'INTEGER'):
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
                                AND (t.auto_share_on_apply = TRUE OR t.share_with_group_lead = TRUE)
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
                            AND (t.auto_share_on_apply = TRUE OR t.share_with_group_lead = TRUE)
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
        add_column('shared_recording_state', 'is_inbox', 'BOOLEAN DEFAULT 1')

        # Handle is_starred -> is_highlighted migration
        inspector = inspect(engine)
        if 'shared_recording_state' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('shared_recording_state')]
            has_is_starred = 'is_starred' in columns
            has_is_highlighted = 'is_highlighted' in columns

            if has_is_starred and not has_is_highlighted:
                # Rename is_starred to is_highlighted by copying data
                try:
                    # Add is_highlighted column using utility (handles PostgreSQL boolean defaults)
                    add_column('shared_recording_state', 'is_highlighted', 'BOOLEAN DEFAULT 0')
                    # Copy data from is_starred to is_highlighted
                    with engine.connect() as conn:
                        conn.execute(text('UPDATE shared_recording_state SET is_highlighted = is_starred'))
                        conn.commit()
                    app.logger.info("Migrated is_starred to is_highlighted in shared_recording_state table")
                    # Note: We keep is_starred for now to avoid breaking existing code during transition
                except Exception as e:
                    app.logger.warning(f"Could not migrate is_starred to is_highlighted: {e}")
            elif not has_is_highlighted:
                # Neither column exists, add is_highlighted
                add_column('shared_recording_state', 'is_highlighted', 'BOOLEAN DEFAULT 0')

    with _migration_section(app, failures, "meeting_date datetime migration"):
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

    with _migration_section(app, failures, "webhook secret rotation grace"):
        # mailr spec W6: recipients' webhooks for shared recordings (opt-in).
        add_column('webhook', 'include_shared', 'BOOLEAN DEFAULT 0')
        # mailr spec W2: the previous secret signs a second V2 value for a while.
        add_column('webhook', 'previous_secret', 'VARCHAR(120)')
        add_column('webhook', 'previous_secret_expires_at', 'DATETIME')

    with _migration_section(app, failures, "notifications"):
        # The table itself comes from create_all(); these cover a database
        # that already had an earlier version of it. Columns are added
        # individually so a partial table is completed rather than rebuilt.
        if create_index_if_not_exists(engine, 'ix_notification_user_id', 'notification', 'user_id'):
            app.logger.info("Added index on notification.user_id")
        if create_index_if_not_exists(engine, 'ix_notification_dedupe_key', 'notification', 'dedupe_key'):
            app.logger.info("Added index on notification.dedupe_key")
        if create_index_if_not_exists(engine, 'ix_notification_resolved_at', 'notification', 'resolved_at'):
            app.logger.info("Added index on notification.resolved_at")

    with _migration_section(app, failures, "sliced file upload sessions"):
        # The model declares kind as NOT NULL with index=True, but both of
        # those only reach a database that create_all() built. An upgraded
        # database gets the column from the ALTER below, so the index has to
        # be created explicitly here or existing installations never get one.
        #
        # The NOT NULL half is deliberately left alone. ADD COLUMN ... DEFAULT
        # backfills every existing row, the ORM always supplies a value, and
        # the default covers a direct insert, so the constraint would buy
        # nothing that is not already true. Adding it to SQLite would mean
        # swapping the column, which is real risk for no gain.
        add_column('recording_session', 'kind', "VARCHAR(20) DEFAULT 'recorder'")
        add_column('recording_session', 'upload_filename', 'VARCHAR(255)')
        add_column('recording_session', 'upload_total_bytes', 'BIGINT')
        if create_index_if_not_exists(engine, 'ix_recording_session_kind', 'recording_session', 'kind'):
            app.logger.info("Added index on recording_session.kind")

    with _migration_section(app, failures, "group creators and permission grants"):
        # Who created each group (#576). Existing groups keep NULL: their
        # creator is not known, so only site admins can delete them. The
        # REFERENCES clause gives an upgraded database the same foreign key
        # as a fresh one. The permission_grant table comes from create_all().
        add_column('group', 'created_by', 'INTEGER REFERENCES "user" (id) ON DELETE SET NULL')
        if create_index_if_not_exists(engine, 'ix_group_created_by', 'group', 'created_by'):
            app.logger.info("Created index ix_group_created_by on group.created_by")

    with _migration_section(app, failures, "performance and uniqueness indexes"):
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
            if create_index_if_not_exists(engine, 'ix_user_sso_subject', 'user', 'sso_subject', unique=True):
                app.logger.info("Created unique index ix_user_sso_subject on user.sso_subject")
        except Exception as e:
            app.logger.warning(f"Could not create unique index on user.sso_subject: {e}")

        # Where a recording's title came from (#412): user-typed titles are
        # kept on reprocess, generated ones are regenerated.
        add_column('recording', 'title_source', 'VARCHAR(16)')

        # Token scopes (mailr spec G1). NULL = full access, so existing tokens
        # keep working exactly as before; no backfill.
        add_column('api_token', 'scopes', 'TEXT')

        # The app that created a token through the SSO token exchange. NULL
        # for every token made in Account settings; no backfill.
        add_column('api_token', 'sso_client_id', 'VARCHAR(255)')

        # Add file_hash column for duplicate detection
        add_column('recording', 'file_hash', 'VARCHAR(64)')
        try:
            if create_index_if_not_exists(engine, 'ix_recording_user_file_hash', 'recording', 'user_id, file_hash'):
                app.logger.info("Created index ix_recording_user_file_hash on recording (user_id, file_hash)")
        except Exception as e:
            app.logger.warning(f"Could not create index on recording (user_id, file_hash): {e}")

        # Composite index that backs the webhook dispatcher's main query:
        # `status IN ('pending','failed') AND next_retry_at <= now`.
        # Without this the dispatcher scans the full webhook_delivery
        # table every poll once a few thousand deliveries accumulate.
        try:
            if create_index_if_not_exists(engine, 'idx_delivery_status_retry', 'webhook_delivery', 'status, next_retry_at'):
                app.logger.info("Created composite index idx_delivery_status_retry on webhook_delivery (status, next_retry_at)")
        except Exception as e:
            app.logger.warning(f"Could not create composite index on webhook_delivery: {e}")

        # Add folder_id column to recording table for folders feature
        add_column('recording', 'folder_id', 'INTEGER')

        # Add default_transcription_model columns to tag and folder for per-tag/per-folder model selection (issue #266)
        add_column('tag', 'default_transcription_model', 'VARCHAR(120)')
        add_column('folder', 'default_transcription_model', 'VARCHAR(120)')

        # Nested folders (#585). Existing folders stay top level (NULL). The
        # REFERENCES clause gives an upgraded database the same foreign key
        # as a fresh one: SQLite and PostgreSQL both accept it in ADD COLUMN
        # for a column without a default. SQLite does not enforce it here,
        # so the Folder before_delete hook promotes children on every
        # backend.
        add_column('folder', 'parent_id', 'INTEGER REFERENCES "folder" (id) ON DELETE SET NULL')
        if create_index_if_not_exists(engine, 'ix_folder_parent_id', 'folder', 'parent_id'):
            app.logger.info("Created index ix_folder_parent_id on folder.parent_id")
        # Create index for folder_id
        try:
            if create_index_if_not_exists(engine, 'ix_recording_folder_id', 'recording', 'folder_id'):
                app.logger.info("Created index ix_recording_folder_id on recording.folder_id")
        except Exception as e:
            app.logger.warning(f"Could not create index on recording.folder_id: {e}")

    with _migration_section(app, failures, "active processing job uniqueness"):
        # One active job per (recording, job_type). enqueue() checks first, but
        # two concurrent requests (two tabs, two summary workers, two app
        # processes) can both pass the check before either inserts, which used
        # to queue duplicate translations and duplicate summaries. The partial
        # unique index makes the check-then-insert atomic at the database.
        # SQLite and PostgreSQL both support partial indexes; other backends
        # fall back to the in-process lock in enqueue().
        if engine.dialect.name in ('sqlite', 'postgresql'):
            try:
                with engine.begin() as conn:
                    dupes = conn.execute(text(
                        "SELECT recording_id, job_type, MAX(id) FROM processing_job "
                        "WHERE status IN ('queued', 'processing') "
                        "GROUP BY recording_id, job_type HAVING COUNT(*) > 1"
                    )).fetchall()
                    for recording_id, job_type, keep_id in dupes:
                        conn.execute(text(
                            "UPDATE processing_job SET status = 'failed', "
                            "completed_at = CURRENT_TIMESTAMP, "
                            "error_message = COALESCE(error_message, '') || "
                            "' (superseded by a concurrent duplicate job)' "
                            "WHERE recording_id = :r AND job_type = :t "
                            "AND status IN ('queued', 'processing') AND id <> :keep"
                        ), {'r': recording_id, 't': job_type, 'keep': keep_id})
                    if dupes:
                        app.logger.warning(
                            "Marked duplicate active processing jobs as failed in %d groups",
                            len(dupes),
                        )
                    conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_processing_job_active_type "
                        "ON processing_job (recording_id, job_type) "
                        "WHERE status IN ('queued', 'processing')"
                    ))
            except Exception as e:
                # Never block startup on this: enqueue() still has its lock and
                # its IntegrityError fallback, and a database that rejects the
                # partial index (an old engine, an exotic backend) keeps the
                # previous best-effort behaviour.
                app.logger.warning(f"Could not create active processing job unique index: {e}")

    with _migration_section(app, failures, "default system settings"):
        # Initialize default system settings
        if not SystemSetting.query.filter_by(key='transcript_length_limit').first():
            SystemSetting.set_setting(
                key='transcript_length_limit',
                value='30000',
                description='Maximum number of characters to send from transcript to LLM for summarization and chat. Use -1 for no limit.',
                setting_type='integer'
            )
            app.logger.info("Initialized default transcript_length_limit setting")
            
        if not SystemSetting.query.filter_by(key='default_storage_quota_mb').first():
            SystemSetting.set_setting(
                key='default_storage_quota_mb',
                value='0',
                description='Storage quota in megabytes (MB) given to new accounts. 0 means no limit. Existing accounts keep their own quota, set in Edit User.',
                setting_type='integer'
            )
            app.logger.info("Initialized default default_storage_quota_mb setting")

        if not SystemSetting.query.filter_by(key='max_file_size_mb').first():
            SystemSetting.set_setting(
                key='max_file_size_mb',
                value='250',
                description='Maximum file size allowed for audio uploads in megabytes (MB).',
                setting_type='integer'
            )
            app.logger.info("Initialized default max_file_size_mb setting")

        if not SystemSetting.query.filter_by(key='login_max_failed_attempts').first():
            SystemSetting.set_setting(
                key='login_max_failed_attempts',
                value='5',
                description='Wrong passwords for one account from one address before password sign-in from that address is locked. Set it to 0 to turn the lock off.',
                setting_type='integer'
            )
            app.logger.info("Initialized default login_max_failed_attempts setting")

        if not SystemSetting.query.filter_by(key='login_lockout_minutes').first():
            SystemSetting.set_setting(
                key='login_lockout_minutes',
                value='15',
                description='Minutes a login lock lasts.',
                setting_type='integer'
            )
            app.logger.info("Initialized default login_lockout_minutes setting")

        if not SystemSetting.query.filter_by(key='max_audio_only_video_size_mb').first():
            # Default to 4x the regular limit so a default deployment lets
            # ~1 GB videos through when only the extracted audio will be
            # kept. The actual transcribed/stored audio is still bounded by
            # max_file_size_mb via the post-extraction size guard.
            try:
                base_mb = int(SystemSetting.get_setting('max_file_size_mb', '250'))
            except (TypeError, ValueError):
                base_mb = 250
            SystemSetting.set_setting(
                key='max_audio_only_video_size_mb',
                value=str(base_mb * 4),
                description='Maximum file size for video uploads in audio-only mode. Only the extracted audio is stored, so the upload itself can be much larger than max_file_size_mb.',
                setting_type='integer'
            )
            app.logger.info("Initialized default max_audio_only_video_size_mb setting")
        
        if not SystemSetting.query.filter_by(key='asr_timeout_seconds').first():
            SystemSetting.set_setting(
                key='asr_timeout_seconds',
                value='1800',
                description='Maximum time in seconds to wait for ASR transcription to complete. Default is 1800 seconds (30 minutes).',
                setting_type='integer'
            )
            app.logger.info("Initialized default asr_timeout_seconds setting")
        
        if not SystemSetting.query.filter_by(key='admin_default_summary_prompt').first():
            from src.config.prompts import DEFAULT_SUMMARY_PROMPT
            SystemSetting.set_setting(
                key='admin_default_summary_prompt',
                value=DEFAULT_SUMMARY_PROMPT,
                description='Default summarization prompt used when users have not set their own prompt. This serves as the base prompt for all users.',
                setting_type='string'
            )
            app.logger.info("Initialized admin_default_summary_prompt setting")

        if not SystemSetting.query.filter_by(key='admin_default_title_prompt').first():
            from src.config.prompts import DEFAULT_TITLE_PROMPT
            SystemSetting.set_setting(
                key='admin_default_title_prompt',
                value=DEFAULT_TITLE_PROMPT,
                description='Default instructions for AI-generated recording titles, used when no tag, folder or user title prompt is set.',
                setting_type='string'
            )
            app.logger.info("Initialized admin_default_title_prompt setting")

        if not SystemSetting.query.filter_by(key='admin_default_contextual_speaker_prompt').first():
            from src.config.prompts import DEFAULT_CONTEXTUAL_SPEAKER_PROMPT
            SystemSetting.set_setting(
                key='admin_default_contextual_speaker_prompt',
                value=DEFAULT_CONTEXTUAL_SPEAKER_PROMPT,
                description='Guidance for contextual speaker labelling, used when a recording has no voice embeddings and speakers are named from the transcript, constrained to the user\'s saved profiles. Only the guidance is editable; the candidate list and JSON format are fixed in code.',
                setting_type='string'
            )
            app.logger.info("Initialized admin_default_contextual_speaker_prompt setting")

        if not SystemSetting.query.filter_by(key='recording_disclaimer').first():
            SystemSetting.set_setting(
                key='recording_disclaimer',
                value='',
                description='Legal disclaimer shown to users before recording starts. Supports Markdown formatting. Leave empty to disable.',
                setting_type='string'
            )
            app.logger.info("Initialized recording_disclaimer setting")

        if not SystemSetting.query.filter_by(key='upload_disclaimer').first():
            SystemSetting.set_setting(
                key='upload_disclaimer',
                value='',
                description='Legal disclaimer shown before file uploads. Supports Markdown. Leave empty to disable.',
                setting_type='string'
            )
            app.logger.info("Initialized upload_disclaimer setting")

        if not SystemSetting.query.filter_by(key='custom_banner').first():
            SystemSetting.set_setting(
                key='custom_banner',
                value='',
                description='Custom banner shown at the top of the page. Supports Markdown. Leave empty to disable.',
                setting_type='string'
            )
            app.logger.info("Initialized custom_banner setting")

        if not SystemSetting.query.filter_by(key='disable_auto_summarization').first():
            SystemSetting.set_setting(
                key='disable_auto_summarization',
                value='false',
                description='Disable automatic summarization after transcription completes. When enabled, recordings will only be transcribed and users must manually trigger summarization.',
                setting_type='boolean'
            )
            app.logger.info("Initialized disable_auto_summarization setting")

        if not SystemSetting.query.filter_by(key='disable_auto_translation').first():
            SystemSetting.set_setting(
                key='disable_auto_translation',
                value='true',
                description='Disable automatic translation after transcription completes. When enabled, recordings will only be transcribed and users must manually trigger translation.',
                setting_type='boolean'
            )
            app.logger.info("Initialized disable_auto_translation setting")

        if not SystemSetting.query.filter_by(key='admin_default_hotwords').first():
            SystemSetting.set_setting(
                key='admin_default_hotwords',
                value='',
                description='Global hotwords injected into all transcription requests. Comma-separated. Applied when users have not set their own hotwords.',
                setting_type='string'
            )
            app.logger.info("Initialized admin_default_hotwords setting")

        if not SystemSetting.query.filter_by(key='admin_default_initial_prompt').first():
            SystemSetting.set_setting(
                key='admin_default_initial_prompt',
                value='',
                description='Global initial prompt injected into all transcription requests. Applied when users have not set their own initial prompt.',
                setting_type='string'
            )
            app.logger.info("Initialized admin_default_initial_prompt setting")

        if not SystemSetting.query.filter_by(key='show_whats_new').first():
            SystemSetting.set_setting(
                key='show_whats_new',
                value='true',
                description="After an update, show each user a short list of the new features once.",
                setting_type='boolean'
            )
            app.logger.info("Initialized show_whats_new setting")

        if not SystemSetting.query.filter_by(key='enable_folders').first():
            SystemSetting.set_setting(
                key='enable_folders',
                value='false',
                description='Enable the Folders feature, allowing users to organize recordings into folders with custom prompts and ASR settings.',
                setting_type='boolean'
            )
            app.logger.info("Initialized enable_folders setting")

        if not SystemSetting.query.filter_by(key='default_timezone').first():
            # Seeded from the legacy TIMEZONE variable (removed in v0.9.0, still in
            # some setups, e.g. the Unraid template) so its intent carries over.
            from src.utils.timezones import is_valid_timezone
            legacy_tz = (os.environ.get('TIMEZONE') or '').strip()
            SystemSetting.set_setting(
                key='default_timezone',
                value=legacy_tz if is_valid_timezone(legacy_tz) else 'UTC',
                description='Timezone for dates in generated titles (naming templates, default titles) for users whose browser has not reported one and who have not chosen one in Account settings.',
                setting_type='string'
            )
            app.logger.info("Initialized default_timezone setting")

        # The mode of each named permission (#576). Admins only by default,
        # so nothing changes until an admin chooses otherwise.
        from src.auth.permissions import PERMISSIONS, MODE_ADMINS
        for _key, _desc in PERMISSIONS.values():
            if not SystemSetting.query.filter_by(key=_key).first():
                SystemSetting.set_setting(key=_key, value=MODE_ADMINS, description=_desc, setting_type='string')
                app.logger.info(f"Initialized {_key} setting")

        # The one-time notice on the Audit log tab of the admin page: shown
        # until an admin dismisses it.
        if not SystemSetting.query.filter_by(key='audit_log_notice_dismissed').first():
            SystemSetting.set_setting(
                key='audit_log_notice_dismissed',
                value='false',
                description='Whether the notice on the Audit log tab was dismissed. The notice states what is recorded and for how long.',
                setting_type='boolean'
            )
            app.logger.info("Initialized audit_log_notice_dismissed setting")

        # Settings that can be changed in System Settings and fall back to
        # .env. The rows have no value, so the .env value applies until an
        # admin saves one (#567).
        from src.utils.system_settings import ENV_BACKED_SETTINGS
        for _key, (_env, _default, _type, _desc) in ENV_BACKED_SETTINGS.items():
            if not SystemSetting.query.filter_by(key=_key).first():
                SystemSetting.set_setting(key=_key, value=None, description=_desc, setting_type=_type)
                app.logger.info(f"Initialized {_key} setting")

    with _migration_section(app, failures, "embedding identifier tracking"):
        # Track the embedding identifier (provider + model) in system_setting
        # so we can warn when either changes between restarts. Issue #262 —
        # old vectors will not match a new model's output dimensionality or
        # semantic space, and inquire mode will silently return wrong results
        # until users reprocess.
        try:
            from src.services.embeddings import EMBEDDING_IDENTIFIER
            current_identifier = EMBEDDING_IDENTIFIER

            raw_stored_identifier = SystemSetting.get_setting('embedding_identifier', None)
            # Backwards-compat path: pre-v0.8.16-alpha instances stored only the
            # bare model name under the legacy 'embedding_model_name' key. Any
            # such value is by definition a local sentence-transformers model,
            # so wrap it in the new 'local::<name>' format before comparing.
            # This prevents a false-positive warning on first upgrade.
            legacy_model_name = SystemSetting.get_setting('embedding_model_name', None)

            stored_identifier, migrated_from_legacy, outcome = classify_embedding_identifier_state(
                current_identifier, raw_stored_identifier, legacy_model_name
            )

            chunk_count = TranscriptChunk.query.filter(TranscriptChunk.embedding.isnot(None)).count()

            if outcome == 'first-run':
                SystemSetting.set_setting(
                    key='embedding_identifier',
                    value=current_identifier,
                    description='Identifier of the embedding provider and model that produced the stored chunk vectors. Used to detect dimensionality and semantic-space mismatches at startup.',
                    setting_type='string',
                )
                if chunk_count:
                    app.logger.info(f"Recorded embedding_identifier={current_identifier} (existing {chunk_count} chunks assumed to match)")
            elif outcome == 'silent-migration':
                # Same configuration, just promote the value into the new key
                # so subsequent restarts skip the legacy lookup.
                SystemSetting.set_setting(
                    key='embedding_identifier',
                    value=current_identifier,
                    description='Identifier of the embedding provider and model that produced the stored chunk vectors. Used to detect dimensionality and semantic-space mismatches at startup.',
                    setting_type='string',
                )
                app.logger.info(
                    f"Migrated legacy embedding_model_name={legacy_model_name!r} to embedding_identifier={current_identifier!r}"
                )
            elif outcome == 'warn-mismatch':
                if chunk_count:
                    app.logger.warning(
                        f"Embedding identifier changed from {stored_identifier!r} to {current_identifier!r} "
                        f"but {chunk_count} chunks were embedded with the previous configuration. "
                        "Inquire mode will return wrong results until you reprocess affected recordings."
                    )
                SystemSetting.set_setting(
                    key='embedding_identifier',
                    value=current_identifier,
                    description='Identifier of the embedding provider and model that produced the stored chunk vectors. Used to detect dimensionality and semantic-space mismatches at startup.',
                    setting_type='string',
                )
                app.logger.info(f"Updated embedding_identifier in system_setting: {stored_identifier!r} -> {current_identifier!r}")
            # outcome == 'no-change': stored value already matches current; nothing to do.
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"embedding_identifier compatibility check skipped: {e}")

    with _migration_section(app, failures, "user limits stored as text"):
        # Before #651 an admin could store a budget or quota such as 'abc'.
        # SQLite keeps text in an INTEGER column, and the admin user list,
        # the overview and the token statistics then fail for every admin.
        # Such a value is cleared (no limit) and the account is logged.
        # PostgreSQL refused the write, so only SQLite can hold one.
        clear_text_user_limits(engine, app.logger)

    with _migration_section(app, failures, "one-shot data cleanups"):
        # One-time email normalization: lowercase (and trim) existing stored
        # emails so they match the normalize-on-write behavior. Login and
        # uniqueness are case-insensitive regardless, so this is cleanup, not a
        # correctness requirement. Collision-aware: if two accounts differ only
        # by case/whitespace, both are left untouched and logged, since merging
        # them is a manual decision. DB-agnostic (operates through the ORM).
        try:
            from collections import defaultdict
            from src.models import User
            users = User.query.all()
            by_norm = defaultdict(list)
            for u in users:
                by_norm[User.normalize_email(u.email)].append(u)
            changed = 0
            for u in users:
                norm = User.normalize_email(u.email)
                if norm == u.email:
                    continue  # already normalized
                if len(by_norm[norm]) > 1:
                    app.logger.warning(
                        f"Skipped email normalization for user {u.id}: multiple "
                        f"accounts map to '{norm}' case-insensitively; resolve manually."
                    )
                    continue
                u.email = norm
                changed += 1
            if changed:
                db.session.commit()
                app.logger.info(f"Normalized {changed} stored email address(es) to lowercase")
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Email normalization migration skipped: {e}")

        # One-shot migration: clean up legacy User.transcription_language values
        # that were stored as display names ("Français", "English") before the
        # account-settings input was a dropdown. Issue #256.
        #
        # Recorded in the ledger rather than re-detected, because detecting it
        # means loading every user on every startup for a fix that can only ever
        # be needed once.
        def _normalize_transcription_languages(_engine):
            from src.utils.language import normalize_language_code
            stale_users = User.query.filter(User.transcription_language.isnot(None)).all()
            cleaned = 0
            for u in stale_users:
                normalized = normalize_language_code(u.transcription_language)
                if normalized != u.transcription_language:
                    app.logger.info(
                        f"Migrating user {u.id} transcription_language: {u.transcription_language!r} -> {normalized!r}"
                    )
                    u.transcription_language = normalized
                    cleaned += 1
            if cleaned:
                db.session.commit()
                app.logger.info(f"Normalized transcription_language for {cleaned} user(s)")

        try:
            run_once(engine, '0001_normalize_transcription_language',
                     _normalize_transcription_languages, logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"transcription_language normalization migration skipped: {e}")

        # Link named transcript segments to saved speakers (speaker_id), once.
        # Voice samples help where they exist; the name match covers the rest.
        try:
            from src.services.speaker_links import backfill_all
            run_once(engine, '0003_link_transcript_speakers',
                     lambda eng: backfill_all(eng, logger=app.logger), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Transcript speaker linking skipped, retried next start: {e}")

        # "Delete all speakers" used to leave the speakers' snippets behind (#475).
        try:
            run_once(engine, '0004_remove_orphaned_speaker_rows',
                     lambda eng: _remove_orphaned_speaker_rows(eng, app), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Orphaned speaker row cleanup skipped, retried next start: {e}")

        # Deleting a recording left its session's link behind on SQLite (#505).
        try:
            run_once(engine, '0005_clear_stale_session_links',
                     lambda eng: _clear_stale_session_links(eng, app), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Recording-session link cleanup skipped, retried next start: {e}")

        # Two admins could create the same folder name in one group (#513).
        try:
            run_once(engine, '0006_folder_group_name_index',
                     lambda eng: _add_folder_group_name_index(eng, app), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Group folder name index skipped, retried next start: {e}")

        # Shares a group tag or folder made outlived the member, the folder
        # link, the tag or the group (#570, #582).
        try:
            from src.services.auto_shares import remove_unfounded_auto_shares
            run_once(engine, '0007_remove_unfounded_auto_shares',
                     lambda eng: remove_unfounded_auto_shares(logger=app.logger), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Stale group share cleanup skipped, retried next start: {e}")

        # Diarization labels (SPEAKER_00) were stored as saved speakers,
        # participants and Inquire chunk speakers. Counts only in the log.
        try:
            from src.utils.generic_speaker_cleanup import remove_generic_speakers
            run_once(engine, '0008_remove_generic_speaker_labels',
                     lambda eng: remove_generic_speakers(eng, logger=app.logger), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Diarization label cleanup skipped, retried next start: {e}")

        # The rows of share_audit_log become share.internal.* rows of the
        # audit log. The old table is left as it is for one release.
        try:
            from src.services.audit_import import copy_share_audit_log
            run_once(engine, '0009_copy_share_audit_log_to_audit_event',
                     lambda eng: copy_share_audit_log(eng, logger=app.logger), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Copy of share_audit_log skipped, retried next start: {e}")

        # Ended recording sessions kept the title and notes typed at the end
        # of a recording, or the file name of an upload in slices.
        try:
            run_once(engine, '0010_clear_ended_session_text',
                     lambda eng: _clear_ended_session_text(eng, app), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Recording-session text cleanup skipped, retried next start: {e}")

        # Email verification and password reset tokens were stored in
        # plaintext (#693); only their hash is stored now.
        try:
            run_once(engine, '0011_clear_plaintext_account_tokens',
                     lambda eng: _clear_plaintext_account_tokens(eng, app), logger=app.logger)
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Plaintext account token cleanup skipped, retried next start: {e}")

    with _migration_section(app, failures, "inquire mode chunk backfill"):
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
                        
                        # A recording without an owner cannot hold chunks (#683).
                        completed_recordings = Recording.query.filter(
                            Recording.status == 'COMPLETED', Recording.user_id.isnot(None)).all()
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
                                        app.logger.info(f"Processed chunks for recording {recording.id}")
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
            
    if failures:
        app.logger.error("=" * 72)
        app.logger.error("DATABASE MIGRATION: %d section(s) failed: %s",
                         len(failures), ", ".join(name for name, _ in failures))
        app.logger.error("Sections after a failed one were still applied, but the schema "
                         "may be partly upgraded.")
        app.logger.error("The application will keep starting, but expect errors until this is fixed.")
        app.logger.error("Please report this with the tracebacks above: "
                         "https://github.com/murtaza-nasir/speakr/issues")
        app.logger.error("=" * 72)
        app.config['MIGRATION_FAILED'] = "; ".join(
            f"{name}: {error}" for name, error in failures)


if __name__ == '__main__':
    # For standalone migration script
    from src.app import app
    with app.app_context():
        initialize_database(app)
