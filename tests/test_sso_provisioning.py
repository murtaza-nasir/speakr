"""Accounts created from SSO access tokens, the SSO status endpoint, and the
display names of SSO clients.

The fake provider of tests/test_sso_access_tokens.py (generated keys, patched
discovery and JWKS fetch) gains a userinfo endpoint, served by patching
_http_get_userinfo. With SSO_API_AUTO_REGISTER, SSO_AUTO_REGISTER and
SSO_API_TOKENS_ENABLED on, a valid token for an unknown subject creates the
account from userinfo; the exchange answers each refusal with its code, any
other route with the 401 of an invalid token.

SHARED-DB: users, tokens and setting rows are removed or restored afterwards.
"""

import json
import os
import secrets
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_sso_access_tokens import (  # noqa: E402
    CLIENT, DISCOVERY, ISSUER, JWKS_URI, MAILR, Provider, _Client, _bearer,
)

from src.app import app, db  # noqa: E402
from src.auth import sso as sso_module  # noqa: E402
from src.auth import sso_access_tokens as sat  # noqa: E402
from src.models import APIToken, SystemSetting, User  # noqa: E402
from src.utils.token_auth import hash_token  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False

USERINFO = f"{ISSUER}/api/oidc/userinfo"
SETTING_KEYS = ("sso_api_tokens_enabled", "sso_api_allowed_clients", "sso_api_scopes",
                "sso_api_exchange_clients", "sso_api_auto_register", "sso_api_client_names")


class UserinfoProvider(Provider):
    """The fake provider with a userinfo endpoint in its discovery document."""

    def __init__(self):
        super().__init__()
        self.people = {}          # sub -> claims
        self.userinfo_calls = []  # the access tokens userinfo was called with
        self.userinfo_answer = None  # a callable (url, token) -> (status, body), to override

    def fetch(self, url):
        doc = super().fetch(url)
        if url == DISCOVERY:
            doc = dict(doc, userinfo_endpoint=USERINFO)
        return doc

    def userinfo(self, url, access_token):
        assert url == USERINFO
        self.userinfo_calls.append(access_token)
        if self.userinfo_answer is not None:
            return self.userinfo_answer(url, access_token)
        from joserfc import jwt as jose_jwt
        sub = jose_jwt.decode(access_token, self.rsa).claims["sub"]
        claims = self.people.get(sub)
        return (200, dict(claims)) if claims is not None else (401, None)


