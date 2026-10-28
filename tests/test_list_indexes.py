"""Indexes behind the recording and sharing lists (D#483).

The batched list queries read shares, tag links and per-user states by
columns that had no index (the primary key of recording_tags and the unique
constraints start with recording_id), so each such lookup scanned its table.
The admin status counts filter recording on status alone (#507).
"""

import pytest
from sqlalchemy import inspect, text

from src.app import app, db

INDEXES = [
    ('share', 'ix_share_recording_id', ['recording_id']),
    ('share', 'ix_share_user_id', ['user_id']),
    ('internal_share', 'ix_internal_share_shared_with_user_id', ['shared_with_user_id']),
    ('internal_share', 'ix_internal_share_owner_id', ['owner_id']),
    ('shared_recording_state', 'ix_shared_recording_state_user_id', ['user_id']),
    ('recording_tags', 'ix_recording_tags_tag_id', ['tag_id']),
    ('recording', 'ix_recording_status', ['status']),
]

# One query of the lists per index, as the list code issues it
QUERIES = {
    'ix_share_recording_id': 'SELECT recording_id, count(*) FROM share WHERE recording_id IN (1, 2) GROUP BY recording_id',
    'ix_share_user_id': 'SELECT * FROM share WHERE user_id = 1 ORDER BY created_at DESC',
    'ix_internal_share_shared_with_user_id': 'SELECT recording_id FROM internal_share WHERE shared_with_user_id = 1',
    'ix_internal_share_owner_id': 'SELECT * FROM internal_share WHERE owner_id = 1 ORDER BY created_at DESC',
    'ix_shared_recording_state_user_id':
        'SELECT recording_id FROM shared_recording_state WHERE user_id = 1 AND is_highlighted = 1',
    'ix_recording_tags_tag_id': 'SELECT tag_id, count(*) FROM recording_tags WHERE tag_id IN (1, 2) GROUP BY tag_id',
    # Admin dashboard status counts (#507)
    'ix_recording_status': "SELECT count(*) FROM recording WHERE status = 'COMPLETED'",
}


@pytest.mark.parametrize('table, name, columns', INDEXES)
def test_list_lookup_columns_are_indexed(table, name, columns):
    with app.app_context():
        indexes = {i['name']: i['column_names'] for i in inspect(db.engine).get_indexes(table)}
        assert indexes.get(name) == columns


@pytest.mark.parametrize('name', sorted(QUERIES))
def test_list_lookups_use_the_index(name):
    with app.app_context():
        if db.engine.dialect.name != 'sqlite':
            return
        plan = db.session.execute(text('EXPLAIN QUERY PLAN ' + QUERIES[name])).fetchall()
        assert any(name in str(row) for row in plan), plan
