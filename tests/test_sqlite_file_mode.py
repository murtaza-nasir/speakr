"""The SQLite database and its WAL are not world-readable (#506)."""

import os
import sqlite3
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.database import remove_world_access_to_sqlite_files


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _wal_database(path, umask=0o022):
    old = os.umask(umask)
    try:
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        return con
    finally:
        os.umask(old)


def test_world_bits_are_cleared_and_owner_and_group_keep_access(tmp_path):
    db = str(tmp_path / "t.db")
    con = _wal_database(db)
    try:
        paths = [db, db + "-wal", db + "-shm"]
        assert [_mode(p) for p in paths] == [0o644] * 3
        assert sorted(remove_world_access_to_sqlite_files(db)) == sorted(paths)
        assert [_mode(p) for p in paths] == [0o640] * 3
        assert remove_world_access_to_sqlite_files(db) == []
    finally:
        con.close()


def test_missing_sidecars_and_missing_database_are_fine(tmp_path):
    db = tmp_path / "plain.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    assert remove_world_access_to_sqlite_files(str(db)) == [str(db)]
    assert _mode(db) == 0o640
    assert remove_world_access_to_sqlite_files(str(tmp_path / "absent.db")) == []
    assert remove_world_access_to_sqlite_files(":memory:") == []
    assert remove_world_access_to_sqlite_files(None) == []


def test_private_modes_are_not_widened(tmp_path):
    db = tmp_path / "private.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o600)
    assert remove_world_access_to_sqlite_files(str(db)) == []
    assert _mode(db) == 0o600


def test_new_sidecars_follow_the_tightened_database(tmp_path):
    db = str(tmp_path / "t.db")
    _wal_database(db).close()
    remove_world_access_to_sqlite_files(db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    con = _wal_database(db)
    try:
        assert _mode(db + "-wal") == 0o640 and _mode(db + "-shm") == 0o640
    finally:
        con.close()