@pytest.fixture
def world(monkeypatch):
    p = UserinfoProvider()
    sat.clear_cache()
    monkeypatch.setattr(sat, "_http_get_json", p.fetch)
    monkeypatch.setattr(sat, "_http_get_userinfo", p.userinfo)
    for key, value in {
        "ENABLE_SSO": "true", "SSO_DISCOVERY_URL": DISCOVERY, "SSO_PROVIDER_NAME": "TestID",
        "SSO_API_TOKENS_ENABLED": "true", "SSO_API_ALLOWED_CLIENTS": f"{CLIENT}, {MAILR}",
        "SSO_API_SCOPES": "read", "SSO_API_EXCHANGE_CLIENTS": MAILR,
        "SSO_API_AUTO_REGISTER": "true", "SSO_AUTO_REGISTER": "true",
        "SSO_REQUIRE_VERIFIED_EMAIL": "true", "SSO_DEFAULT_USERNAME_CLAIM": "preferred_username",
        "SSO_DEFAULT_NAME_CLAIM": "name", "SSO_API_CLIENT_NAMES": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SSO_ALLOWED_DOMAINS", raising=False)
    with app.app_context():
        saved = {r.key: (r.value, r.setting_type) for r in
                 SystemSetting.query.filter(SystemSetting.key.in_(SETTING_KEYS)).all()}
        for key in SETTING_KEYS:
            kind = "boolean" if key.endswith(("enabled", "register")) else "string"
            SystemSetting.set_setting(key, None, None, kind)
    app.test_client_class = _Client
    tag = secrets.token_hex(4)
    made = {"client": app.test_client(), "provider": p, "env": monkeypatch, "tag": tag, "local": []}
    yield made
    sat.clear_cache()
    with app.app_context():
        db.session.rollback()
        users = User.query.filter(User.sso_subject.like(f"sub-{tag}-%")).all()
        users += [u for u in (db.session.get(User, uid) for uid in made["local"]) if u is not None]
        for u in {u.id: u for u in users}.values():
            APIToken.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()
        for key in SETTING_KEYS:
            if key in saved:
                SystemSetting.set_setting(key, saved[key][0], None, saved[key][1])
            else:
                row = SystemSetting.query.filter_by(key=key).first()
                if row is not None:
                    db.session.delete(row)
                    db.session.commit()


def _person(world, label, **claims):
    """A person known to the provider only. Returns their sub."""
    sub = f"sub-{world['tag']}-{label}"
    email = f"{label}.{world['tag']}@example.test"
    body = {"sub": sub, "email": email, "email_verified": True,
            "preferred_username": f"{label}{world['tag']}", "name": f"{label.title()} Person"}
    body.update(claims)
    world["provider"].people[sub] = {k: v for k, v in body.items() if v is not None}
    return sub


def _local_user(world, email, sso_subject=None):
    with app.app_context():
        n = f"local_{secrets.token_hex(4)}"
        u = User(username=n, email=email, password="x", sso_subject=sso_subject,
                 sso_provider="TestID" if sso_subject else None)
        db.session.add(u)
        db.session.commit()
        world["local"].append(u.id)
        return u.id


def _mailr(world, sub, **claims):
    claims.setdefault("aud", MAILR)
    return world["provider"].token(sub, **claims)


def _exchange(world, token, **body):
    body.setdefault("name", "sync")
    return world["client"].post("/api/v1/tokens/exchange", json=body, headers=_bearer(token))


def _me(world, token):
    return world["client"].get("/api/v1/users/me", headers=_bearer(token))


def _account(sub):
    with app.app_context():
        return User.query.filter_by(sso_subject=sub).first()


def _invalid_token_answer(world):
    resp = world["client"].get("/api/v1/users/me", headers=_bearer("not-a-real-token"))
    assert resp.status_code == 401
    return resp.get_json()


# --- Provisioning off ----------------------------------------------------------

@pytest.mark.parametrize("env", ["SSO_API_AUTO_REGISTER", "SSO_AUTO_REGISTER"])
def test_provisioning_off_refuses_and_creates_nothing(world, env):
    world["env"].setenv(env, "false")
    sub = _person(world, "closed")
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "sso_registration_closed"
    assert resp.get_json()["error"]
    plain = _me(world, world["provider"].token(sub))
    assert plain.status_code == 401 and plain.get_json() == _invalid_token_answer(world)
    assert _account(sub) is None
    assert world["provider"].userinfo_calls == []


def test_provisioning_off_by_system_setting(world):
    with app.app_context():
        SystemSetting.set_setting("sso_api_auto_register", "false", None, "boolean")
    sub = _person(world, "setting")
    assert _exchange(world, _mailr(world, sub)).get_json()["code"] == "sso_registration_closed"
    assert _account(sub) is None


def test_feature_off_gives_401_on_the_exchange(world):
    # With SSO access tokens off, no token is valid, so nothing is provisioned.
    world["env"].setenv("SSO_API_TOKENS_ENABLED", "false")
    sub = _person(world, "off")
    assert _exchange(world, _mailr(world, sub)).status_code == 401
    assert _account(sub) is None


# --- Provisioning on -------------------------------------------------------------

def test_exchange_creates_the_account_and_a_working_token(world):
    sub = _person(world, "ada")
    tok = _mailr(world, sub)
    resp = _exchange(world, tok, name="sync")
    assert resp.status_code == 201
    body = resp.get_json()
    user = _account(sub)
    assert user is not None
    assert user.username == f"ada{world['tag']}"
    assert user.name == "Ada Person"
    assert user.email == f"ada.{world['tag']}@example.test"
    assert user.sso_provider == "TestID" and user.password is None
    assert user.ui_theme and user.theme_color_scheme and user.audio_player_position
    assert world["provider"].userinfo_calls == [tok]   # the same access token
    with app.app_context():
        row = APIToken.query.filter_by(token_hash=hash_token(body["token"])).one()
        assert row.user_id == user.id and row.sso_client_id == MAILR
    me = _me(world, body["token"])
    assert me.status_code == 200 and me.get_json()["username"] == user.username
    # A second exchange finds the account and does not call userinfo again.
    assert _exchange(world, _mailr(world, sub), name="again").status_code == 201
    assert len(world["provider"].userinfo_calls) == 1


def test_new_account_gets_the_default_quota(world):
    with app.app_context():
        saved = SystemSetting.get_setting("default_storage_quota_mb", None)
        SystemSetting.set_setting("default_storage_quota_mb", "2048", None, "integer")
    try:
        sub = _person(world, "quota")
        assert _exchange(world, _mailr(world, sub)).status_code == 201
        assert _account(sub).storage_quota_mb == 2048
    finally:
        with app.app_context():
            SystemSetting.set_setting("default_storage_quota_mb", saved, None, "integer")


def test_username_falls_back_to_the_email_local_part(world):
    sub = _person(world, "nouser", preferred_username=None)
    assert _exchange(world, _mailr(world, sub)).status_code == 201
    assert _account(sub).username == f"nouser.{world['tag']}"[:20]


def test_plain_api_call_creates_the_account(world):
    # A tool server (allowed, not an exchange client) calling the API for a
    # person who never signed in to Speakr.
    sub = _person(world, "tool")
    resp = _me(world, world["provider"].token(sub))
    assert resp.status_code == 200
    user = _account(sub)
    assert user is not None and resp.get_json()["username"] == user.username


def test_exchange_by_a_client_that_may_not_exchange_creates_nothing(world):
    sub = _person(world, "owui")
    resp = _exchange(world, world["provider"].token(sub))   # Open WebUI's client
    assert resp.status_code == 403 and resp.get_json()["code"] == "sso_exchange_not_allowed"
    assert _account(sub) is None
    assert world["provider"].userinfo_calls == []


def test_rfc9068_token_provisions_on_exchange(world):
    sub = _person(world, "pocket")
    tok = world["provider"].token(sub, typ="at+jwt", aud=[ISSUER], client_id=MAILR)
    assert _exchange(world, tok).status_code == 201
    assert _account(sub) is not None


# --- Refusals ------------------------------------------------------------------------

def test_existing_local_account_with_the_email_is_never_linked(world):
    sub = _person(world, "local")
    email = world["provider"].people[sub]["email"]
    uid = _local_user(world, email.upper())
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 409 and resp.get_json()["code"] == "sso_link_required"
    assert _me(world, world["provider"].token(sub)).status_code == 401
    with app.app_context():
        local = db.session.get(User, uid)
        assert local.sso_subject is None and local.sso_provider is None
        assert local.email == email.upper() and local.password == "x"
    assert _account(sub) is None


def test_account_linked_to_another_subject_is_refused(world):
    sub = _person(world, "other")
    email = world["provider"].people[sub]["email"]
    uid = _local_user(world, email, sso_subject=f"sub-{world['tag']}-someone-else")
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 409 and resp.get_json()["code"] == "sso_link_required"
    with app.app_context():
        assert db.session.get(User, uid).sso_subject == f"sub-{world['tag']}-someone-else"
    assert _account(sub) is None


def test_domain_not_allowed(world):
    world["env"].setenv("SSO_ALLOWED_DOMAINS", "corp.example")
    sub = _person(world, "domain")
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 403 and resp.get_json()["code"] == "sso_domain_not_allowed"
    assert _me(world, world["provider"].token(sub)).status_code == 401
    assert _account(sub) is None


def test_domain_allowed(world):
    world["env"].setenv("SSO_ALLOWED_DOMAINS", "corp.example, example.test")
    sub = _person(world, "domainok")
    assert _exchange(world, _mailr(world, sub)).status_code == 201


@pytest.mark.parametrize("verified", [False, "false", None])
def test_unverified_email_refused_when_verification_is_required(world, verified):
    sub = _person(world, "unverified", email_verified=verified)
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 403 and resp.get_json()["code"] == "sso_email_not_verified"
    assert _account(sub) is None


def test_unverified_email_accepted_when_verification_is_off(world):
    world["env"].setenv("SSO_REQUIRE_VERIFIED_EMAIL", "false")
    sub = _person(world, "trusted", email_verified=False)
    assert _exchange(world, _mailr(world, sub)).status_code == 201
    assert _account(sub) is not None


def test_unverified_email_still_never_links_when_verification_is_off(world):
    world["env"].setenv("SSO_REQUIRE_VERIFIED_EMAIL", "false")
    sub = _person(world, "trustedlink", email_verified=False)
    _local_user(world, world["provider"].people[sub]["email"])
    assert _exchange(world, _mailr(world, sub)).get_json()["code"] == "sso_link_required"


@pytest.mark.parametrize("email", [None, "", 5])
def test_missing_email_refused(world, email):
    sub = _person(world, "noemail", email=email)
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 403 and resp.get_json()["code"] == "sso_email_missing"
    assert _account(sub) is None


def _timeout(url, token):
    import requests
    raise requests.Timeout("slow")


@pytest.mark.parametrize("case", ["timeout", "500", "non-json", "list", "sub mismatch", "no sub", "401"])
def test_userinfo_failures_give_502_and_no_account(world, case, caplog):
    sub = _person(world, "uifail")
    answers = {
        "timeout": _timeout,
        "500": lambda url, tok: (500, None),
        "401": lambda url, tok: (401, None),
        "non-json": lambda url, tok: (200, None),
        "list": lambda url, tok: (200, ["not", "an", "object"]),
        "sub mismatch": lambda url, tok: (200, dict(world["provider"].people[sub], sub="sub-other")),
        "no sub": lambda url, tok: (200, {"email": "x@example.test", "email_verified": True}),
    }
    world["provider"].userinfo_answer = answers[case]
    tok = _mailr(world, sub)
    with caplog.at_level("INFO", logger="src.auth.sso_access_tokens"):
        resp = _exchange(world, tok)
    assert resp.status_code == 502 and resp.get_json()["code"] == "sso_userinfo_failed"
    assert tok not in caplog.text
    assert "sso_userinfo_failed" in caplog.text
    assert _me(world, world["provider"].token(sub)).status_code == 401
    assert _account(sub) is None


def test_discovery_without_userinfo_endpoint_gives_502(world, monkeypatch):
    p = world["provider"]
    monkeypatch.setattr(sat, "_http_get_json", lambda url: Provider.fetch(p, url))
    sat.clear_cache()
    sub = _person(world, "noendpoint")
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 502 and resp.get_json()["code"] == "sso_userinfo_failed"
    assert p.userinfo_calls == []


def test_userinfo_http_fetch_has_timeouts_and_no_redirects(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"sub": "s"}

    def fake_get(url, **kwargs):
        seen.update(kwargs, url=url)
        return _Resp()
    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    assert sat._http_get_userinfo("https://id.example.test/userinfo", "tok-value") == (200, {"sub": "s"})
    assert seen["allow_redirects"] is False and seen["timeout"] == sat.HTTP_TIMEOUT
    assert seen["headers"]["Authorization"] == "Bearer tok-value"


def test_invalid_tokens_never_call_userinfo(world):
    sub = _person(world, "invalid")
    p = world["provider"]
    from joserfc.jwk import RSAKey
    other = RSAKey.generate_key(2048, parameters={"kid": "rsa-1"}, private=True)
    bad = [
        _mailr(world, sub, iss="https://evil.example.test"),
        _mailr(world, sub, exp=1),
        _mailr(world, sub, aud="someone-else"),
        _mailr(world, sub, nonce="n"),
        p.token(sub, key=other, aud=MAILR),
    ]
    for tok in bad:
        assert _exchange(world, tok).status_code == 401
        assert _me(world, tok).status_code == 401
    assert p.userinfo_calls == []
    assert _account(sub) is None


# --- Concurrency -------------------------------------------------------------------

def test_a_concurrent_winner_is_reused_not_duplicated(world, monkeypatch):
    """The other request commits the account between this request's lookup and
    its own commit: the unique sso_subject refuses the second row, and the
    account that won is used."""
    sub = _person(world, "race")
    claims = world["provider"].people[sub]
    real = sso_module.generate_unique_username
    state = {"done": False}

    def competitor_first(preferred):
        if not state["done"]:
            state["done"] = True
            from sqlalchemy.orm import Session
            with Session(db.engine) as other:
                other.add(User(username=f"winner{world['tag']}", email=claims["email"],
                               password=None, sso_provider="TestID", sso_subject=sub))
                other.commit()
        return real(preferred)
    monkeypatch.setattr(sso_module, "generate_unique_username", competitor_first)
    # The competitor commits after this request checked email and subject.
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 201
    with app.app_context():
        accounts = User.query.filter_by(sso_subject=sub).all()
        assert len(accounts) == 1 and accounts[0].username == f"winner{world['tag']}"
        assert APIToken.query.filter_by(user_id=accounts[0].id).count() == 1


def test_concurrent_first_requests_create_one_account(world):
    sub = _person(world, "threads")
    p = world["provider"]
    barrier = threading.Barrier(2, timeout=10)
    real_userinfo = p.userinfo

    def both_at_once(url, token):
        result = real_userinfo(url, token)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return result
    world["env"].setattr(sat, "_http_get_userinfo", both_at_once)
    results = []

    def call():
        client = app.test_client()
        results.append(client.get("/api/v1/users/me", headers=_bearer(p.token(sub))).status_code)
    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [200, 200]
    with app.app_context():
        assert User.query.filter_by(sso_subject=sub).count() == 1


# --- Interactive sign-in is unchanged ----------------------------------------------

def test_interactive_sign_in_still_links_by_verified_email(world):
    email = f"interactive.{world['tag']}@example.test"
    uid = _local_user(world, email)
    sub = f"sub-{world['tag']}-interactive"
    with app.app_context():
        user = sso_module.create_or_update_sso_user(
            {"sub": sub, "email": email, "email_verified": True, "name": "Linked"})
        assert user.id == uid and user.sso_subject == sub and user.name == "Linked"


def test_interactive_sign_in_still_creates_placeholder_email_accounts(world):
    sub = f"sub-{world['tag']}-placeholder"
    with app.app_context():
        user = sso_module.create_or_update_sso_user({"sub": sub, "preferred_username": "ph"})
        assert user.email == f"{sub}@placeholder.local"
        assert user.sso_provider == "TestID" and user.ui_theme


def test_after_interactive_link_the_token_works(world):
    sub = _person(world, "later")
    email = world["provider"].people[sub]["email"]
    uid = _local_user(world, email)
    assert _exchange(world, _mailr(world, sub)).status_code == 409
    with app.app_context():
        sso_module.create_or_update_sso_user(dict(world["provider"].people[sub]))
    assert _exchange(world, _mailr(world, sub)).status_code == 201
    assert _account(sub).id == uid


# --- Capabilities and /sso/status -----------------------------------------------------

def test_capability_flag(world):
    sub = _person(world, "caps")
    tok = _mailr(world, sub)
    body = world["client"].get("/api/v1/capabilities", headers=_bearer(tok)).get_json()
    assert body["features"]["sso_exchange_provisioning"] is True
    world["env"].setenv("SSO_AUTO_REGISTER", "false")
    body = world["client"].get("/api/v1/capabilities", headers=_bearer(tok)).get_json()
    assert body["features"]["sso_exchange_provisioning"] is False


def _status(world, query="", **kwargs):
    return world["client"].get(f"/api/v1/sso/status{query}", **kwargs)


def test_status_without_auth(world):
    resp = _status(world)
    assert resp.status_code == 200
    assert resp.get_json() == {"sso_token_exchange": True, "sso_exchange_provisioning": True,
                               "exchange_client_allowed": None}
    text = resp.get_data(as_text=True)
    for secret in (MAILR, CLIENT, ISSUER, "speakr_version", "version"):
        assert secret not in text


@pytest.mark.parametrize("client_id, allowed", [
    (MAILR, True), (CLIENT, False), ("unknown", False), ("", False),
])
def test_status_with_client_id(world, client_id, allowed):
    body = _status(world, f"?client_id={client_id}").get_json()
    assert body["exchange_client_allowed"] is allowed
    assert set(body) == {"sso_token_exchange", "sso_exchange_provisioning", "exchange_client_allowed"}


def test_status_exchange_listed_but_not_allowed_is_false(world):
    world["env"].setenv("SSO_API_EXCHANGE_CLIENTS", f"{MAILR}, only-exchange")
    assert _status(world, "?client_id=only-exchange").get_json()["exchange_client_allowed"] is False


@pytest.mark.parametrize("env, value, expected", [
    ("SSO_API_TOKENS_ENABLED", "false", (False, False)),
    ("ENABLE_SSO", "false", (False, False)),
    ("SSO_API_EXCHANGE_CLIENTS", "", (False, False)),
    ("SSO_API_AUTO_REGISTER", "false", (True, False)),
    ("SSO_AUTO_REGISTER", "false", (True, False)),
])
def test_status_follows_the_switches(world, env, value, expected):
    world["env"].setenv(env, value)
    body = _status(world, f"?client_id={MAILR}").get_json()
    assert (body["sso_token_exchange"], body["sso_exchange_provisioning"]) == expected
    assert body["exchange_client_allowed"] is expected[0]


def test_status_ignores_a_token_and_provisions_nothing(world):
    sub = _person(world, "status")
    for headers in (_bearer(_mailr(world, sub)), _bearer("garbage-token")):
        resp = _status(world, headers=headers)
        assert resp.status_code == 200
    assert world["provider"].userinfo_calls == []
    assert _account(sub) is None


def test_status_is_in_the_openapi_document_without_security(world):
    spec = world["client"].get("/api/v1/openapi.json").get_json()
    op = spec["paths"]["/sso/status"]["get"]
    assert op["security"] == [] and op["operationId"] == "get_sso_status"


def test_status_has_a_per_ip_rate_limit():
    from src.api.api_v1 import get_sso_status
    assert getattr(get_sso_status, "_rate_limit", None) == "30 per minute"
    with app.test_request_context("/api/v1/sso/status"):
        from src.app import exempt_status_endpoints
        assert exempt_status_endpoints() is False


# --- Client display names --------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("", {}),
    ("mailr-client=Mail App", {"mailr-client": "Mail App"}),
    (" a = Alpha , b=Beta\nc=Gamma\r\nd=Delta ", {"a": "Alpha", "b": "Beta", "c": "Gamma", "d": "Delta"}),
    ("novalue=, =noid, plain, x==y", {"x": "=y"}),
    ("long=" + "N" * 200, {"long": "N" * sat.CLIENT_NAME_MAX}),
    ("dup=One, dup=Two", {"dup": "Two"}),
])
def test_client_names_parse_leniently(monkeypatch, raw, expected):
    monkeypatch.setenv("SSO_API_CLIENT_NAMES", raw)
    with app.app_context():
        row = SystemSetting.query.filter_by(key="sso_api_client_names").first()
        saved = (row.value, row.setting_type) if row is not None else None
        SystemSetting.set_setting("sso_api_client_names", None, None, "string")
        try:
            assert sat.client_names() == expected
        finally:
            if saved is not None:
                SystemSetting.set_setting("sso_api_client_names", saved[0], None, saved[1])


