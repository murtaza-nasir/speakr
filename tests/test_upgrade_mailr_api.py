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


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_recording_updated_at_and_tombstones(tmp_path, fixture):
    """G2: updated_at is filled from completed_at or created_at, once."""
    db_path, con = _load(tmp_path, fixture)
    cols = _columns(con, "recording")
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
                ("u", "u@example.test", "hash"))
    extra = {"keep_audio_only": 0} if "keep_audio_only" in cols else {}

    def _insert(**values):
        values.update(extra)
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        con.execute(f"INSERT INTO recording ({names}) VALUES ({marks})", tuple(values.values()))

    _insert(id=7, user_id=1, title="Old", status="COMPLETED", created_at="2024-03-01 10:00:00")
    if "completed_at" in cols:
        _insert(id=8, user_id=1, title="Done", status="COMPLETED", created_at="2024-03-02 10:00:00",
                completed_at="2024-03-02 11:30:00")
    con.commit()
    con.close()

    _upgrade(db_path)
    con = sqlite3.connect(db_path)
    con.execute("UPDATE recording SET updated_at = '2025-05-05 05:05:05' WHERE id = 7")
    con.commit()
    con.close()
    _upgrade(db_path)   # second start: the backfill does not run again

    con = sqlite3.connect(db_path)
    assert "updated_at" in _columns(con, "recording")
    rows = dict(con.execute("SELECT id, updated_at FROM recording").fetchall())
    titles = dict(con.execute("SELECT id, title FROM recording").fetchall())
    indexes = {r[1] for r in con.execute("PRAGMA index_list('recording')")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ledger = con.execute("SELECT count(*) FROM schema_migrations "
                         "WHERE migration_id = '0002_backfill_recording_updated_at'").fetchone()[0]
    since = con.execute("SELECT value FROM system_setting WHERE key = 'changes_feed_since'").fetchone()
    con.close()
    assert rows[7].startswith("2025-05-05 05:05:05"), "second start left the value alone"
    if 8 in rows:
        assert rows[8].startswith("2024-03-02 11:30:00"), "filled from completed_at"
    assert titles[7] == "Old"
    assert {"ix_recording_updated_at", "ix_recording_user_updated"} <= indexes
    assert "recording_tombstone" in tables
    assert ledger == 1 and since and since[0].endswith("Z")
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT count(*) FROM schema_migrations "
                       "WHERE migration_id = '0003_link_transcript_speakers'").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_webhook_rotation_grace_columns(tmp_path, fixture):
    """W2: previous_secret columns arrive empty; the current secret is untouched."""
    db_path, con = _load(tmp_path, fixture)
    if "webhook" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        con.close()
        pytest.skip("webhook table not in this release")
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)',
                ("u", "u@example.test", "hash"))
    cols = _columns(con, "webhook")
    values = {"id": 3, "user_id": 1, "name": "n8n", "url": "https://example.com/h", "secret": "s" * 40,
              "events": "[]", "enabled": 1, "auto_paused": 0, "consecutive_failures": 0,
              "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00", "allow_http": 0}
    values = {k: v for k, v in values.items() if k in cols}
    con.execute(f"INSERT INTO webhook ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()))
    con.commit()
    con.close()

    _upgrade(db_path)
    _upgrade(db_path)

    con = sqlite3.connect(db_path)
    assert {"previous_secret", "previous_secret_expires_at", "include_shared"} <= _columns(con, "webhook")
    assert con.execute("SELECT include_shared FROM webhook WHERE id = 3").fetchone()[0] in (0, False)
    row = con.execute("SELECT secret, previous_secret, previous_secret_expires_at FROM webhook WHERE id = 3").fetchone()
    con.close()
    assert row == ("s" * 40, None, None)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_upload_idempotency_and_external_refs(tmp_path, fixture):
    """G8: the idempotency column and index, and the reference table."""
    db_path, con = _load(tmp_path, fixture)
    con.close()
    _upgrade(db_path)
    _upgrade(db_path)
    con = sqlite3.connect(db_path)
    assert "upload_idempotency_key" in _columns(con, "recording")
    assert "ix_recording_user_idem" in {r[1] for r in con.execute("PRAGMA index_list('recording')")}
    assert {"recording_id", "user_id", "system", "kind", "ref", "url", "label"} <= _columns(con, "recording_external_ref")
    con.close()


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_speaker_contact_columns(tmp_path, fixture):
    """G6: email, aliases and updated_at arrive empty; names stay."""
    db_path, con = _load(tmp_path, fixture)
    if "speaker" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        con.close()
        pytest.skip("speaker table not in this release")
    con.execute('INSERT INTO "user" (id, username, email, password) VALUES (1, ?, ?, ?)', ("u", "u@example.test", "hash"))
    con.execute("INSERT INTO speaker (id, name, user_id) VALUES (4, 'Dana', 1)")
    con.commit()
    con.close()
    _upgrade(db_path)
    _upgrade(db_path)
    con = sqlite3.connect(db_path)
    assert {"email", "aliases", "updated_at"} <= _columns(con, "speaker")
    assert "ix_speaker_user_email" in {r[1] for r in con.execute("PRAGMA index_list('speaker')")}
    assert con.execute("SELECT name, email, aliases FROM speaker WHERE id = 4").fetchone() == ("Dana", None, None)
    con.close()


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f[:-4] for f in FIXTURES])
def test_share_expiry_column(tmp_path, fixture):
    """G10: share.expires_at arrives empty, so existing links never expire."""
    db_path, con = _load(tmp_path, fixture)
    if "share" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        con.close()
        pytest.skip("share table not in this release")
    con.close()
    _upgrade(db_path)
    _upgrade(db_path)
    con = sqlite3.connect(db_path)
    assert "expires_at" in _columns(con, "share")
    con.close()

