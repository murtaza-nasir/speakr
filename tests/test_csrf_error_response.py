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


# --- Form posts with an expired or missing token (sign-out, other forms) ---

FORM = {'Accept': 'text/html,application/xhtml+xml'}


def _form(client, path, headers):
    return client.post(path, data={'x': '1'},
                       headers={**FORM, **headers})


def _signed_in(client):
    with client.session_transaction() as sess:
        return bool(sess.get('_user_id'))


def test_sign_out_with_an_expired_token_from_speakr_signs_out(client):
    r = _form(client, '/logout', {'Origin': 'http://localhost'})
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    assert not _signed_in(client)


def test_sign_out_with_only_a_same_site_referer_signs_out(client):
    r = _form(client, '/logout', {'Referer': 'http://localhost/recordings'})
    assert r.status_code == 302
    assert not _signed_in(client)


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://attacker.example'},
    {'Origin': 'null'},
    {'Referer': 'https://attacker.example/page'},
    {},
])
def test_sign_out_from_another_site_is_still_refused(client, headers):
    r = _form(client, '/logout', headers)
    assert r.status_code == 400
    assert _signed_in(client)


def test_another_form_with_an_expired_token_goes_back_with_a_message(client):
    r = _form(client, '/recording/999999/reset_status', {'Referer': 'http://localhost/account'})
    assert r.status_code == 302 and r.headers['Location'] == 'http://localhost/account'
    with client.session_transaction() as sess:
        flashes = sess.get('_flashes') or []
    assert any(category == 'warning' for category, _ in flashes)
    assert _signed_in(client)


def test_another_form_from_another_site_keeps_the_400(client):
    r = _form(client, '/recording/999999/reset_status', {'Origin': 'https://attacker.example',
                                                        'Referer': 'https://attacker.example/x'})
    assert r.status_code == 400
