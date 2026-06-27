#!/usr/bin/env python3
"""
Coverage tests for src/api/initial_prompt_templates.py (issue #309).

User-scoped CRUD for reusable transcription initial-prompt texts. Mirrors the
transcript/export/naming template blueprints. Pattern follows tests/test_cov_admin.py:
isolated DB via repo-root conftest, login by session injection, hermetic/offline.

Run:
  docker run --rm -v $PWD:/app:ro -e UPLOAD_FOLDER=/tmp/up \
    -e ASR_BASE_URL=http://x:9999 speakr-test:cov \
    sh -c "cd /app && python -m pytest tests/test_cov_initial_prompt_templates.py -q"
"""

import uuid

import pytest

from src.app import app, db
from src.models import User, InitialPromptTemplate

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


def _mk_user():
    suffix = uuid.uuid4().hex[:8]
    u = User(username=f"u_{suffix}", email=f"{suffix}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True


@pytest.fixture
def ctx():
    with app.app_context():
        yield


@pytest.fixture
def user(ctx):
    u = _mk_user()
    yield u
    InitialPromptTemplate.query.filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def client(user):
    c = app.test_client()
    _login(c, user)
    return c


def test_requires_auth():
    c = app.test_client()
    assert c.get('/api/initial-prompt-templates').status_code in (401, 302)


def test_create_and_list(client, user):
    resp = client.post('/api/initial-prompt-templates',
                       json={'name': 'Meeting', 'template': 'This is a meeting.', 'hotwords': 'Acme, KPI'})
    assert resp.status_code == 201
    created = resp.get_json()
    assert created['name'] == 'Meeting'
    assert created['template'] == 'This is a meeting.'
    assert created['hotwords'] == 'Acme, KPI'

    listing = client.get('/api/initial-prompt-templates').get_json()
    assert any(t['id'] == created['id'] for t in listing)


def test_create_hotwords_only(client):
    # A template may carry hotwords with no prompt.
    resp = client.post('/api/initial-prompt-templates',
                       json={'name': 'Jargon', 'hotwords': 'CTranslate2, PyAnnote'})
    assert resp.status_code == 201
    created = resp.get_json()
    assert created['template'] == ''
    assert created['hotwords'] == 'CTranslate2, PyAnnote'


def test_update_hotwords(client):
    t = client.post('/api/initial-prompt-templates',
                    json={'name': 'A', 'template': 'a'}).get_json()
    resp = client.put(f"/api/initial-prompt-templates/{t['id']}", json={'hotwords': 'foo, bar'})
    assert resp.status_code == 200
    assert resp.get_json()['hotwords'] == 'foo, bar'


def test_create_requires_name_and_some_content(client):
    # Need a name AND at least one of prompt/hotwords.
    assert client.post('/api/initial-prompt-templates', json={'name': 'x'}).status_code == 400
    assert client.post('/api/initial-prompt-templates', json={'template': 'x'}).status_code == 400
    assert client.post('/api/initial-prompt-templates', json={'name': 'x', 'hotwords': '  '}).status_code == 400


def test_single_default_enforced(client, user):
    a = client.post('/api/initial-prompt-templates',
                    json={'name': 'A', 'template': 'a', 'is_default': True}).get_json()
    b = client.post('/api/initial-prompt-templates',
                    json={'name': 'B', 'template': 'b', 'is_default': True}).get_json()
    # Creating B as default must demote A.
    defaults = [t for t in client.get('/api/initial-prompt-templates').get_json() if t['is_default']]
    assert len(defaults) == 1
    assert defaults[0]['id'] == b['id']


def test_update(client):
    t = client.post('/api/initial-prompt-templates',
                    json={'name': 'A', 'template': 'a'}).get_json()
    resp = client.put(f"/api/initial-prompt-templates/{t['id']}",
                      json={'name': 'A2', 'template': 'a2'})
    assert resp.status_code == 200
    assert resp.get_json()['name'] == 'A2'
    assert resp.get_json()['template'] == 'a2'


def test_delete(client):
    t = client.post('/api/initial-prompt-templates',
                    json={'name': 'A', 'template': 'a'}).get_json()
    assert client.delete(f"/api/initial-prompt-templates/{t['id']}").status_code == 200
    assert client.delete(f"/api/initial-prompt-templates/{t['id']}").status_code == 404


def test_cannot_touch_other_users_template(client, ctx):
    other = _mk_user()
    try:
        foreign = InitialPromptTemplate(user_id=other.id, name='X', template='x')
        db.session.add(foreign)
        db.session.commit()
        fid = foreign.id
        # The logged-in user (client) must not see, edit, or delete it.
        assert all(t['id'] != fid for t in client.get('/api/initial-prompt-templates').get_json())
        assert client.put(f'/api/initial-prompt-templates/{fid}', json={'name': 'hack'}).status_code == 404
        assert client.delete(f'/api/initial-prompt-templates/{fid}').status_code == 404
    finally:
        InitialPromptTemplate.query.filter_by(user_id=other.id).delete()
        db.session.delete(other)
        db.session.commit()


def test_create_defaults_seeds_then_noops(client):
    resp = client.post('/api/initial-prompt-templates/create-defaults')
    assert resp.status_code == 201
    seeded = resp.get_json()['templates']
    assert len(seeded) >= 1
    assert sum(1 for t in seeded if t['is_default']) == 1
    # Second call is a no-op because the user now has templates.
    again = client.post('/api/initial-prompt-templates/create-defaults')
    assert again.status_code == 200