def test_token_to_dict_names_the_sso_client(world):
    sub = _person(world, "named")
    world["env"].setenv("SSO_API_CLIENT_NAMES", f"{MAILR}=Mail Sync, other=Other")
    plain = _exchange(world, _mailr(world, sub)).get_json()["token"]
    with app.app_context():
        row = APIToken.query.filter_by(token_hash=hash_token(plain)).one()
        assert row.to_dict()["sso_client_name"] == "Mail Sync"
        assert row.to_dict()["sso_client_id"] == MAILR
    world["env"].setenv("SSO_API_CLIENT_NAMES", "other=Other")
    with app.app_context():
        row = APIToken.query.filter_by(token_hash=hash_token(plain)).one()
        assert row.to_dict()["sso_client_name"] is None
        plain_row = APIToken(user_id=row.user_id, token_hash=hash_token("x" + plain), name="manual",
                             scopes=json.dumps(["read"]))
        assert plain_row.to_dict()["sso_client_name"] is None


def test_token_list_carries_the_name_and_not_the_mapping(world):
    sub = _person(world, "listed")
    world["env"].setenv("SSO_API_CLIENT_NAMES", f"{MAILR}=Mail Sync, hidden-client=Hidden App")
    assert _exchange(world, _mailr(world, sub), name="listed").status_code == 201
    uid = _account(sub).id
    c = world["client"]
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    resp = c.get("/api/tokens")
    tokens = [t for t in resp.get_json()["tokens"] if t["name"] == "listed"]
    assert tokens[0]["sso_client_name"] == "Mail Sync"
    assert "Hidden App" not in resp.get_data(as_text=True)
    with c.session_transaction() as s:
        s.clear()


