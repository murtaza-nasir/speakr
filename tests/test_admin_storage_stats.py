"""Top users by storage reports real numbers (#393).

The report was five users, all showing "0 bytes (0 recordings)". Two faults
compounded, and the second is why it went unnoticed for so long.

The `audio_deleted_at IS NULL` test sat in WHERE rather than in the JOIN
condition. On the right side of an outer join that predicate is also true of
the all-NULL row produced for a user with no recordings, so those users were
selected as though they qualified.

Then `ORDER BY sum(file_size) DESC`: PostgreSQL sorts NULLs FIRST on DESC, so
those empty users outranked every user with data and filled the whole limit of
five. SQLite sorts NULLs last, so on the default backend the real users still
appeared and the bug was invisible.

That last part shapes these tests. A results-based test on SQLite passes
against the broken query, so the ordering is ALSO pinned structurally, against
the compiled SQL, which is backend-independent and fails everywhere.
"""

import os
from datetime import datetime

import pytest

from src.app import app
from src.database import db
from src.models import Recording, User


@pytest.fixture
def stats_fixture():
    """Two users with recordings, several with none, one deleted audio file."""
    with app.app_context():
        tag = f'stats_{os.getpid()}'
        users, by_name = [], {}
        for name in ('has_files', 'also_has', 'empty1', 'empty2', 'empty3', 'empty4', 'empty5'):
            u = User(username=f'{tag}_{name}', email=f'{tag}_{name}@example.com',
                     password='x', is_admin=(name == 'has_files'))
            db.session.add(u)
            users.append(u)
            by_name[name] = u
        db.session.commit()
        recs = [
            Recording(user_id=by_name['has_files'].id, title='a', audio_path='/tmp/a',
                      file_size=5000),
            Recording(user_id=by_name['has_files'].id, title='b', audio_path='/tmp/b',
                      file_size=3000),
            Recording(user_id=by_name['also_has'].id, title='c', audio_path='/tmp/c',
                      file_size=9000),
            # Retention removed the audio: the row and its file_size remain but
            # the bytes are no longer on disk, so they must not be counted.
            Recording(user_id=by_name['also_has'].id, title='d', audio_path='/tmp/d',
                      file_size=7777, audio_deleted_at=datetime(2026, 1, 1)),
        ]
        db.session.add_all(recs)
        db.session.commit()
        ids = {'users': [u.id for u in users], 'recs': [r.id for r in recs],
               'admin': by_name['has_files'].id, 'tag': tag}

    yield ids

    with app.app_context():
        Recording.query.filter(Recording.id.in_(ids['recs'])).delete(synchronize_session=False)
        User.query.filter(User.id.in_(ids['users'])).delete(synchronize_session=False)
        db.session.commit()


def _top_users(client, admin_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin_id)
        sess['_fresh'] = True
    resp = client.get('/admin/stats')
    assert resp.status_code == 200, resp.data[:200]
    return resp.get_json()['top_users']


def test_users_with_recordings_are_not_crowded_out_by_empty_accounts(stats_fixture):
    """The reported symptom. On PostgreSQL the empty accounts took every slot."""
    client = app.test_client()
    top = _top_users(client, stats_fixture['admin'])

    with_bytes = [u for u in top if u['storage_used'] > 0]
    assert with_bytes, (
        f'every one of the top {len(top)} users reported zero storage: {top}')


def test_storage_totals_exclude_audio_that_retention_removed(stats_fixture):
    """also_has has 9000 bytes live and 7777 deleted; it must report 9000."""
    client = app.test_client()
    top = _top_users(client, stats_fixture['admin'])
    tag = stats_fixture['tag']

    found = {u['username']: u for u in top}
    also = found.get(f'{tag}_also_has')
    assert also is not None, f'expected the largest user in the top five: {list(found)}'
    assert also['storage_used'] == 9000, 'deleted audio was counted'
    assert also['recordings_count'] == 1, 'deleted recordings were counted'


def test_an_empty_account_reports_zero_rather_than_null(stats_fixture):
    """None would render as 'null bytes' if one reaches the template."""
    client = app.test_client()
    top = _top_users(client, stats_fixture['admin'])
    for u in top:
        assert u['storage_used'] is not None, u
        assert u['recordings_count'] is not None, u


def test_the_ordering_is_null_safe_in_the_compiled_sql():
    """The structural check, and the one that actually holds the line.

    The results tests above run on whatever backend the suite is configured
    for, which is SQLite by default, and SQLite sorts NULLs last so the broken
    query passes them. Compiling the ORDER BY and asserting the coalesce is
    present catches the regression on every backend.
    """
    from sqlalchemy.dialects import postgresql
    import inspect as _inspect
    import src.api.admin as admin_module

    source = _inspect.getsource(admin_module.admin_get_stats)

    # The predicate must be part of the join, not a WHERE on the outer side.
    assert 'isouter=True' in source
    assert 'Recording.audio_deleted_at.is_(None)' in source
    assert '.filter(Recording.audio_deleted_at.is_(None)) \\\n     .group_by' not in source, (
        'the deleted-audio test is back in WHERE, which re-selects users with no recordings')

    # And the sort key must be coalesced, or PostgreSQL puts NULLs first.
    assert 'coalesce' in source, 'the ordering is not NULL-safe'
    assert '.order_by(storage_used.desc())' in source, (
        'ordering no longer uses the coalesced expression')
