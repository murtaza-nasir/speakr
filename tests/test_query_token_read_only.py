"""?token= is deprecated and read-only (#500).

A personal API token in the ?token= query parameter is accepted on GET and
HEAD only, and every response to such a request carries Deprecation: true.
Any other method that supplies the token only as ?token= gets 401 with the
code query_token_read_only, before the CSRF check and before the view runs.
Header tokens are unchanged.

SHARED-DB: users, tokens, tags and recordings are removed afterwards.
"""

import logging
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, Tag, User
from src.utils import token_auth
from src.utils.token_auth import hash_token


@pytest.fixture
def world():
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    plain = f"tok-{secrets.token_urlsafe(16)}"
    with app.app_context():
        n = f"qtok_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Original title", status="COMPLETED",
                      original_filename="q.wav", notes="original notes",
                      transcription="Speaker 1: hello.")
        db.session.add(r)
        t = APIToken(user_id=u.id, token_hash=hash_token(plain), name="q")
        db.session.add(t)
        db.session.commit()
        ids = dict(user=u.id, rec=r.id, token_id=t.id)
    try:
        yield dict(plain=plain, **ids)
    finally:
        app.config['WTF_CSRF_ENABLED'] = saved
        with app.app_context():
            Tag.query.filter_by(user_id=ids['user']).delete()
            Recording.query.filter_by(user_id=ids['user']).delete()
            APIToken.query.filter_by(user_id=ids['user']).delete()
            db.session.delete(db.session.get(User, ids['user']))
            db.session.commit()


def _title(rid):
    with app.app_context():
        rec = db.session.get(Recording, rid)
        return None if rec is None else rec.title


def _tag_count(uid):
    with app.app_context():
        return Tag.query.filter_by(user_id=uid).count()


def test_get_with_query_token_works_and_is_marked_deprecated(world):
    with app.test_client() as c:
        resp = c.get(f"/api/v1/recordings?token={world['plain']}")
        head = c.head(f"/api/v1/recordings?token={world['plain']}")
    assert resp.status_code == 200
    assert resp.headers.get('Deprecation') == 'true'
    assert head.status_code == 200
    assert head.headers.get('Deprecation') == 'true'


def test_header_token_carries_no_deprecation_header(world):
    with app.test_client() as c:
        resp = c.get("/api/v1/recordings", headers={'Authorization': f"Bearer {world['plain']}"})
    assert resp.status_code == 200
    assert 'Deprecation' not in resp.headers


def _assert_refused(resp):
    assert resp.status_code == 401, (resp.status_code, resp.data)
    body = resp.get_json()
    assert body['code'] == 'query_token_read_only'
    assert '?token=' in body['error']
    assert resp.headers.get('Deprecation') == 'true'


def test_post_with_query_token_only_is_refused(world):
    with app.test_client() as c:
        resp = c.post(f"/api/v1/tags?token={world['plain']}", json={'name': 'via-query'})
    _assert_refused(resp)
    assert _tag_count(world['user']) == 0


def test_put_with_query_token_only_is_refused(world):
    with app.test_client() as c:
        resp = c.put(f"/api/v1/recordings/{world['rec']}/notes?token={world['plain']}",
                     json={'notes': 'changed'})
    _assert_refused(resp)
    with app.app_context():
        assert db.session.get(Recording, world['rec']).notes == 'original notes'


def test_patch_with_query_token_only_is_refused(world):
    with app.test_client() as c:
        resp = c.patch(f"/api/v1/recordings/{world['rec']}?token={world['plain']}",
                       json={'title': 'Renamed'})
    _assert_refused(resp)
    assert _title(world['rec']) == 'Original title'


def test_delete_with_query_token_only_is_refused(world):
    with app.test_client() as c:
        resp = c.delete(f"/api/v1/recordings/{world['rec']}?token={world['plain']}")
    _assert_refused(resp)
    assert _title(world['rec']) == 'Original title'


def test_query_token_write_outside_v1_gets_401_not_a_csrf_error(world):
    with app.test_client() as c:
        resp = c.post(f"/api/tokens?token={world['plain']}", json={'name': 'x'})
    _assert_refused(resp)


def test_header_token_on_post_still_works(world):
    with app.test_client() as c:
        resp = c.post("/api/v1/tags", json={'name': 'via-header'},
                      headers={'Authorization': f"Bearer {world['plain']}"})
        resp2 = c.post("/api/v1/tags", json={'name': 'via-x-header'},
                       headers={'X-API-Token': world['plain']})
    assert resp.status_code == 201, (resp.status_code, resp.data)
    assert resp2.status_code == 201, (resp2.status_code, resp2.data)
    assert 'Deprecation' not in resp.headers


def test_header_token_wins_over_query_on_post(world):
    with app.test_client() as c:
        resp = c.post(f"/api/v1/tags?token={world['plain']}", json={'name': 'both'},
                      headers={'Authorization': f"Bearer {world['plain']}"})
    assert resp.status_code == 201, (resp.status_code, resp.data)
    assert 'Deprecation' not in resp.headers


def test_invalid_query_token_on_post_is_a_plain_auth_failure(world):
    with app.test_client() as c:
        resp = c.post("/api/v1/tags?token=not-a-real-token", json={'name': 'x'})
    assert resp.status_code == 401
    assert resp.get_json()['code'] == 'authentication_required'
    assert 'Deprecation' not in resp.headers


def test_query_token_warning_is_logged_once_per_token(world, caplog):
    token_auth._query_token_warned.discard(world['token_id'])
    with caplog.at_level(logging.WARNING, logger='src.utils.token_auth'):
        with app.test_client() as c:
            for _ in range(3):
                assert c.get(f"/api/v1/recordings?token={world['plain']}").status_code == 200
    lines = [r for r in caplog.records
             if r.name == 'src.utils.token_auth' and 'deprecated ?token=' in r.getMessage()]
    assert len(lines) == 1
    assert world['plain'] not in lines[0].getMessage()


def test_openapi_marks_the_query_token_deprecated_and_get_only():
    with app.app_context(), app.test_request_context():
        from src.api.api_v1 import _openapi_with_scopes
        spec = _openapi_with_scopes()
    scheme = spec['components']['securitySchemes']['apiKeyQuery']
    assert scheme['x-deprecated'] is True
    assert 'GET' in scheme['description'] and 'Deprecated' in scheme['description']
    writes = gets = 0
    for path, ops in spec['paths'].items():
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            if method in ('get', 'head'):
                gets += 1
                assert 'security' not in op or op['security'] == [] or any(
                    'apiKeyQuery' in s for s in op['security']), (path, method)
            else:
                writes += 1
                assert not any('apiKeyQuery' in s for s in op.get('security', [])), (path, method)
    assert writes and gets
    assert {'apiKeyQuery': []} in spec['security']


def test_api_description_is_public():
    """#503: the API description is available without signing in."""
    with app.test_client() as c:
        spec = c.get("/api/v1/openapi.json")
        docs = c.get("/api/v1/docs")
    assert spec.status_code == 200
    assert 'paths' in spec.get_json()
    assert docs.status_code == 200