# --- /tokens/current names the exchanging client ---------------------------------------

def test_current_token_of_an_exchanged_token_names_its_client(world):
    sub = _person(world, "current")
    world["env"].setenv("SSO_API_CLIENT_NAMES", f"{MAILR}=Mail Sync")
    plain = _exchange(world, _mailr(world, sub)).get_json()["token"]
    body = world["client"].get("/api/v1/tokens/current", headers=_bearer(plain)).get_json()
    assert body["id"] is not None
    assert body["sso_client_id"] == MAILR and body["sso_client_name"] == "Mail Sync"
    world["env"].setenv("SSO_API_CLIENT_NAMES", "")
    body = world["client"].get("/api/v1/tokens/current", headers=_bearer(plain)).get_json()
    assert body["sso_client_id"] == MAILR and body["sso_client_name"] is None


def test_current_token_of_a_manual_token_has_no_client(world):
    uid = _local_user(world, f"manual.{world['tag']}@example.test")
    plain = f"tok-{secrets.token_urlsafe(16)}"
    with app.app_context():
        db.session.add(APIToken(user_id=uid, token_hash=hash_token(plain), name="manual",
                                scopes=json.dumps(["read"])))
        db.session.commit()
    body = world["client"].get("/api/v1/tokens/current", headers=_bearer(plain)).get_json()
    assert body["name"] == "manual"
    assert body["sso_client_id"] is None and body["sso_client_name"] is None


