"""Boot the current migrations against databases created by older Speakr versions.

Speakr is self-hosted, so people upgrade from whatever version they happened to
stop at, sometimes years old. Every other test in this suite runs against a
database that `db.create_all()` just built from today's models, which is the one
shape no upgrading user ever has. This module covers the gap.

The fixtures in tests/fixtures/schemas/ are real schemas, dumped from each tag's
own models by scripts/generate_schema_fixtures.py. They are checked in as plain
SQL so the tests need no old dependencies, no network and no Docker, and cannot
drift as the tooling changes.

The assertions are deliberately about what a *bad* migration takes away rather
than what a good one adds. #379 shipped a migration that did its stated job
perfectly while destroying 27 unrelated columns; a test that only checked the
intended change would have passed.
"""

import os
import sqlite3

import pytest
from flask import Flask
from sqlalchemy import inspect

from src.database import db
from src.init_db import initialize_database
from src.models import User


FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "schemas")
FIXTURES = sorted(f for f in os.listdir(FIXTURE_DIR) if f.endswith(".sql"))

# Columns whose values must survive an upgrade. Seeded only where the fixture
# already has them, so one list covers every version.
SKIP_SEED = {"id", "username", "email", "password"}

# Some columns are validated or normalised by their own one-shot migrations, so
# a nonsense marker would legitimately be rewritten. Seed those with values a
# real installation would hold.
REALISTIC = {
    "transcription_language": "en",
    "ui_language": "en",
    "output_language": "English",
    "auto_speaker_labelling_threshold": "medium",
    "audio_player_position": "bottom",
    "filename_date_pattern": "auto",
    "speaker_count_mode": "range",
}


def _fixture_app(db_path):
    """A second Flask app bound to the fixture database.

    Flask-SQLAlchemy supports one db instance across several apps, so this runs
    the real initialize_database() against an arbitrary file without disturbing
    the suite-wide app that conftest configured.
    """
    app = Flask(f"upgrade_test_{os.path.basename(db_path)}")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _seed(db_path):
    """Populate the old database the way a real installation would be."""
    con = sqlite3.connect(db_path)
    columns = {row[1]: row[2].upper() for row in con.execute('PRAGMA table_info("user")')}

    con.execute(
        'INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
        ("existing", "existing@example.test", "bcrypt-hash-preserved"),
    )

    seeded = {}
    for name, sql_type in columns.items():
        if name in SKIP_SEED or name.endswith("_id"):
            continue
        if name in REALISTIC:
            value = REALISTIC[name]
        elif "CHAR" in sql_type or "TEXT" in sql_type:
            value = f"keep-{name}"
        elif "INT" in sql_type or "BOOL" in sql_type or "FLOAT" in sql_type:
            value = 7
        else:
            continue
        con.execute(f'UPDATE "user" SET "{name}" = ? WHERE id = 1', (value,))
        seeded[name] = value

    con.commit()
    snapshot = {
        "columns": set(columns),
        "indexes": {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user'"
            )
        },
        "seeded": seeded,
    }
    con.close()
    return snapshot


@pytest.fixture(params=FIXTURES, ids=[f[:-4] for f in FIXTURES])
def upgraded(request, tmp_path):
    """An old database, seeded, then upgraded by the current migrations."""
    version = request.param[:-4]
    db_path = str(tmp_path / f"{version}.db")

    with open(os.path.join(FIXTURE_DIR, request.param)) as fh:
        schema_sql = fh.read()
    con = sqlite3.connect(db_path)
    con.executescript(schema_sql)
    con.commit()
    con.close()

    snapshot = _seed(db_path)

    app = _fixture_app(db_path)
    with app.app_context():
        initialize_database(app)

    return {"version": version, "path": db_path, "app": app, "before": snapshot}


def _live(db_path):
    con = sqlite3.connect(db_path)
    columns = [row[1] for row in con.execute('PRAGMA table_info("user")')]
    indexes = {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user'"
        )
    }
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    return columns, indexes, tables


