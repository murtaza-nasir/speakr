"""The folder.parent_id migration (#585) on upgraded databases.

An upgraded database must end up with what a fresh one gets from the model:
the column, its index and its foreign key (ON DELETE SET NULL). The tests
also assert on what a bad migration would take away: the other folder
columns, the other indexes and the stored values. Every existing folder
becomes a top-level folder.

Set TEST_DATABASE_URI to a PostgreSQL URL to run the PostgreSQL test, e.g.
    TEST_DATABASE_URI=postgresql://speakr:speakr@localhost:5432/speakr
The database must be empty; the test drops every table it finds.
"""

import os
import sqlite3

import pytest
from flask import Flask
from sqlalchemy import inspect, text

from src.database import db
from src.init_db import initialize_database
import src.models  # noqa: F401  (registers the models)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "schemas", "v0.10.10-alpha.sql")

FOLDER_ROWS = [
    # id, name, user_id, color, custom_prompt, default_language, retention_days, protect_from_deletion
    (1, "Client Calls", 1, "#10B981", "keep-prompt-1", "en", 30, 0),
    (2, "Standups", 1, "#123456", "keep-prompt-2", "de", None, 1),
]
FOLDER_COLUMNS = "id, name, user_id, color, custom_prompt, default_language, retention_days, protect_from_deletion"


def _app(uri):
    app = Flask(f"folder_parent_migration_{abs(hash(uri))}")
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _parent_fk(engine):
    return [
        (fk["constrained_columns"], fk["referred_table"], fk["referred_columns"],
         (fk.get("options") or {}).get("ondelete"))
        for fk in inspect(engine).get_foreign_keys("folder")
        if fk["constrained_columns"] == ["parent_id"]
    ]


def _model_parent_fk():
    fks = [fk for fk in src.models.Folder.__table__.c.parent_id.foreign_keys]
    return [(["parent_id"], fk.column.table.name, [fk.column.name], fk.ondelete) for fk in fks]


def _sqlite_parent_fk(db_path):
    """(table, from, to, on_delete) of folder.parent_id, from SQLite itself."""
    con = sqlite3.connect(db_path)
    rows = [(row[2], row[3], row[4], row[6]) for row in con.execute('PRAGMA foreign_key_list("folder")')
            if row[3] == "parent_id"]
    con.close()
    return rows


def _fresh_sqlite(db_path):
    """A database built by create_all from today's models."""
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        db.create_all()
        db.session.remove()
    return db_path


@pytest.fixture
def old_sqlite(tmp_path):
    db_path = str(tmp_path / "v0.10.10.db")
    with open(FIXTURE) as fh:
        schema_sql = fh.read()
    con = sqlite3.connect(db_path)
    con.executescript(schema_sql)
    assert "parent_id" not in {row[1] for row in con.execute('PRAGMA table_info("folder")')}
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
                ("existing", "existing@example.test", "x"))
    con.executemany(f"INSERT INTO folder ({FOLDER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", FOLDER_ROWS)
    con.commit()
    before = {
        "columns": [row[1] for row in con.execute('PRAGMA table_info("folder")')],
        "indexes": {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='folder'")},
        "fks": sorted(tuple(row[2:5]) for row in con.execute('PRAGMA foreign_key_list("folder")')),
    }
    con.close()
    return db_path, before


def test_sqlite_upgrade_matches_a_fresh_database(old_sqlite):
    db_path, before = old_sqlite
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        initialize_database(app)
        engine = db.engine
        columns = {col["name"]: col for col in inspect(engine).get_columns("folder")}
        indexes = {idx["name"]: idx for idx in inspect(engine).get_indexes("folder")}
        assert columns["parent_id"]["nullable"] is True
        assert indexes["ix_folder_parent_id"]["column_names"] == ["parent_id"]
        # SQLAlchemy does not read ON DELETE from a column-level REFERENCES
        # clause on SQLite, so the action is checked with the PRAGMA below.
        assert [fk[:3] for fk in _parent_fk(engine)] == [fk[:3] for fk in _model_parent_fk()]

    con = sqlite3.connect(db_path)
    after_columns = [row[1] for row in con.execute('PRAGMA table_info("folder")')]
    after_indexes = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='folder'")}
    after_fks = sorted(tuple(row[2:5]) for row in con.execute('PRAGMA foreign_key_list("folder")'))
    rows = con.execute(f"SELECT {FOLDER_COLUMNS}, parent_id FROM folder ORDER BY id").fetchall()
    con.close()

    assert set(before["columns"]) <= set(after_columns)
    assert before["indexes"] <= after_indexes
    assert set(before["fks"]) <= set(after_fks)
    assert ("folder", "parent_id", "id") in after_fks
    assert _sqlite_parent_fk(db_path) == _sqlite_parent_fk(_fresh_sqlite(db_path + ".fresh"))
    assert _sqlite_parent_fk(db_path) == [("folder", "parent_id", "id", "SET NULL")]
    assert rows == [row + (None,) for row in FOLDER_ROWS]


def test_sqlite_second_startup_is_a_no_op(old_sqlite):
    db_path, _ = old_sqlite
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        initialize_database(app)
    con = sqlite3.connect(db_path)
    schema = con.execute("SELECT sql FROM sqlite_master WHERE name='folder'").fetchone()
    con.close()
    with app.app_context():
        initialize_database(app)
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT sql FROM sqlite_master WHERE name='folder'").fetchone() == schema
    con.close()


def test_promotion_does_not_depend_on_the_foreign_key(old_sqlite):
    """SQLite does not enforce the foreign key here, so deleting a parent
    through the ORM must move its children up by itself."""
    db_path, _ = old_sqlite
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        initialize_database(app)
        Folder = src.models.Folder
        with db.engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 0
        db.session.get(Folder, 2).parent_id = 1
        db.session.commit()
        db.session.delete(db.session.get(Folder, 1))
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Folder, 2).parent_id is None
        db.session.remove()


