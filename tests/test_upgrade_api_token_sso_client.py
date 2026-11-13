"""Upgrade path for api_token.sso_client_id (SSO token exchange).

Each historical schema fixture with an api_token table is loaded, tokens are
seeded as a real installation holds them, the current migrations run twice,
and the new column is checked: present, NULL for every existing token, and no
other value changed.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_upgrade_mailr_api import FIXTURES, _columns, _load, _upgrade


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_api_token_sso_client_id_column(tmp_path, fixture):
    db_path, con = _load(tmp_path, fixture)
    if "api_token" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        con.close()
        pytest.skip("api_token table not in this release")
    before = _columns(con, "api_token")
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
                ("u", "u@example.test", "hash"))
    con.execute("INSERT INTO api_token (id, user_id, token_hash, name, created_at, last_used_at, expires_at, revoked) "
                "VALUES (5, 1, 'abc123', 'n8n', '2026-01-01 00:00:00', '2026-02-01 00:00:00', "
                "'2027-01-01 00:00:00', 1)")
    con.execute("INSERT INTO api_token (id, user_id, token_hash, name, created_at, revoked) "
                "VALUES (6, 1, 'def456', 'cli', '2026-03-01 00:00:00', 0)")
    if "scopes" in before:
        con.execute("UPDATE api_token SET scopes = '[\"read\"]' WHERE id = 6")
    con.commit()
    con.close()

    _upgrade(db_path)
    _upgrade(db_path)   # second start: no-op, no error

    con = sqlite3.connect(db_path)
    after = _columns(con, "api_token")
    assert "sso_client_id" in after
    assert before <= after, "no column of api_token was lost"
    rows = con.execute("SELECT id, user_id, token_hash, name, created_at, last_used_at, expires_at, revoked, "
                       "scopes, sso_client_id FROM api_token ORDER BY id").fetchall()
    con.close()
    assert rows == [
        (5, 1, "abc123", "n8n", "2026-01-01 00:00:00", "2026-02-01 00:00:00", "2027-01-01 00:00:00", 1, None, None),
        (6, 1, "def456", "cli", "2026-03-01 00:00:00", None, None, 0,
         '["read"]' if "scopes" in before else None, None),
    ]