def test_no_column_is_lost(upgraded):
    """The failure mode of #379: an upgrade that silently discards user settings."""
    columns, _, _ = _live(upgraded["path"])

    missing = upgraded["before"]["columns"] - set(columns)

    assert not missing, (
        f"upgrading from {upgraded['version']} destroyed {len(missing)} column(s): "
        f"{sorted(missing)}"
    )


def test_no_value_is_lost(upgraded):
    """Columns surviving as empty is the same data loss, one step later."""
    con = sqlite3.connect(upgraded["path"])
    row = con.execute('SELECT * FROM "user" WHERE id = 1').fetchone()
    names = [d[0] for d in con.execute('SELECT * FROM "user" WHERE id = 1').description]
    con.close()
    actual = dict(zip(names, row))

    changed = {
        name: (expected, actual.get(name))
        for name, expected in upgraded["before"]["seeded"].items()
        if actual.get(name) != expected
    }

    assert not changed, f"upgrading from {upgraded['version']} altered values: {changed}"
    assert actual["password"] == "bcrypt-hash-preserved"
    assert actual["username"] == "existing"


def test_no_index_is_lost(upgraded):
    _, indexes, _ = _live(upgraded["path"])

    missing = upgraded["before"]["indexes"] - indexes

    assert not missing, f"upgrading from {upgraded['version']} dropped indexes: {sorted(missing)}"


def test_schema_reaches_the_current_model(upgraded):
    """The upgrade has to actually finish, not merely avoid breaking things."""
    columns, _, _ = _live(upgraded["path"])

    expected = {column.name for column in User.__table__.columns}

    assert expected - set(columns) == set()


def test_password_ends_up_nullable(upgraded):
    """SSO and LDAP provisioning depend on this, and it is what #379 blocked."""
    with upgraded["app"].app_context():
        password = next(
            column for column in inspect(db.engine).get_columns("user")
            if column["name"] == "password"
        )

    assert password["nullable"] is True


def test_no_scratch_tables_are_left_behind(upgraded):
    _, _, tables = _live(upgraded["path"])

    leftovers = {name for name in tables if name.endswith("_new")}

    assert not leftovers, f"migration left scratch tables behind: {sorted(leftovers)}"


def test_second_startup_is_a_no_op(upgraded):
    """Migrations run about five times per container start, and on every restart."""
    before = _live(upgraded["path"])

    with upgraded["app"].app_context():
        initialize_database(upgraded["app"])
        initialize_database(upgraded["app"])

    assert _live(upgraded["path"]) == before


