"""SSO access tokens on the API, and the token exchange.

A fake OIDC provider (discovery document and JWKS) is served by patching the
fetch in src/auth/sso_access_tokens.py, with keys generated here. Every check
on a token must hold; a failure answers exactly like an invalid API token.

SHARED-DB: users, tokens, recordings and the three setting rows are removed
or restored afterwards.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from joserfc import jwt as jose_jwt
from joserfc.jwk import ECKey, RSAKey

from src.app import app, db
from src.auth import sso_access_tokens as sat
from src.models import APIToken, Recording, SystemSetting, User
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False

ISSUER = "https://id.example.test"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
CLIENT = "openwebui-client"
MAILR = "mailr-client"
SETTING_KEYS = ("sso_api_tokens_enabled", "sso_api_allowed_clients", "sso_api_scopes",
                "sso_api_exchange_clients")


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Provider:
    """Discovery document and JWKS, counted per URL."""

    def __init__(self):
        self.rsa = RSAKey.generate_key(2048, parameters={"kid": "rsa-1"}, private=True)
        self.ec = ECKey.generate_key("P-256", parameters={"kid": "ec-1"}, private=True)
        self.keys = [self.rsa, self.ec]
        self.calls = {DISCOVERY: 0, JWKS_URI: 0}

    def fetch(self, url):
        self.calls[url] = self.calls.get(url, 0) + 1
        if url == DISCOVERY:
            return {"issuer": ISSUER, "jwks_uri": JWKS_URI, "authorization_endpoint": f"{ISSUER}/authorize"}
        if url == JWKS_URI:
            return {"keys": [k.as_dict(private=False) for k in self.keys]}
        raise AssertionError(f"unexpected fetch of {url}")

    def token(self, sub, key=None, alg="RS256", **claims):
        key = key or self.rsa
        now = int(time.time())
        body = {"iss": ISSUER, "sub": sub, "aud": CLIENT, "iat": now, "exp": now + 300}
        body.update(claims)
        body = {k: v for k, v in body.items() if v is not None}
        header = {"alg": alg, "typ": "at+jwt"}
        if key.kid:
            header["kid"] = key.kid
        return jose_jwt.encode(header, body, key, algorithms=[alg])


@pytest.fixture
def provider(monkeypatch):
    p = Provider()
    sat.clear_cache()
    monkeypatch.setattr(sat, "_http_get_json", p.fetch)
    yield p
    sat.clear_cache()


@pytest.fixture
def settings(monkeypatch):
    """SSO on, the three settings from .env (rows hold no value)."""
    monkeypatch.setenv("ENABLE_SSO", "true")
    monkeypatch.setenv("SSO_DISCOVERY_URL", DISCOVERY)
    monkeypatch.setenv("SSO_API_TOKENS_ENABLED", "true")
    monkeypatch.setenv("SSO_API_ALLOWED_CLIENTS", f"{CLIENT}, mailr-client")
    monkeypatch.setenv("SSO_API_SCOPES", "read")
    monkeypatch.setenv("SSO_API_EXCHANGE_CLIENTS", MAILR)
    with app.app_context():
        saved = {r.key: (r.value, r.setting_type) for r in
                 SystemSetting.query.filter(SystemSetting.key.in_(SETTING_KEYS)).all()}
        for key in SETTING_KEYS:
            SystemSetting.set_setting(key, None, None, "boolean" if key.endswith("enabled") else "string")
    yield monkeypatch
    with app.app_context():
        for key in SETTING_KEYS:
            if key in saved:
                SystemSetting.set_setting(key, saved[key][0], None, saved[key][1])
            else:
                row = SystemSetting.query.filter_by(key=key).first()
                if row is not None:
                    db.session.delete(row)
                    db.session.commit()


@pytest.fixture
def world(provider, settings):
    made = {}
    with app.app_context():
        users = {}
        for label in ("alice", "bob"):
            n = f"sso_{label}_{secrets.token_hex(4)}"
            u = User(username=n, email=f"{n}@local.test", password="x",
                     sso_provider="SSO", sso_subject=f"sub-{n}")
            db.session.add(u)
            db.session.commit()
            users[label] = (u.id, u.sso_subject, u.username)
        r = Recording(user_id=users["alice"][0], title="Original title", status="COMPLETED",
                      original_filename="s.wav", transcription="Speaker 1: hello.")
        db.session.add(r)
        db.session.commit()
        made.update(users=users, rec=r.id)
    app.test_client_class = _Client
    made["client"] = app.test_client()
    made["provider"] = provider
    made["settings"] = settings
    yield made
    with app.app_context():
        db.session.rollback()
        for uid, _, _ in made["users"].values():
            APIToken.query.filter_by(user_id=uid).delete()
        obj = db.session.get(Recording, made["rec"])
        if obj is not None:
            db.session.delete(obj)
        for uid, _, _ in made["users"].values():
            obj = db.session.get(User, uid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _alice(world):
    return world["users"]["alice"]


def _invalid_token_answer(world):
    """What a request with an unknown API token gets."""
    resp = world["client"].get("/api/v1/users/me", headers=_bearer("not-a-real-token"))
    assert resp.status_code == 401
    return resp.get_json()


# --- Accepted ----------------------------------------------------------------

def test_valid_token_reads_as_its_user(world):
    uid, sub, username = _alice(world)
    resp = world["client"].get("/api/v1/users/me", headers=_bearer(world["provider"].token(sub)))
    assert resp.status_code == 200
    assert resp.get_json()["username"] == username


def test_ec_signed_token_is_accepted(world):
    _, sub, username = _alice(world)
    p = world["provider"]
    resp = world["client"].get("/api/v1/users/me", headers=_bearer(p.token(sub, key=p.ec, alg="ES256")))
    assert resp.status_code == 200
    assert resp.get_json()["username"] == username


def test_write_route_refused_with_scope_error_when_scopes_are_read(world):
    uid, sub, _ = _alice(world)
    resp = world["client"].patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Changed"},
                                 headers=_bearer(world["provider"].token(sub)))
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "insufficient_scope"
    assert body["required_scopes"] == ["write"] and body["token_scopes"] == ["read"]
    with app.app_context():
        assert db.session.get(Recording, world["rec"]).title == "Original title"


def test_write_route_allowed_when_scopes_include_write(world):
    _, sub, _ = _alice(world)
    world["settings"].setenv("SSO_API_SCOPES", "read,write")
    resp = world["client"].patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Changed"},
                                 headers=_bearer(world["provider"].token(sub)))
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Recording, world["rec"]).title == "Changed"


def test_system_setting_overrides_env(world):
    _, sub, _ = _alice(world)
    with app.app_context():
        SystemSetting.set_setting("sso_api_tokens_enabled", "false", None, "boolean")
    resp = world["client"].get("/api/v1/users/me", headers=_bearer(world["provider"].token(sub)))
    assert resp.status_code == 401


def test_allowed_by_azp_or_by_aud_list(world):
    _, sub, _ = _alice(world)
    p = world["provider"]
    by_azp = p.token(sub, aud="some-api", azp="mailr-client")
    by_list = p.token(sub, aud=["other", CLIENT])
    for tok in (by_azp, by_list):
        assert world["client"].get("/api/v1/users/me", headers=_bearer(tok)).status_code == 200


def test_current_token_describes_the_sso_token(world):
    _, sub, _ = _alice(world)
    body = world["client"].get("/api/v1/tokens/current",
                               headers=_bearer(world["provider"].token(sub, aud="x", azp="mailr-client"))).get_json()
    assert body["id"] is None and body["sso_client_id"] == "mailr-client"
    assert body["scopes"] == ["read"] and body["via"] == "header"


def test_rate_limit_keys_are_per_user_and_sso(world):
    uid, sub, _ = _alice(world)
    from src.utils.token_auth import token_rate_key, user_rate_key, current_api_token
    with app.test_request_context("/api/v1/users/me", headers=_bearer(world["provider"].token(sub))):
        assert current_api_token().scope_set == frozenset({"read"})
        assert token_rate_key() == f"token:sso-{uid}"
        assert user_rate_key() == f"user:{uid}"


def test_jwks_and_discovery_are_cached(world):
    _, sub, _ = _alice(world)
    p = world["provider"]
    for _ in range(4):
        assert world["client"].get("/api/v1/users/me", headers=_bearer(p.token(sub))).status_code == 200
    assert p.calls[DISCOVERY] == 1
    assert p.calls[JWKS_URI] == 1


def test_unknown_kid_refetches_once_for_a_rotated_key(world):
    _, sub, _ = _alice(world)
    p = world["provider"]
    assert world["client"].get("/api/v1/users/me", headers=_bearer(p.token(sub))).status_code == 200
    assert p.calls[JWKS_URI] == 1
    # The provider rotates: a new key appears in its JWKS.
    new = RSAKey.generate_key(2048, parameters={"kid": "rsa-2"}, private=True)
    p.keys = [new, p.ec]
    tok = p.token(sub, key=new)
    assert world["client"].get("/api/v1/users/me", headers=_bearer(tok)).status_code == 200
    assert p.calls[JWKS_URI] == 2
    # A kid nobody publishes does not cause a fetch per request.
    stray = RSAKey.generate_key(2048, parameters={"kid": "rsa-stray"}, private=True)
    for _ in range(3):
        assert world["client"].get("/api/v1/users/me", headers=_bearer(p.token(sub, key=stray))).status_code == 401
    assert p.calls[JWKS_URI] == 2


def test_unknown_kid_refused_when_rotation_does_not_publish_it(world):
    _, sub, _ = _alice(world)
    p = world["provider"]
    stray = RSAKey.generate_key(2048, parameters={"kid": "rsa-9"}, private=True)
    assert world["client"].get("/api/v1/users/me", headers=_bearer(p.token(sub, key=stray))).status_code == 401
    assert p.calls[JWKS_URI] == 1  # first fetch only; it was fresh, so no refetch


# --- Refused -----------------------------------------------------------------

def _forge_unsigned(claims, alg):
    header = _b64(json.dumps({"alg": alg, "typ": "JWT", "kid": "rsa-1"}).encode())
    payload = _b64(json.dumps(claims).encode())
    return header, payload


def _claims(sub, **extra):
    now = int(time.time())
    body = {"iss": ISSUER, "sub": sub, "aud": CLIENT, "iat": now, "exp": now + 300}
    body.update(extra)
    return body


def _refused_cases(world):
    _, sub, _ = _alice(world)
    p = world["provider"]
    now = int(time.time())
    header, payload = _forge_unsigned(_claims(sub), "none")
    alg_none = f"{header}.{payload}."
    # Key confusion: HS256 with the provider's public key as the secret.
    pem = p.rsa.as_pem(private=False)
    header, payload = _forge_unsigned(_claims(sub), "HS256")
    mac = hmac.new(pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    hs256 = f"{header}.{payload}.{_b64(mac)}"
    other = RSAKey.generate_key(2048, parameters={"kid": "rsa-1"}, private=True)
    return {
        "wrong iss": p.token(sub, iss="https://evil.example.test"),
        "expired": p.token(sub, iat=now - 3600, exp=now - 600),
        "nbf in future": p.token(sub, nbf=now + 3600),
        "iat in future": p.token(sub, iat=now + 3600, exp=now + 7200),
        "no exp": p.token(sub, exp=None),
        "aud not allowed": p.token(sub, aud="someone-else"),
        "azp not allowed": p.token(sub, aud="someone-else", azp="also-not"),
        "alg none": alg_none,
        "HS256 key confusion": hs256,
        "wrong signing key, same kid": p.token(sub, key=other),
        "tampered payload": _tamper(p.token(sub)),
        "unknown sub": p.token("sub-nobody"),
        "no sub": p.token(None),
    }


def _tamper(tok):
    h, b, s = tok.split(".")
    claims = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    claims["sub"] = "sub-someone-else"
    return f"{h}.{_b64(json.dumps(claims).encode())}.{s}"


@pytest.mark.parametrize("case", [
    "wrong iss", "expired", "nbf in future", "iat in future", "no exp", "aud not allowed",
    "azp not allowed", "alg none", "HS256 key confusion", "wrong signing key, same kid",
    "tampered payload", "unknown sub", "no sub",
])
def test_refused_tokens_answer_like_an_invalid_api_token(world, case, caplog):
    expected = _invalid_token_answer(world)
    tok = _refused_cases(world)[case]
    with caplog.at_level("INFO", logger="src.auth.sso_access_tokens"):
        resp = world["client"].get("/api/v1/users/me", headers=_bearer(tok))
    assert resp.status_code == 401, case
    assert resp.get_json() == expected
    assert tok not in caplog.text  # the reason is logged, never the token


def test_inactive_user_is_refused(world, monkeypatch):
    uid, sub, _ = _alice(world)
    monkeypatch.setattr(User, "is_active", property(lambda self: self.id != uid))
    resp = world["client"].get("/api/v1/users/me", headers=_bearer(world["provider"].token(sub)))
    assert resp.status_code == 401


def test_email_is_never_used_to_match(world):
    uid, _, username = _alice(world)
    with app.app_context():
        email = db.session.get(User, uid).email
    tok = world["provider"].token("sub-unlinked", email=email, email_verified=True)
    assert world["client"].get("/api/v1/users/me", headers=_bearer(tok)).status_code == 401


@pytest.mark.parametrize("env, value", [
    ("SSO_API_TOKENS_ENABLED", "false"),
    ("ENABLE_SSO", "false"),
    ("SSO_API_ALLOWED_CLIENTS", ""),
    ("SSO_API_SCOPES", "nonsense"),
])
def test_feature_off_or_unconfigured_refuses(world, env, value):
    _, sub, _ = _alice(world)
    world["settings"].setenv(env, value)
    resp = world["client"].get("/api/v1/users/me", headers=_bearer(world["provider"].token(sub)))
    assert resp.status_code == 401


def test_feature_off_fetches_nothing(world):
    _, sub, _ = _alice(world)
    world["settings"].setenv("SSO_API_TOKENS_ENABLED", "false")
    world["client"].get("/api/v1/users/me", headers=_bearer(world["provider"].token(sub)))
    assert world["provider"].calls == {DISCOVERY: 0, JWKS_URI: 0}


def test_opaque_token_is_not_checked_with_the_provider(world):
    resp = world["client"].get("/api/v1/users/me", headers=_bearer("opaque-access-token-value"))
    assert resp.status_code == 401
    assert world["provider"].calls == {DISCOVERY: 0, JWKS_URI: 0}


def test_sso_token_in_query_string_is_not_accepted(world):
    _, sub, _ = _alice(world)
    resp = world["client"].get(f"/api/v1/users/me?token={world['provider'].token(sub)}")
    assert resp.status_code == 401


@pytest.mark.parametrize("header", ["X-API-Token", "API-Token"])
def test_sso_token_only_in_the_authorization_header(world, header):
    _, sub, _ = _alice(world)
    resp = world["client"].get("/api/v1/users/me", headers={header: world["provider"].token(sub)})
    assert resp.status_code == 401


def test_jku_header_is_never_fetched(world):
    _, sub, _ = _alice(world)
    p = world["provider"]
    now = int(time.time())
    tok = jose_jwt.encode({"alg": "RS256", "kid": "rsa-1", "jku": "https://evil.example.test/jwks"},
                          _claims(sub, iat=now), p.rsa)
    # The token is valid; jku is ignored and only the discovery jwks_uri is used.
    assert world["client"].get("/api/v1/users/me", headers=_bearer(tok)).status_code == 200
    assert set(p.calls) == {DISCOVERY, JWKS_URI}


def test_provider_unreachable_is_401_not_500(world, monkeypatch):
    _, sub, _ = _alice(world)

    def down(url):
        raise ConnectionError("down")
    monkeypatch.setattr(sat, "_http_get_json", down)
    sat.clear_cache()
    assert world["client"].get("/api/v1/users/me", headers=_bearer(world["provider"].token(sub))).status_code == 401


# --- Exchange ----------------------------------------------------------------

def _mailr(world, sub, **claims):
    """An access token issued to the app that may exchange."""
    claims.setdefault("aud", MAILR)
    return world["provider"].token(sub, **claims)


def _exchange(world, token, **body):
    return world["client"].post("/api/v1/tokens/exchange", json=body, headers=_bearer(token))


def test_exchange_creates_a_usable_scoped_api_token(world):
    uid, sub, username = _alice(world)
    world["settings"].setenv("SSO_API_SCOPES", "read,write")
    resp = _exchange(world, _mailr(world, sub),
                     name="mailr sync", scopes=["read"], expires_in_days=30)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "mailr sync" and body["scopes"] == ["read"]
    assert body["sso_client_id"] == "mailr-client" and body["expires_at"]
    plain = body["token"]
    with app.app_context():
        row = APIToken.query.filter_by(token_hash=hash_token(plain)).one()
        assert row.user_id == uid and row.sso_client_id == "mailr-client"
        assert json.loads(row.scopes) == ["read"]
    me = world["client"].get("/api/v1/users/me", headers=_bearer(plain))
    assert me.status_code == 200 and me.get_json()["username"] == username
    refused = world["client"].patch(f"/api/v1/recordings/{world['rec']}", json={"title": "x"},
                                    headers=_bearer(plain))
    assert refused.status_code == 403 and refused.get_json()["code"] == "insufficient_scope"


def test_exchanged_token_is_listed_and_revocable_in_account_settings(world):
    uid, sub, _ = _alice(world)
    plain = _exchange(world, _mailr(world, sub), name="Open WebUI").get_json()["token"]
    c = world["client"]
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    listed = [t for t in c.get("/api/tokens").get_json()["tokens"] if t["name"] == "Open WebUI"]
    assert len(listed) == 1 and listed[0]["sso_client_id"] == MAILR
    assert c.delete(f"/api/tokens/{listed[0]['id']}").status_code == 200
    with c.session_transaction() as s:
        s.clear()
    assert c.get("/api/v1/users/me", headers=_bearer(plain)).status_code == 401


def test_exchange_defaults_to_every_allowed_scope(world):
    _, sub, _ = _alice(world)
    world["settings"].setenv("SSO_API_SCOPES", "write, read upload")
    body = _exchange(world, _mailr(world, sub), name="all").get_json()
    assert body["scopes"] == ["read", "upload", "write"]


@pytest.mark.parametrize("scopes", [["read", "delete"], ["full"], ["write"], [], "read", ["bogus"]])
def test_exchange_refuses_scopes_beyond_the_maximum(world, scopes):
    uid, sub, _ = _alice(world)
    resp = _exchange(world, _mailr(world, sub), name="t", scopes=scopes)
    assert resp.status_code == 400
    with app.app_context():
        assert APIToken.query.filter_by(user_id=uid).count() == 0


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": 5}, {"name": "x" * 101},
                                  {"name": "t", "expires_in_days": -1}, {"name": "t", "expires_in_days": "3"},
                                  {"name": "t", "expires_in_days": True}])
def test_exchange_validates_its_body(world, body):
    _, sub, _ = _alice(world)
    assert _exchange(world, _mailr(world, sub), **body).status_code == 400


def test_exchange_refuses_an_api_token(world):
    uid, _, _ = _alice(world)
    plain = f"tok-{secrets.token_urlsafe(16)}"
    with app.app_context():
        for scopes in (None, json.dumps(["read"])):
            db.session.add(APIToken(user_id=uid, token_hash=hash_token(plain + str(scopes)), name="t", scopes=scopes))
        db.session.commit()
    for scopes in (None, json.dumps(["read"])):
        resp = _exchange(world, plain + str(scopes), name="minted")
        assert resp.status_code == 403
        assert resp.get_json()["code"] == "sso_token_required"
    with app.app_context():
        assert APIToken.query.filter_by(user_id=uid, name="minted").count() == 0


def test_exchange_refuses_a_session(world):
    uid, sub, _ = _alice(world)
    c = world["client"]
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    assert c.post("/api/v1/tokens/exchange", json={"name": "x"}).status_code == 403
    # A session wins over a bearer header, as everywhere else.
    resp = c.post("/api/v1/tokens/exchange", json={"name": "x"}, headers=_bearer(_mailr(world, sub)))
    assert resp.status_code == 403
    with app.app_context():
        assert APIToken.query.filter_by(user_id=uid).count() == 0


def test_exchange_without_any_token_is_401(world):
    assert world["client"].post("/api/v1/tokens/exchange", json={"name": "x"}).status_code == 401


@pytest.mark.parametrize("exchange_clients", ["", "openwebui-client", "mailr-client, other", "not-allowed-anywhere"])
def test_exchange_needs_a_client_in_both_lists(world, exchange_clients):
    uid, sub, _ = _alice(world)
    world["settings"].setenv("SSO_API_EXCHANGE_CLIENTS", exchange_clients)
    if exchange_clients == "not-allowed-anywhere":
        # In the exchange list but not in the allowed list: the token is refused outright.
        tok = world["provider"].token(sub, aud="not-allowed-anywhere")
        assert _exchange(world, tok, name="x").status_code == 401
        return
    # Open WebUI's token reads, but may exchange only when its client is listed.
    owui = world["provider"].token(sub)
    assert world["client"].get("/api/v1/users/me", headers=_bearer(owui)).status_code == 200
    resp = _exchange(world, owui, name="owui")
    if "openwebui-client" in exchange_clients:
        assert resp.status_code == 201
    else:
        assert resp.status_code == 403 and resp.get_json()["code"] == "sso_exchange_not_allowed"
    resp = _exchange(world, _mailr(world, sub), name="mailr")
    assert resp.status_code == (201 if MAILR in exchange_clients else 403)
    with app.app_context():
        expected = int("openwebui-client" in exchange_clients) + int(MAILR in exchange_clients)
        assert APIToken.query.filter_by(user_id=uid).count() == expected


def test_exchange_records_the_exchanging_client_from_an_aud_list(world):
    uid, sub, _ = _alice(world)
    tok = world["provider"].token(sub, aud=["openwebui-client", MAILR])
    body = _exchange(world, tok, name="both").get_json()
    assert body["sso_client_id"] == MAILR


def test_exchange_with_an_invalid_sso_token_is_401(world):
    _, sub, _ = _alice(world)
    assert _exchange(world, _mailr(world, sub, iss="https://evil.example.test"), name="x").status_code == 401


def test_exchange_respects_the_token_cap(world, monkeypatch):
    uid, sub, _ = _alice(world)
    import src.api.tokens as tokens_module
    monkeypatch.setattr(tokens_module, "API_MAX_TOKENS_PER_USER", 2)
    tok = _mailr(world, sub)
    assert _exchange(world, tok, name="one").status_code == 201
    assert _exchange(world, tok, name="two").status_code == 201
    resp = _exchange(world, tok, name="three")
    assert resp.status_code == 409 and resp.get_json()["code"] == "token_limit_reached"
    with app.app_context():
        assert APIToken.query.filter_by(user_id=uid).count() == 2


def test_exchange_is_not_blocked_by_csrf(world):
    _, sub, _ = _alice(world)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = _exchange(world, _mailr(world, sub), name="csrf")
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert resp.status_code == 201


def test_exchange_for_the_other_user_maps_by_subject(world):
    bob_id, bob_sub, _ = world["users"]["bob"]
    body = _exchange(world, _mailr(world, bob_sub), name="bob").get_json()
    with app.app_context():
        assert APIToken.query.filter_by(token_hash=hash_token(body["token"])).one().user_id == bob_id


# --- OpenAPI -----------------------------------------------------------------

def test_openapi_served_without_a_token_and_leaves_out_the_exchange(world):
    # A tool server built from the document must never offer the exchange to a model.
    resp = world["client"].get("/api/v1/openapi.json")
    assert resp.status_code == 200
    spec = resp.get_json()
    assert "/tokens/exchange" not in spec["paths"]
    full = world["client"].get("/api/v1/openapi.json", headers=_bearer(world["provider"].token(_alice(world)[1]))).get_json()
    assert "/tokens/exchange" not in full["paths"]
    assert "SSO" in spec["components"]["securitySchemes"]["bearerAuth"]["description"]


def test_openapi_with_an_sso_token_lists_only_what_it_may_call(world):
    _, sub, _ = _alice(world)
    spec = world["client"].get("/api/v1/openapi.json", headers=_bearer(_mailr(world, sub))).get_json()
    for operations in spec["paths"].values():
        for op in operations.values():
            assert set(op["x-required-scopes"]) <= {"read"}


def test_capabilities_report_the_exchange(world):
    _, sub, _ = _alice(world)
    body = world["client"].get("/api/v1/capabilities", headers=_bearer(_mailr(world, sub))).get_json()
    assert body["features"]["sso_token_exchange"] is True
