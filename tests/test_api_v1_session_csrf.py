"""Browser sessions on the CSRF-exempt API blueprints (#478).

api_v1_bp and webhooks_bp are exempt from CSRF so that token clients need
no CSRF token. A request signed in by the browser session or the remember-me
cookie must still present one; token clients keep working unchanged.
"""

import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.app import app, db
from src.models import User, APIToken, Recording, Tag
from src.utils.token_auth import hash_token


@pytest.fixture
def csrf_on():
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with app.app_context():
            yield
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved


def _make_user():
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"csrfv1_{suffix}", email=f"csrfv1_{suffix}@local.test",
                password='placeholder-bcrypt-hash')
    db.session.add(user)
    db.session.commit()
    return user


def _make_token(user):
    plaintext = uuid.uuid4().hex
    db.session.add(APIToken(user_id=user.id, token_hash=hash_token(plaintext), name='test'))
    db.session.commit()
    return plaintext


def _session_client(user):
    from flask import g
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = user.get_id()
        sess['_fresh'] = True
    g.pop('_login_user', None)
    return client


def _csrf_token(client):
    page = client.get('/account')
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert m, 'Could not find csrf_token in /account page'
    return m.group(1).decode()


def _recording(user):
    rec = Recording(user_id=user.id, title='original', status='COMPLETED')
    db.session.add(rec)
    db.session.commit()
    return rec


def _is_csrf_rejection(resp):
    return resp.status_code == 400 and b'csrf_reason' in resp.data


def test_session_patch_without_csrf_token_is_rejected(csrf_on):
    user = _make_user()
    rec = _recording(user)
    client = _session_client(user)
    resp = client.patch(f'/api/v1/recordings/{rec.id}', json={'title': 'changed'},
                        headers={'Origin': 'https://attacker.example'})
    assert _is_csrf_rejection(resp), (resp.status_code, resp.data)
    db.session.refresh(rec)
    assert rec.title == 'original'


def test_session_delete_without_csrf_token_is_rejected(csrf_on):
    user = _make_user()
    tag = Tag(name='keep-me', user_id=user.id)
    db.session.add(tag)
    db.session.commit()
    client = _session_client(user)
    resp = client.delete(f'/api/v1/tags/{tag.id}')
    assert _is_csrf_rejection(resp), (resp.status_code, resp.data)
    assert db.session.get(Tag, tag.id) is not None


def test_session_post_to_webhooks_without_csrf_token_is_rejected(csrf_on):
    user = _make_user()
    client = _session_client(user)
    resp = client.post('/api/v1/webhooks', json={
        'name': 'x', 'url': 'https://example.invalid/hook', 'events': ['recording.created']})
    assert _is_csrf_rejection(resp), (resp.status_code, resp.data)


def test_remember_cookie_without_csrf_token_is_rejected(csrf_on):
    """A remember-me cookie alone signs the user in, so it counts as a session."""
    user = _make_user()
    client = app.test_client()
    client.set_cookie(app.config.get('REMEMBER_COOKIE_NAME', 'remember_token'), 'anything')
    resp = client.post('/api/v1/tags', json={'name': 'from-remember'})
    assert _is_csrf_rejection(resp), (resp.status_code, resp.data)
    assert Tag.query.filter_by(user_id=user.id, name='from-remember').first() is None


def test_session_patch_with_csrf_token_succeeds(csrf_on):
    user = _make_user()
    rec = _recording(user)
    client = _session_client(user)
    token = _csrf_token(client)
    resp = client.patch(f'/api/v1/recordings/{rec.id}', json={'title': 'renamed'},
                        headers={'X-CSRFToken': token})
    assert resp.status_code == 200, (resp.status_code, resp.data)
    db.session.refresh(rec)
    assert rec.title == 'renamed'


def test_session_get_needs_no_csrf_token(csrf_on):
    user = _make_user()
    client = _session_client(user)
    assert client.get('/api/v1/stats').status_code == 200


@pytest.mark.parametrize('how', ['header', 'query'])
def test_token_clients_still_need_no_csrf_token(csrf_on, how):
    user = _make_user()
    plaintext = _make_token(user)
    client = app.test_client()
    if how == 'header':
        resp = client.post('/api/v1/tags', json={'name': 'tok-header'},
                           headers={'Authorization': f'Bearer {plaintext}'})
        assert resp.status_code == 201, (resp.status_code, resp.data)
    else:
        # #500: ?token= is GET-only; a write with it gets 401, not a CSRF 400.
        resp = client.post(f'/api/v1/tags?token={plaintext}', json={'name': 'tok-query'})
        assert resp.status_code == 401, (resp.status_code, resp.data)
        assert resp.get_json()['code'] == 'query_token_read_only'


def test_session_with_header_token_needs_no_csrf_token(csrf_on):
    """Automation that also carries a cookie keeps the header-token path."""
    user = _make_user()
    plaintext = _make_token(user)
    client = _session_client(user)
    resp = client.post('/api/v1/tags', json={'name': 'tok-and-session'},
                       headers={'Authorization': f'Bearer {plaintext}'})
    assert resp.status_code == 201, (resp.status_code, resp.data)


def test_share_target_view_exemption_is_unchanged(csrf_on):
    user = _make_user()
    client = _session_client(user)
    resp = client.post('/share-target', data={})
    assert not _is_csrf_rejection(resp), (resp.status_code, resp.data)


def teardown_module(module):
    with app.app_context():
        for u in User.query.filter(User.username.like('csrfv1_%')).all():
            Tag.query.filter_by(user_id=u.id).delete()
            Recording.query.filter_by(user_id=u.id).delete()
            APIToken.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()
