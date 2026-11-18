"""Date filters of the recording list refuse a bad date with 400 (#631).

Before, the web route (/api/recordings, the date:, date_from: and date_to:
tokens of q) and API v1 (/api/v1/recordings, date_from and date_to) dropped
a date they could not read and returned the whole list. Now a value that is
not a day (or, for date:, a preset, month or year) from 1900 to 2100 answers
400 invalid_parameter, as a bad folder_id or updated_since does.

SHARED-DB: every assertion is scoped to the user the test made.
"""

import os
import sys
import uuid
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import Recording, User  # noqa: E402
from src.utils.dates import filter_day  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture
def owner():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'df_{suffix}', email=f'df_{suffix}@local.test', password='x')
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title='dated', status='COMPLETED', original_filename='x.mp3',
                        audio_path='local://recordings/x.mp3',
                        meeting_date=datetime(2026, 10, 31, 19, 42, 56))
        db.session.add(rec)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        Recording.query.filter_by(user_id=uid).delete(synchronize_session=False)
        User.query.filter_by(id=uid).delete(synchronize_session=False)
        db.session.commit()


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    return client


def _web(client, q):
    return client.get('/api/recordings', query_string={'q': q, 'tz': 'UTC'})


@pytest.mark.parametrize('q', [
    'date_from:9999-99-99', 'date_to:0000-00-00', 'date:2026-13-40', 'date_from:not-a-date',
    'date_from:2026-02-30', 'date_to:1899-12-31', 'date_from:2101-01-01', 'date:9999',
    'date:2026-13', 'date:3000-01', 'date:sometime', 'notes date_to:2026-1-1',
])
def test_the_web_route_refuses_an_invalid_date(owner, q):
    resp = _web(_client(owner), q)
    assert resp.status_code == 400, resp.data
    body = resp.get_json()
    assert body['code'] == 'invalid_parameter'
    field = q.split()[-1].split(':')[0]
    assert body['error'].startswith(field)


def test_the_web_route_still_filters_by_a_valid_date(owner):
    client = _client(owner)

    def total(q):
        resp = _web(client, q)
        assert resp.status_code == 200, resp.data
        return resp.get_json()['pagination']['total']

    assert total('date_from:2026-10-01') == 1
    assert total('date_from:2099-01-01') == 0
    assert total('date_to:2026-10-31') == 1
    assert total('date_to:2026-10-30') == 0
    assert total('date:2026-10-31') == 1
    assert total('date:2026-10') == 1
    assert total('date:2026') == 1
    assert total('date:2025') == 0
    assert total('date:today') == 0
    assert total('date_from:1900-01-01 date_to:2100-12-31') == 1


def test_the_web_route_without_a_filter_is_unchanged(owner):
    resp = _client(owner).get('/api/recordings')
    assert resp.status_code == 200 and resp.get_json()['pagination']['total'] == 1


def test_a_word_that_ends_in_date_is_text_not_a_filter(owner):
    # "candidate:x" is searched as text; it is not a date: token.
    resp = _web(_client(owner), 'candidate:smith')
    assert resp.status_code == 200, resp.data
    assert resp.get_json()['pagination']['total'] == 0


@pytest.mark.parametrize('params', [
    {'date_from': '9999-99-99'}, {'date_to': '0000-00-00'}, {'date_from': 'not-a-date'},
    {'date_from': '2026-02-30'}, {'date_to': '2101-01-01'}, {'date_from': '1899-12-31T23:00:00Z'},
])
def test_api_v1_refuses_an_invalid_date(owner, params):
    resp = _client(owner).get('/api/v1/recordings', query_string=params)
    assert resp.status_code == 400, resp.data
    body = resp.get_json()
    assert body['code'] == 'invalid_parameter'
    assert body['error'].startswith(next(iter(params)))


def test_api_v1_still_filters_by_a_valid_date(owner):
    client = _client(owner)

    def total(**params):
        resp = client.get('/api/v1/recordings', query_string={'date_field': 'meeting_date', **params})
        assert resp.status_code == 200, resp.data
        return resp.get_json()['pagination']['total']

    assert total(date_from='2099-01-01') == 0
    assert total(date_from='2026-10-01') == 1
    assert total(date_to='2026-10-31') == 1
    assert total(date_to='2026-10-31T12:00:00Z') == 0
    assert total(date_from='2026-10-31T19:00:00+00:00') == 1
    assert total() == 1


def test_filter_day():
    assert filter_day('2026-10-31').isoformat() == '2026-10-31'
    assert filter_day('1900-01-01') and filter_day('2100-12-31')
    for value in ('9999-99-99', '2026-02-30', '1899-12-31', '2101-01-01', '2026-1-1', '', None, 20261031):
        assert filter_day(value) is None
