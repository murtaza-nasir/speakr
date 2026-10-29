"""SSO sign-in through the real Authlib client against a local OpenID provider.

The other SSO tests mock Authlib. These run the whole exchange: discovery,
the token request, ID token signature and claim checks, and the sign-in.
They guard Authlib upgrades: a provider that puts client_id in the JWS
header must still sign in (#298), and forged or mismatched ID tokens must
not.
"""
import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.app import app, db
from src.models import User

CLIENT_ID = "speakr-test"
CLIENT_SECRET = "test-secret"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(n):
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class Provider:
    """A minimal OpenID provider; `next_token` shapes the next ID token."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.next_token = {}
        self.nonce = None
        self.claims = {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    self._json(provider.metadata())
                elif self.path == "/jwks":
                    pub = provider.key.public_key().public_numbers()
                    self._json({"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
                                          "n": _int_b64(pub.n), "e": _int_b64(pub.e)}]})
                elif self.path == "/userinfo":
                    self._json(provider.claims)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if self.path == "/token":
                    self._json({"access_token": "at", "token_type": "Bearer", "expires_in": 300,
                                "id_token": provider.id_token()})
                else:
                    self.send_response(404)
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def metadata(self):
        return {"issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token",
                "userinfo_endpoint": self.issuer + "/userinfo",
                "jwks_uri": self.issuer + "/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"]}

    def id_token(self):
        spec = self.next_token
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                  "nonce": self.nonce, **self.claims}
        claims.update(spec.get("claims", {}))
        header = {"alg": spec.get("alg", "RS256"), "kid": "k1", "typ": "JWT", **spec.get("header", {})}
        signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
        if header["alg"] == "none":
            return signing_input + "."
        key = self.other_key if spec.get("foreign_key") else self.key
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64(signature)}"


@pytest.fixture(scope="module")
def provider():
    p = Provider()
    yield p
    p.server.shutdown()


@pytest.fixture
def sso(provider, monkeypatch):
    import src.auth.sso as sso_module
    monkeypatch.setenv("ENABLE_SSO", "true")
    monkeypatch.setenv("SSO_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SSO_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("SSO_DISCOVERY_URL", provider.issuer + "/.well-known/openid-configuration")
    monkeypatch.setenv("SSO_REDIRECT_URI", "http://localhost/auth/sso/callback")
    monkeypatch.setenv("SSO_AUTO_REGISTER", "true")
    monkeypatch.setenv("AUTHLIB_INSECURE_TRANSPORT", "1")
    monkeypatch.setattr(sso_module, "_oauth", None)
    suffix = uuid.uuid4().hex[:8]
    provider.claims = {"sub": f"oidc-{suffix}", "email": f"oidc_{suffix}@local.test",
                       "email_verified": True, "preferred_username": f"oidc_{suffix}", "name": "OIDC Test"}
    provider.next_token = {}
    yield provider
    sso_module._oauth = None
    with app.app_context():
        user = User.query.filter_by(sso_subject=provider.claims["sub"]).first()
        if user:
            db.session.delete(user)
            db.session.commit()


def _sign_in(provider, **token):
    """Start a sign-in, let the provider issue an ID token shaped by `token`,
    and return whether the browser ended up signed in."""
    client = app.test_client()
    start = client.get("/auth/sso/login")
    assert start.status_code == 200
    body = start.get_data(as_text=True)
    url = body.split('url=', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == [CLIENT_ID]
    provider.nonce = query["nonce"][0]
    provider.next_token = token
    client.get(f"/auth/sso/callback?code=c&state={query['state'][0]}")
    with client.session_transaction() as session:
        return session.get("_user_id") is not None


def test_valid_id_token_signs_in(sso):
    assert _sign_in(sso)


def test_client_id_in_the_jws_header_still_signs_in(sso):
    """CAS puts client_id in the ID token header; Authlib 1.7 refused it (#298)."""
    assert _sign_in(sso, header={"client_id": CLIENT_ID})


def test_wrong_nonce_is_refused(sso):
    assert not _sign_in(sso, claims={"nonce": "not-the-nonce"})


def test_nonce_cannot_be_switched_off_by_the_token(sso):
    """A token claiming nonce_supported=false must not skip the nonce check."""
    assert not _sign_in(sso, claims={"nonce": "not-the-nonce", "nonce_supported": False})


def test_token_signed_with_another_key_is_refused(sso):
    assert not _sign_in(sso, foreign_key=True)


def test_unsigned_token_is_refused(sso):
    assert not _sign_in(sso, alg="none")


def test_token_for_another_client_is_refused(sso):
    assert not _sign_in(sso, claims={"aud": "another-client"})


def test_token_from_another_issuer_is_refused(sso):
    assert not _sign_in(sso, claims={"iss": "https://evil.example"})


def test_expired_token_is_refused(sso):
    assert not _sign_in(sso, claims={"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200})
