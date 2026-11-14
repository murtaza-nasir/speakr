"""The group.created_by column and the permission_grant table (#576) on fresh
and upgraded databases.

An upgraded database must end up with what a fresh one gets from the models:
the column, its index and its foreign key (ON DELETE SET NULL), the grant
table and the mode setting, which starts at admins. Existing groups keep a
NULL creator, and the other group columns and values stay.

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
GROUP_ROWS = [(1, "Engineering", "first"), (2, "Sales", None)]


def _app(uri):
    app = Flask(f"group_creator_migration_{abs(hash(uri))}")
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _created_by_fk(engine):
    return [
        (fk["constrained_columns"], fk["referred_table"], fk["referred_columns"],
         (fk.get("options") or {}).get("ondelete"))
        for fk in inspect(engine).get_foreign_keys("group")
        if fk["constrained_columns"] == ["created_by"]
    ]


def _sqlite_created_by_fk(db_path):
    con = sqlite3.connect(db_path)
    rows = [(row[2], row[3], row[4], row[6]) for row in con.execute('PRAGMA foreign_key_list("group")')
            if row[3] == "created_by"]
    con.close()
    return rows


def _mode_setting():
    from src.models import SystemSetting
    row = SystemSetting.query.filter_by(key="permission_create_groups").first()
    return row.value if row else None


def _grant_columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("permission_grant")}


def test_fresh_sqlite_database(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        initialize_database(app)
        engine = db.engine
        assert {"id", "permission", "user_id", "group_id", "created_at"} <= _grant_columns(engine)
        assert "created_by" in {c["name"] for c in inspect(engine).get_columns("group")}
        assert "ix_group_created_by" in {i["name"] for i in inspect(engine).get_indexes("group")}
        assert _mode_setting() == "admins"
        db.session.remove()
    assert _sqlite_created_by_fk(db_path) == [("user", "created_by", "id", "SET NULL")]


@pytest.fixture
def old_sqlite(tmp_path):
    db_path = str(tmp_path / "v0.10.10.db")
    with open(FIXTURE) as fh:
        schema_sql = fh.read()
    con = sqlite3.connect(db_path)
    con.executescript(schema_sql)
    assert "created_by" not in {row[1] for row in con.execute('PRAGMA table_info("group")')}
    assert con.execute("SELECT name FROM sqlite_master WHERE name='permission_grant'").fetchone() is None
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
                ("existing", "existing@example.test", "x"))
    con.executemany('INSERT INTO "group" (id, name, description) VALUES (?, ?, ?)', GROUP_ROWS)
    con.commit()
    before = {
        "columns": [row[1] for row in con.execute('PRAGMA table_info("group")')],
        "indexes": {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='group'")},
    }
    con.close()
    return db_path, before


def test_sqlite_upgrade_matches_a_fresh_database(old_sqlite, tmp_path):
    db_path, before = old_sqlite
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        initialize_database(app)
        engine = db.engine
        columns = {c["name"]: c for c in inspect(engine).get_columns("group")}
        assert columns["created_by"]["nullable"] is True
        indexes = {i["name"]: i for i in inspect(engine).get_indexes("group")}
        assert indexes["ix_group_created_by"]["column_names"] == ["created_by"]
        assert [fk[:3] for fk in _created_by_fk(engine)] == [(["created_by"], "user", ["id"])]
        assert {"id", "permission", "user_id", "group_id", "created_at"} <= _grant_columns(engine)
        assert _mode_setting() == "admins"
        db.session.remove()

    con = sqlite3.connect(db_path)
    after_columns = [row[1] for row in con.execute('PRAGMA table_info("group")')]
    after_indexes = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='group'")}
    rows = con.execute('SELECT id, name, description, created_by FROM "group" ORDER BY id').fetchall()
    con.close()
    assert set(before["columns"]) <= set(after_columns)
    assert before["indexes"] <= after_indexes
    assert rows == [row + (None,) for row in GROUP_ROWS]
    assert _sqlite_created_by_fk(db_path) == [("user", "created_by", "id", "SET NULL")]


def test_sqlite_second_startup_is_a_no_op(old_sqlite):
    db_path, _ = old_sqlite
    app = _app(f"sqlite:///{db_path}")
    with app.app_context():
        initialize_database(app)
        from src.models import SystemSetting
        SystemSetting.set_setting("permission_create_groups", "everyone")
        db.session.remove()
    con = sqlite3.connect(db_path)
    schema = con.execute("SELECT sql FROM sqlite_master WHERE name='group'").fetchone()
    con.close()
    with app.app_context():
        initialize_database(app)
        # An admin's choice is not reset at startup
        assert _mode_setting() == "everyone"
        db.session.remove()
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT sql FROM sqlite_master WHERE name='group'").fetchone() == schema
    con.close()


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
            fresh_fk = _created_by_fk(engine)
            assert fresh_fk == [(["created_by"], "user", ["id"], "SET NULL")]
            assert _mode_setting() == "admins"
            db.session.remove()

            # Turn it back into a database from before #576, with groups in it.
            with engine.connect() as conn:
                conn.execute(text('ALTER TABLE "group" DROP COLUMN created_by'))
                conn.execute(text('DROP TABLE permission_grant'))
                conn.execute(text("DELETE FROM system_setting WHERE key = 'permission_create_groups'"))
                conn.execute(text('INSERT INTO "user" (id, username, email, password) '
                                  "VALUES (1, 'existing', 'existing@example.test', 'x')"))
                for gid, name, desc in GROUP_ROWS:
                    conn.execute(text('INSERT INTO "group" (id, name, description) VALUES (:i, :n, :d)'),
                                 {"i": gid, "n": name, "d": desc})
                conn.commit()
            before_columns = {c["name"] for c in inspect(engine).get_columns("group")}
            before_indexes = {i["name"] for i in inspect(engine).get_indexes("group")}

            initialize_database(app)

            insp = inspect(engine)
            assert before_columns | {"created_by"} == {c["name"] for c in insp.get_columns("group")}
            assert before_indexes | {"ix_group_created_by"} == {i["name"] for i in insp.get_indexes("group")}
            assert _created_by_fk(engine) == fresh_fk
            assert {"id", "permission", "user_id", "group_id", "created_at"} <= _grant_columns(engine)
            assert _mode_setting() == "admins"
            db.session.remove()
            with engine.connect() as conn:
                rows = conn.execute(text('SELECT id, name, description, created_by FROM "group" ORDER BY id')).fetchall()
                assert [tuple(r) for r in rows] == [row + (None,) for row in GROUP_ROWS]
                # The foreign key is enforced on PostgreSQL: deleting the
                # creator leaves the group without one.
                conn.execute(text('UPDATE "group" SET created_by = 1 WHERE id = 1'))
                conn.execute(text('DELETE FROM "user" WHERE id = 1'))
                conn.commit()
                assert conn.execute(text('SELECT created_by FROM "group" WHERE id = 1')).scalar() is None
                # A grant names exactly one user or one group
                with pytest.raises(Exception):
                    conn.execute(text("INSERT INTO permission_grant (permission) VALUES ('create_groups')"))
                conn.rollback()
        finally:
            db.session.remove()
            _drop_everything(engine)