class TestSectionIsolation:
    """One failing migration section must not take down the sections after it.

    Before the sections existed, everything shared a single try block, so the
    first unguarded failure silently skipped every remaining migration and the
    app booted against a part-upgraded schema.
    """

    @pytest.fixture
    def old_db(self, tmp_path):
        db_path = str(tmp_path / "poisoned.db")
        with open(os.path.join(FIXTURE_DIR, "v0.5.8-alpha.sql")) as fh:
            schema_sql = fh.read()
        con = sqlite3.connect(db_path)
        con.executescript(schema_sql)
        con.execute(
            'INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
            ("existing", "existing@example.test", "bcrypt-hash-preserved"),
        )
        con.commit()
        con.close()
        return db_path

    def test_later_sections_still_run_after_a_failure(self, old_db, monkeypatch):
        import src.init_db as init_db

        real_add = init_db.add_column_if_not_exists

        def poisoned(engine, table, column, column_type):
            if column == "mime_type":  # first statement of its section
                raise RuntimeError("poisoned migration")
            return real_add(engine, table, column, column_type)

        monkeypatch.setattr(init_db, "add_column_if_not_exists", poisoned)

        app = _fixture_app(old_db)
        with app.app_context():
            initialize_database(app)

        con = sqlite3.connect(old_db)
        user_cols = [r[1] for r in con.execute('PRAGMA table_info("user")')]
        recording_cols = [r[1] for r in con.execute('PRAGMA table_info("recording")')]
        password_not_null = [
            r[3] for r in con.execute('PRAGMA table_info("user")') if r[1] == "password"
        ][0]
        con.close()

        # The section before the failure completed.
        assert password_not_null == 0
        # The poisoned section aborted: statements after the failing one were
        # skipped. (mime_type itself already exists in the v0.5.8 schema; the
        # poison fires on the call, not the column's absence.)
        assert "audio_deleted_at" not in recording_cols
        assert "prompt_variables" not in recording_cols
        # Sections after it still ran.
        assert "monthly_token_budget" in user_cols
        assert "speaker_count_mode" in user_cols
        # The failure is recorded, named, and loud.
        assert "recording, tag, speaker and processing columns" in app.config["MIGRATION_FAILED"]
        assert "poisoned migration" in app.config["MIGRATION_FAILED"]

    def test_a_failure_mid_session_does_not_poison_later_sections(self, old_db, monkeypatch):
        """A section failing with pending ORM writes must not damage the rest.

        Its open transaction holds SQLite's write lock, so later sections fail
        with "database is locked", and its half-built rows are flushed to disk
        by the next section that commits. The section handler rolls the session
        back to prevent both.
        """
        import src.init_db as init_db
        from src.database import db
        from src.models import SystemSetting

        real_index = init_db.create_index_if_not_exists
        fired = []

        def poisoned(*args, **kwargs):
            if not fired:
                fired.append(True)
                db.session.add(SystemSetting(key="poison-row", value="v"))
                db.session.flush()
                raise RuntimeError("poisoned mid-session")
            return real_index(*args, **kwargs)

        monkeypatch.setattr(init_db, "create_index_if_not_exists", poisoned)

        app = _fixture_app(old_db)
        with app.app_context():
            initialize_database(app)

        con = sqlite3.connect(old_db)
        leaked = con.execute(
            "SELECT COUNT(*) FROM system_setting WHERE key = 'poison-row'"
        ).fetchone()[0]
        recording_cols = [r[1] for r in con.execute('PRAGMA table_info("recording")')]
        con.close()

        assert leaked == 0, "the failed section's pending row was committed by a later section"
        # The section after the failure completed rather than dying on the lock.
        assert "file_hash" in recording_cols
        assert app.config["MIGRATION_FAILED"].count(";") == 0, (
            f"only one section should have failed: {app.config['MIGRATION_FAILED']}"
        )

    def test_next_clean_startup_heals_the_skipped_section(self, old_db, monkeypatch):
        import src.init_db as init_db

        real_add = init_db.add_column_if_not_exists

        def poisoned(engine, table, column, column_type):
            if column == "mime_type":
                raise RuntimeError("poisoned migration")
            return real_add(engine, table, column, column_type)

        monkeypatch.setattr(init_db, "add_column_if_not_exists", poisoned)
        app = _fixture_app(old_db)
        with app.app_context():
            initialize_database(app)
        monkeypatch.setattr(init_db, "add_column_if_not_exists", real_add)

        clean_app = _fixture_app(old_db)
        with clean_app.app_context():
            initialize_database(clean_app)

        columns, _, _ = _live(old_db)
        assert {c.name for c in User.__table__.columns} - set(columns) == set()
        con = sqlite3.connect(old_db)
        assert "audio_deleted_at" in [r[1] for r in con.execute('PRAGMA table_info("recording")')]
        con.close()
        assert "MIGRATION_FAILED" not in clean_app.config


