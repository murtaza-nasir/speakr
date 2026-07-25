"""The app must set baseline security headers itself, so deployments without
a hardening reverse proxy are still protected. Previously only X-Robots-Tag
was set, leaving direct/plain-proxy installs with no clickjacking, MIME-sniff,
or CSP defense.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app


def _headers(path='/login', secure=False):
    client = app.test_client()
    env = {'HTTP_X_FORWARDED_PROTO': 'https'} if secure else {}
    return client.get(path, environ_overrides=env).headers


def test_clickjacking_and_mime_headers_present():
    h = _headers()
    assert h.get('X-Frame-Options') == 'SAMEORIGIN'
    assert h.get('X-Content-Type-Options') == 'nosniff'
    assert h.get('Referrer-Policy') == 'strict-origin-when-cross-origin'
    assert 'Permissions-Policy' in h


def test_csp_present_and_locks_dangerous_directives():
    csp = _headers().get('Content-Security-Policy', '')
    assert csp, 'CSP header missing'
    # These are the directives that add value even with the unavoidable
    # 'unsafe-inline'/'unsafe-eval' on scripts (Vue runtime compiler).
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "default-src 'self'" in csp


def test_hsts_only_over_https():
    assert 'Strict-Transport-Security' not in _headers(secure=False)
    assert 'Strict-Transport-Security' in _headers(secure=True)


def test_permissions_policy_allows_mic_denies_geolocation():
    pp = _headers().get('Permissions-Policy', '')
    assert 'microphone=(self)' in pp
    assert 'geolocation=()' in pp
