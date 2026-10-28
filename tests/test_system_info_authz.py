"""/api/system/info needs a signed-in user; service addresses go to administrators (#499).

SHARED-DB: the users are removed afterwards.
"""

import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User

ADDRESS_KEYS = ('llm_endpoint', 'transcription_endpoint', 'whisper_endpoint', 'asr_endpoint')


@pytest.fixture
def users():
    with app.app_context():
        s = secrets.token_hex(4)
        member = User(username=f"si_{s}", email=f"si_{s}@local.test", password="x")
        admin = User(username=f"sia_{s}", email=f"sia_{s}@local.test", password="x", is_admin=True)
        db.session.add_all([member, admin])
        db.session.commit()
        ids = {'member': member.id, 'admin': admin.id}
    yield ids
    with app.app_context():
        User.query.filter(User.id.in_(ids.values())).delete(synchronize_session=False)
        db.session.commit()


def _get(user_id=None):
    client = app.test_client()
    if user_id is not None:
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
    return client.get('/api/system/info')


def test_anonymous_caller_gets_401():
    resp = _get()
    assert resp.status_code == 401
    body = resp.get_json()
    assert body['code'] == 'authentication_required'
    assert 'version' not in body
    assert not any(k in body for k in ADDRESS_KEYS)


def test_member_gets_version_and_models_without_addresses(users):
    resp = _get(users['member'])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['version'] and 'llm_model' in body and 'transcription' in body and 'asr_enabled' in body
    assert not any(k in body for k in ADDRESS_KEYS)


def test_admin_gets_the_addresses(users):
    resp = _get(users['admin'])
    assert resp.status_code == 200
    body = resp.get_json()
    assert all(k in body for k in ADDRESS_KEYS)
    assert body['version']