class TestIssue379:
    """The specific database state reported in #379, on a real historical schema."""

    @pytest.fixture
    def wedged(self, tmp_path):
        """v0.5.8 predates can_share_publicly, so the old migration stranded user_new here."""
        db_path = str(tmp_path / "wedged.db")
        with open(os.path.join(FIXTURE_DIR, "v0.5.8-alpha.sql")) as fh:
            schema_sql = fh.read()
        con = sqlite3.connect(db_path)
        con.executescript(schema_sql)
        con.execute(
            'INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
            ("existing", "existing@example.test", "bcrypt-hash-preserved"),
        )
        # Exactly what the old migration left behind: created, never populated.
        con.execute(
            "CREATE TABLE user_new (id INTEGER NOT NULL, username VARCHAR(20) NOT NULL, "
            "email VARCHAR(120) NOT NULL, password VARCHAR(60), PRIMARY KEY (id))"
        )
        con.commit()
        con.close()
        return db_path

    def test_starts_clean_from_the_wedged_state(self, wedged):
        app = _fixture_app(wedged)
        with app.app_context():
            initialize_database(app)

        columns, _, tables = _live(wedged)
        assert "user_new" not in tables
        assert {c.name for c in User.__table__.columns} - set(columns) == set()

        con = sqlite3.connect(wedged)
        assert con.execute('SELECT password FROM "user" WHERE id = 1').fetchone()[0] == (
            "bcrypt-hash-preserved"
        )
        con.close()

    def test_a_long_wedged_database_keeps_its_settings(self, tmp_path):
        """The trap in #379: unwedging the old migration is what destroys data.

        A database stuck since v0.5.8 kept collecting columns while the old
        migration failed every boot, so by v0.10.3 it holds 41 of them behind a
        password column that is still NOT NULL. The old migration would finally
        run here and rebuild the table from its frozen 17-column list.
        """
        db_path = str(tmp_path / "long_wedged.db")
        with open(os.path.join(FIXTURE_DIR, "v0.10.3-alpha.sql")) as fh:
            schema_sql = fh.read()
        con = sqlite3.connect(db_path)
        con.executescript(schema_sql)

        # Restore the NOT NULL the migration never managed to remove.
        con.execute("PRAGMA writable_schema=ON")
        user_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user'"
        ).fetchone()[0]
        assert "\tpassword VARCHAR(60), " in user_sql
        con.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='user'",
            (user_sql.replace("\tpassword VARCHAR(60), ", "\tpassword VARCHAR(60) NOT NULL, "),),
        )
        con.commit()
        con.execute("PRAGMA writable_schema=OFF")
        con.close()

        snapshot = _seed(db_path)
        assert len(snapshot["columns"]) == 41

        app = _fixture_app(db_path)
        with app.app_context():
            initialize_database(app)

        columns, indexes, _ = _live(db_path)
        assert snapshot["columns"] - set(columns) == set(), "settings columns were destroyed"

        # Check the values before the indexes. A rebuild's damage is invisible at
        # the column-name level, because the add_column_if_not_exists() calls
        # further down the same function immediately put the names back, empty.
        con = sqlite3.connect(db_path)
        row = con.execute('SELECT * FROM "user" WHERE id = 1').fetchone()
        names = [d[0] for d in con.execute('SELECT * FROM "user" WHERE id = 1').description]
        con.close()
        actual = dict(zip(names, row))
        emptied = {
            name: (expected, actual.get(name))
            for name, expected in snapshot["seeded"].items()
            if actual.get(name) != expected
        }
        assert emptied == {}, f"settings were silently emptied: {emptied}"

        assert snapshot["indexes"] - indexes == set(), "indexes were destroyed"

    def test_sso_user_can_be_created_afterwards(self, wedged):
        app = _fixture_app(wedged)
        with app.app_context():
            initialize_database(app)

        con = sqlite3.connect(wedged)
        con.execute(
            'INSERT INTO "user" (username, email, password, sso_provider, sso_subject) '
            "VALUES ('sso', 'sso@example.test', NULL, 'oidc', 'subject-1')"
        )
        con.commit()
        assert con.execute(
            'SELECT COUNT(*) FROM "user" WHERE password IS NULL'
        ).fetchone()[0] == 1
        con.close()
