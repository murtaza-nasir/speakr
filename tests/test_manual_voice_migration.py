"""Additive manual tables on a copied old SQLite schema, then reopen.

The full initializer may legitimately update old migrations; the separate
local differential run compares its old-binary result on an identical copy.
"""
import sqlite3

from flask import Flask
from sqlalchemy import inspect, text

from src.database import db
from src.models import ManualVoiceSample, ManualVoiceSampleReceipt
from src.init_db import initialize_database


def _application(path):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=f'sqlite:///{path}', SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    return app


def _old_rows(path):
    with sqlite3.connect(path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not r[0].startswith('manual_voice_')]
        return {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}


def test_additive_tables_on_copy_preserve_old_rows_and_reopen(tmp_path):
    original = tmp_path / 'old.sqlite'
    old = _application(original)
    with old.app_context():
        # All current old tables, without either additive model.
        tables = [t for t in db.metadata.sorted_tables if not t.name.startswith('manual_voice_')]
        db.metadata.create_all(db.engine, tables=tables)
        from src.models import User, Speaker
        user = User(username='migration', email='migration@local.test', password='fixture')
        db.session.add(user)
        db.session.flush()
        speaker = Speaker(user_id=user.id, name='Old average', average_embedding=b'\x00\x00\x80?', embedding_count=3)
        db.session.add(speaker)
        db.session.commit()
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(original) as source, sqlite3.connect(tmp_path / 'copy.sqlite') as destination:
        source.backup(destination)
    copied = tmp_path / 'copy.sqlite'
    before_source = _old_rows(original)
    migrated = _application(copied)
    with migrated.app_context():
        initialize_database(migrated)
        tables = inspect(db.engine).get_table_names()
        assert 'manual_voice_sample' in tables and 'manual_voice_sample_receipt' in tables
        first = _old_rows(copied)
        initialize_database(migrated)
        assert _old_rows(copied) == first
        assert ManualVoiceSample.query.count() == ManualVoiceSampleReceipt.query.count() == 0
        db.session.remove()
        db.engine.dispose()
    assert _old_rows(original) == before_source
    reopened = _application(copied)
    with reopened.app_context():
        assert ManualVoiceSample.query.count() == ManualVoiceSampleReceipt.query.count() == 0
        assert db.session.execute(text('PRAGMA integrity_check')).scalar() == 'ok'
        assert _old_rows(copied) == first
        db.session.remove()
        db.engine.dispose()
