"""CSRF rejections tell the caller why, and whether a fresh token can help (#388).

Flask-WTF checks the Referer header on every HTTPS POST. A reverse proxy that
strips it, or forwards a different Host, fails that check on every request.
Its default answer is an HTML 400 page mentioning neither "csrf" nor "token",
so the frontend refreshed the token, retried, failed identically and showed
"Could not start summary reprocessing" with nothing in the server log.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User


@pytest.fixture
def client():
    previous = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'csrf_{suffix}', email=f'csrf_{suffix}@local.test',
                    password='placeholder-bcrypt-hash')
        db.session.add(user)
        db.session.commit()
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
            sess['_fresh'] = True
        yield c
    app.config['WTF_CSRF_ENABLED'] = previous


def _post(client, base_url, headers=None, token=True, **kwargs):
    h = dict(headers or {})
    if token:
        h['X-CSRFToken'] = client.get('/api/csrf-token', base_url=base_url).get_json()['csrf_token']
    kwargs.setdefault('data', '{}')
    kwargs.setdefault('content_type', 'application/json')
    return client.post('/recording/999999/reset_status', base_url=base_url, headers=h, **kwargs)


def test_missing_referer_over_https_is_json_and_not_retryable(client):
    r = _post(client, 'https://speakr.example')
    assert r.status_code == 400
    body = r.get_json()
    assert body['csrf_retryable'] is False
    assert 'Referer' in body['error']
    assert 'referrer header is missing' in body['csrf_reason'].lower()


def test_host_mismatch_names_both_hosts(client):
    r = _post(client, 'https://speakr:8899', headers={'Referer': 'https://speakr.example/recordings'})
    body = r.get_json()
    assert r.status_code == 400
    assert body['csrf_retryable'] is False
    assert 'speakr.example' in body['error'] and 'speakr:8899' in body['error']
    assert 'TRUSTED_PROXY_HOPS' in body['error']


def test_missing_token_is_retryable(client):
    r = _post(client, 'http://speakr.example', token=False)
    body = r.get_json()
    assert r.status_code == 400
    assert body['csrf_retryable'] is True
    assert 'token' in body['csrf_reason'].lower()


def test_valid_same_origin_request_passes_csrf(client):
    r = _post(client, 'https://speakr.example', headers={'Referer': 'https://speakr.example/'})
    assert r.status_code != 400


def test_browser_form_post_keeps_the_html_page(client):
    r = client.post('/recording/999999/reset_status', base_url='http://speakr.example',
                    data={'x': '1'}, headers={'Accept': 'text/html,application/xhtml+xml'})
    assert r.status_code == 400
    assert r.mimetype == 'text/html'


def test_rejection_is_logged_with_hosts(client, caplog):
    with caplog.at_level('WARNING'):
        _post(client, 'https://speakr:8899', headers={'Referer': 'https://speakr.example/'})
    logged = ' '.join(rec.getMessage() for rec in caplog.records)
    assert 'CSRF validation failed' in logged
    assert 'speakr.example' in logged and 'speakr:8899' in logged