def test_current_token_of_an_sso_access_token_carries_the_name(world):
    sub = _person(world, "currentsso")
    world["env"].setenv("SSO_API_CLIENT_NAMES", f"{CLIENT}=Open WebUI")
    body = world["client"].get("/api/v1/tokens/current",
                               headers=_bearer(world["provider"].token(sub))).get_json()
    assert body["id"] is None
    assert body["sso_client_id"] == CLIENT and body["sso_client_name"] == "Open WebUI"


# --- Only under /api/v1 ------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/account", "/api/recordings"])
def test_no_account_is_created_outside_api_v1(world, path):
    sub = _person(world, "outside")
    world["client"].get(path, headers=_bearer(world["provider"].token(sub)))
    assert world["provider"].userinfo_calls == []
    assert _account(sub) is None
    # The same token on an API v1 route then creates the account.
    assert _me(world, world["provider"].token(sub)).status_code == 200
    assert _account(sub) is not None


def test_validate_without_a_request_creates_nothing(world):
    sub = _person(world, "norequest")
    with app.app_context():
        assert sat.validate(world["provider"].token(sub)) is None
    assert world["provider"].userinfo_calls == []
    assert _account(sub) is None


# --- Refusal cache -------------------------------------------------------------------------

def _expire(key):
    with sat._cache_lock:
        code, reason, _ = sat._refusals[key]
        sat._refusals[key] = (code, reason, 0.0)


