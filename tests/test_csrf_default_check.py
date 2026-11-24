"""Flask-WTF's own CSRF check is on (#690).

WTF_CSRF_CHECK_DEFAULT is no longer turned off: the check runs in
Flask-WTF's csrf_protect before_request hook, through
TokenAwareCSRFProtect, which adds the per-request bypass for a valid
header token and the session rule of the exempt API blueprints (#478).
With every other before_request hook removed, a browser request without a
CSRF token is still refused, and token clients still write without one.

SHARED-DB: users, tokens and tags are removed afterwards.
"""

import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from flask_wtf.csrf import CSRFProtect

from src.app import app, db, csrf, TokenAwareCSRFProtect
from src.models import APIToken, Tag, User
from src.utils.token_auth import hash_token


@pytest.fixture
def world():
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"csrfdef_{suffix}", email=f"csrfdef_{suffix}@local.test",
                    password='placeholder-bcrypt-hash')
        db.session.add(user)
        db.session.commit()
        plain = uuid.uuid4().hex
        db.session.add(APIToken(user_id=user.id, token_hash=hash_token(plain), name='t'))
        db.session.commit()
        ids = dict(user=user.id, session_id=user.get_id(), plain=plain)
    try:
        yield ids
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved
        with app.app_context():
            db.session.rollback()
            Tag.query.filter_by(user_id=ids['user']).delete()
            APIToken.query.filter_by(user_id=ids['user']).delete()
            User.query.filter_by(id=ids['user']).delete()
            db.session.commit()


@pytest.fixture
def only_flask_wtf_hook():
    """Leave Flask-WTF's csrf_protect as the only app-wide before_request hook."""
    hooks = app.before_request_funcs[None]
    saved = list(hooks)
    hooks[:] = [h for h in hooks if _is_flask_wtf_hook(h)]
    assert len(hooks) == 1
    try:
        yield
    finally:
        hooks[:] = saved


def _is_flask_wtf_hook(hook):
    return hook.__name__ == 'csrf_protect' and hook.__module__ == 'flask_wtf.csrf'


def _session_client(world):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = world['session_id']
        sess['_fresh'] = True
    return client


def _csrf_token(client):
    page = client.get('/account')
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert m, 'no csrf_token in the /account page'
    return m.group(1).decode()


def _is_csrf_rejection(resp):
    return resp.status_code == 400 and b'csrf_reason' in resp.data


def _tag_names(world):
    with app.app_context():
        return {t.name for t in Tag.query.filter_by(user_id=world['user'])}


def test_flask_wtf_default_check_is_on():
    assert app.config['WTF_CSRF_CHECK_DEFAULT'] is True
    assert isinstance(csrf, TokenAwareCSRFProtect)
    hooks = app.before_request_funcs[None]
    names = [h.__name__ for h in hooks]
    assert sum(_is_flask_wtf_hook(h) for h in hooks) == 1
    assert 'csrf_token_aware_check' not in names
    # The query-token refusal answers before the CSRF check, as before.
    assert names.index('refuse_query_token_writes') < names.index('csrf_protect')


@pytest.mark.parametrize('path,body', [
    ('/api/tags', {'name': 'no-csrf-web'}),          # a web-UI route
    ('/api/v1/tags', {'name': 'no-csrf-v1'}),        # an exempt blueprint, with a session (#478)
])
def test_session_write_without_csrf_token_is_refused_by_flask_wtf_alone(world, only_flask_wtf_hook, path, body):
    resp = _session_client(world).post(path, json=body)
    assert _is_csrf_rejection(resp), (resp.status_code, resp.data)
    assert body['name'] not in _tag_names(world)


def test_session_write_with_csrf_token_passes_flask_wtf_alone(world, only_flask_wtf_hook):
    client = _session_client(world)
    token = _csrf_token(client)
    resp = client.post('/api/v1/tags', json={'name': 'with-csrf'}, headers={'X-CSRFToken': token})
    assert resp.status_code == 201, (resp.status_code, resp.data)


@pytest.mark.parametrize('header', ['Authorization', 'X-API-Token', 'API-Token'])
def test_header_token_writes_need_no_csrf_token(world, header):
    value = f"Bearer {world['plain']}" if header == 'Authorization' else world['plain']
    with app.test_client() as c:
        v1 = c.post('/api/v1/tags', json={'name': f'v1-{header}'}, headers={header: value})
        web = c.post('/api/tags', json={'name': f'web-{header}'}, headers={header: value})
    assert v1.status_code == 201, (v1.status_code, v1.data)
    assert web.status_code in (200, 201), (web.status_code, web.data)
    assert {f'v1-{header}', f'web-{header}'} <= _tag_names(world)


def test_header_token_write_works_with_flask_wtf_alone(world, only_flask_wtf_hook):
    """The bypass lives in the CSRFProtect that Flask-WTF's hook calls."""
    with app.test_client() as c:
        resp = c.post('/api/tags', json={'name': 'bypass'},
                      headers={'Authorization': f"Bearer {world['plain']}"})
    assert not _is_csrf_rejection(resp), resp.data


def test_a_plain_csrfprotect_refuses_the_header_token_bypass(world, monkeypatch):
    """Without the subclass the token write fails closed with a CSRF error."""
    monkeypatch.setattr(csrf, 'protect', CSRFProtect.protect.__get__(csrf))
    with app.test_client() as c:
        resp = c.post('/api/tags', json={'name': 'plain'},
                      headers={'Authorization': f"Bearer {world['plain']}"})
    assert _is_csrf_rejection(resp), (resp.status_code, resp.data)
    assert 'plain' not in _tag_names(world)


def test_invalid_header_token_gets_401_not_a_csrf_error(world):
    with app.test_client() as c:
        resp = c.post('/api/tags', json={'name': 'bad'}, headers={'Authorization': 'Bearer not-a-token'})
    assert resp.status_code == 401 and resp.get_json()['code'] == 'authentication_required'
    assert resp.headers['WWW-Authenticate'] == 'Bearer error="invalid_token"'


def test_exempt_views_and_token_free_routes_are_unchanged(world):
    """The share target stays exempt for a session; a GET needs no token."""
    from src.api.recordings import share_target
    assert f"{share_target.__module__}.{share_target.__name__}" in csrf._exempt_views
    client = _session_client(world)
    assert client.get('/api/v1/stats').status_code == 200
    resp = client.post('/share-target', data={})
    assert not _is_csrf_rejection(resp), (resp.status_code, resp.data)
