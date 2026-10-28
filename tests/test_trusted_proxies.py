"""TRUSTED_PROXIES limits X-Forwarded-* to listed peers (#498, #501).

Unset keeps the old behaviour: the headers are read from any peer.
"""

import os
import sys
from unittest.mock import patch

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.proxy import TrustedProxyMiddleware, parse_trusted_proxies

FORGED = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https",
          "X-Forwarded-Host": "attacker.example"}


def _probe(peer, env):
    probe = Flask("probe")

    @probe.route("/")
    def seen():
        return jsonify(addr=request.remote_addr, secure=request.is_secure, host=request.host)

    probe.wsgi_app = TrustedProxyMiddleware(probe.wsgi_app)
    with patch.dict(os.environ, env, clear=False):
        for key in ("TRUSTED_PROXIES", "TRUSTED_PROXY_HOPS"):
            if key not in env:
                os.environ.pop(key, None)
        return probe.test_client().get("/", headers=FORGED,
                                       environ_overrides={"REMOTE_ADDR": peer}).get_json()


def test_unset_list_keeps_the_old_default():
    seen = _probe("198.51.100.5", {})
    assert seen == {"addr": "203.0.113.7", "secure": True, "host": "attacker.example"}


def test_hops_zero_ignores_the_headers():
    seen = _probe("198.51.100.5", {"TRUSTED_PROXY_HOPS": "0"})
    assert seen == {"addr": "198.51.100.5", "secure": False, "host": "localhost"}


def test_a_peer_outside_the_list_cannot_set_address_scheme_or_host():
    seen = _probe("198.51.100.5", {"TRUSTED_PROXIES": "172.16.0.0/12, 10.0.0.1"})
    assert seen == {"addr": "198.51.100.5", "secure": False, "host": "localhost"}


def test_a_listed_proxy_still_forwards_the_client():
    for peer in ("172.18.0.3", "10.0.0.1", "::ffff:172.18.0.3"):
        seen = _probe(peer, {"TRUSTED_PROXIES": "172.16.0.0/12,10.0.0.1"})
        assert seen == {"addr": "203.0.113.7", "secure": True, "host": "attacker.example"}, peer


def test_invalid_entries_are_skipped():
    assert [str(n) for n in parse_trusted_proxies("nonsense, 10.0.0.0/8,,")] == ["10.0.0.0/8"]
    seen = _probe("198.51.100.5", {"TRUSTED_PROXIES": "nonsense"})
    assert seen["addr"] == "198.51.100.5"


def test_the_app_uses_the_middleware():
    from src.app import app
    assert isinstance(app.wsgi_app, TrustedProxyMiddleware)


def test_login_limit_follows_the_peer_when_a_list_is_set():
    """Rate limits key on request.remote_addr, so a forged X-Forwarded-For
    from an unlisted peer no longer opens a new bucket (#501)."""
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limited = Flask("limited")
    limiter = Limiter(get_remote_address, app=limited, storage_uri="memory://")

    @limited.route("/login", methods=["POST"])
    @limiter.limit("3 per minute")
    def login():
        return "ok"

    limited.wsgi_app = TrustedProxyMiddleware(limited.wsgi_app)
    client = limited.test_client()
    with patch.dict(os.environ, {"TRUSTED_PROXIES": "10.0.0.1"}):
        codes = [client.post("/login", headers={"X-Forwarded-For": f"203.0.113.{i}"},
                             environ_overrides={"REMOTE_ADDR": "198.51.100.5"}).status_code
                 for i in range(1, 6)]
        assert codes == [200, 200, 200, 429, 429]
        via_proxy = [client.post("/login", headers={"X-Forwarded-For": f"203.0.113.{i}"},
                                 environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code
                     for i in range(101, 106)]
        assert via_proxy == [200] * 5