@pytest.mark.parametrize("label, claims, setup, code", [
    ("cachelink", {}, "local", "sso_link_required"),
    ("cachedomain", {}, "domain", "sso_domain_not_allowed"),
    ("cacheunverified", {"email_verified": False}, None, "sso_email_not_verified"),
    ("cachenoemail", {"email": None}, None, "sso_email_missing"),
])
def test_policy_refusal_is_cached_for_five_minutes(world, label, claims, setup, code):
    sub = _person(world, label, **claims)
    if setup == "local":
        _local_user(world, world["provider"].people[sub]["email"])
    if setup == "domain":
        world["env"].setenv("SSO_ALLOWED_DOMAINS", "corp.example")
    p = world["provider"]
    first = _exchange(world, _mailr(world, sub))
    assert first.get_json()["code"] == code and len(p.userinfo_calls) == 1
    for _ in range(3):
        again = _exchange(world, _mailr(world, sub))
        assert again.status_code == first.status_code and again.get_json()["code"] == code
        assert _me(world, p.token(sub)).status_code == 401
    assert len(p.userinfo_calls) == 1
    key = (ISSUER, sub)
    remaining = sat._refusals[key][2] - __import__("time").monotonic()
    assert sat.REFUSAL_TTL - 5 < remaining <= sat.REFUSAL_TTL
    _expire(key)
    assert _exchange(world, _mailr(world, sub)).get_json()["code"] == code
    assert len(p.userinfo_calls) == 2


