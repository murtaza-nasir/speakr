"""Index on transcript chunks by recording (D#441).

Chunks are read and deleted by recording, in order, and recording_id had no
index, so every such query scanned the whole table.
"""

from sqlalchemy import inspect, text

from src.app import app, db


def test_chunks_are_indexed_by_recording():
    with app.app_context():
        indexes = {i['name']: i['column_names'] for i in inspect(db.engine).get_indexes('transcript_chunk')}
        assert indexes.get('ix_transcript_chunk_recording') == ['recording_id', 'chunk_index']


def test_chunk_lookup_by_recording_uses_the_index():
    with app.app_context():
        if db.engine.dialect.name != 'sqlite':
            return
        plan = db.session.execute(text(
            'EXPLAIN QUERY PLAN SELECT * FROM transcript_chunk WHERE recording_id = 1 ORDER BY chunk_index'
        )).fetchall()
        assert any('ix_transcript_chunk_recording' in str(row) for row in plan)
