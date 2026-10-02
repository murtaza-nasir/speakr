"""Upgrade path for the mailr API migrations (spec section 2).

Each historical schema fixture is loaded, rows are seeded the way a real
installation holds them, the current migrations run, and the new columns are
checked: present, with their default, and no existing value changed. A second
startup must be a no-op. New migrations in this work add a case below.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_upgrade_path import FIXTURE_DIR, FIXTURES, _fixture_app
from src.init_db import initialize_database


def _load(tmp_path, fixture):
    db_path = str(tmp_path / fixture.replace(".sql", ".db"))
    con = sqlite3.connect(db_path)
    con.executescript(open(os.path.join(FIXTURE_DIR, fixture)).read())
    con.commit()
    return db_path, con


def _columns(con, table):
    return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}


def _upgrade(db_path):
    app = _fixture_app(db_path)
    with app.app_context():
        initialize_database(app)
    return app


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_api_token_scopes_column(tmp_path, fixture):
    db_path, con = _load(tmp_path, fixture)
    if "api_token" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        con.close()
        pytest.skip("api_token table not in this release")
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
                ("u", "u@example.test", "hash"))
    con.execute("INSERT INTO api_token (id, user_id, token_hash, name, created_at, expires_at, revoked) "
                "VALUES (5, 1, 'abc123', 'n8n', '2026-01-01 00:00:00', '2027-01-01 00:00:00', 1)")
    con.commit()
    con.close()

    _upgrade(db_path)
    _upgrade(db_path)   # second start: no-op, no error

    con = sqlite3.connect(db_path)
    assert "scopes" in _columns(con, "api_token")
    row = con.execute("SELECT token_hash, name, expires_at, revoked, scopes FROM api_token WHERE id = 5").fetchone()
    con.close()
    assert row[:4] == ("abc123", "n8n", "2027-01-01 00:00:00", 1)
    assert row[4] is None, "existing tokens keep full access (scopes NULL)"