def test_userinfo_failure_is_cached_for_a_minute_and_then_retried(world):
    sub = _person(world, "cachefail")
    p = world["provider"]
    p.userinfo_answer = lambda url, tok: (500, None)
    assert _exchange(world, _mailr(world, sub)).status_code == 502
    p.userinfo_answer = None   # the provider recovers
    resp = _exchange(world, _mailr(world, sub))
    assert resp.status_code == 502 and resp.get_json()["code"] == "sso_userinfo_failed"
    assert len(p.userinfo_calls) == 1
    key = (ISSUER, sub)
    remaining = sat._refusals[key][2] - __import__("time").monotonic()
    assert sat.USERINFO_FAILURE_TTL - 5 < remaining <= sat.USERINFO_FAILURE_TTL
    _expire(key)
    assert _exchange(world, _mailr(world, sub)).status_code == 201
    assert len(p.userinfo_calls) == 2 and _account(sub) is not None
    assert key not in sat._refusals


def test_cache_entry_is_cleared_when_the_account_is_created(world):
    sub = _person(world, "cacheclear")
    sat._remember_refusal((ISSUER, sub), "sso_email_missing", "test")
    _expire((ISSUER, sub))
    assert _exchange(world, _mailr(world, sub)).status_code == 201
    assert (ISSUER, sub) not in sat._refusals


def test_registration_closed_is_not_cached(world):
    world["env"].setenv("SSO_API_AUTO_REGISTER", "false")
    sub = _person(world, "closedcache")
    assert _exchange(world, _mailr(world, sub)).get_json()["code"] == "sso_registration_closed"
    world["env"].setenv("SSO_API_AUTO_REGISTER", "true")
    assert _exchange(world, _mailr(world, sub)).status_code == 201


def test_refusal_cache_is_capped(world, monkeypatch):
    monkeypatch.setattr(sat, "REFUSAL_CACHE_MAX", 3)
    for i in range(5):
        sat._remember_refusal((ISSUER, f"s{i}"), "sso_email_missing", "test")
    assert len(sat._refusals) == 3
    assert set(sat._refusals) == {(ISSUER, "s2"), (ISSUER, "s3"), (ISSUER, "s4")}