def _drop_everything(engine):
    with engine.connect() as conn:
        for table in inspect(engine).get_table_names():
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        conn.commit()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URI", "").startswith("postgresql"),
                    reason="needs TEST_DATABASE_URI pointing at an empty PostgreSQL database")
def test_postgresql_upgrade_matches_a_fresh_database():
    app = _app(os.environ["TEST_DATABASE_URI"])
    with app.app_context():
        engine = db.engine
        _drop_everything(engine)
        try:
            initialize_database(app)
            fresh_fk = _parent_fk(engine)
            fresh_fk_names = sorted(fk["name"] for fk in inspect(engine).get_foreign_keys("folder")
                                    if fk["constrained_columns"] == ["parent_id"])
            assert fresh_fk == [(["parent_id"], "folder", ["id"], "SET NULL")]

            # Turn it back into a database from before #585, with folders in it.
            with engine.connect() as conn:
                conn.execute(text('ALTER TABLE folder DROP COLUMN parent_id'))
                conn.execute(text('INSERT INTO "user" (id, username, email, password) '
                                  "VALUES (1, 'existing', 'existing@example.test', 'x')"))
                for row in FOLDER_ROWS:
                    conn.execute(text(f"INSERT INTO folder ({FOLDER_COLUMNS}) VALUES "
                                      "(:id, :name, :user_id, :color, :prompt, :lang, :days, :protect)"),
                                 dict(zip(["id", "name", "user_id", "color", "prompt", "lang", "days", "protect"],
                                          row[:7] + (bool(row[7]),))))
                conn.commit()
            assert "parent_id" not in {c["name"] for c in inspect(engine).get_columns("folder")}
            before_columns = {c["name"] for c in inspect(engine).get_columns("folder")}
            before_indexes = {i["name"] for i in inspect(engine).get_indexes("folder")}

            initialize_database(app)

            insp = inspect(engine)
            assert before_columns | {"parent_id"} == {c["name"] for c in insp.get_columns("folder")}
            assert before_indexes | {"ix_folder_parent_id"} == {i["name"] for i in insp.get_indexes("folder")}
            assert _parent_fk(engine) == fresh_fk
            assert sorted(fk["name"] for fk in insp.get_foreign_keys("folder")
                          if fk["constrained_columns"] == ["parent_id"]) == fresh_fk_names
            with engine.connect() as conn:
                rows = conn.execute(text(f"SELECT {FOLDER_COLUMNS}, parent_id FROM folder ORDER BY id")).fetchall()
            assert [tuple(r) for r in rows] == [row[:7] + (bool(row[7]), None) for row in FOLDER_ROWS]

            # The foreign key is enforced on PostgreSQL: a raw delete of a
            # parent leaves its child top level, not dangling.
            with engine.connect() as conn:
                conn.execute(text("UPDATE folder SET parent_id = 1 WHERE id = 2"))
                conn.execute(text("DELETE FROM folder WHERE id = 1"))
                conn.commit()
                assert conn.execute(text("SELECT parent_id FROM folder WHERE id = 2")).scalar() is None
        finally:
            db.session.remove()
            _drop_everything(engine)
