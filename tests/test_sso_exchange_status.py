"""SSO access tokens for people with no Speakr account, the SSO status
endpoint, and the display names of SSO clients.

Uses the fake provider of tests/test_sso_access_tokens.py (generated keys,
patched discovery and JWKS fetch). An account is created only by signing in
to Speakr with SSO. A valid token for an unknown subject is refused: the
exchange answers with sso_registration_closed, or with sso_link_required
when the email claim of the token belongs to an account; any other route
gives the 401 of an invalid token.

SHARED-DB: users, tokens and setting rows are removed or restored afterwards.
"""

import json
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_sso_access_tokens import (  # noqa: E402
    CLIENT, DISCOVERY, ISSUER, MAILR, Provider, _Client, _bearer,
)

from src.app import app, db  # noqa: E402
from src.auth import sso as sso_module  # noqa: E402
from src.auth import sso_access_tokens as sat  # noqa: E402
from src.models import APIToken, SystemSetting, User  # noqa: E402
from src.utils.token_auth import hash_token  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False

SETTING_KEYS = ("sso_api_tokens_enabled", "sso_api_allowed_clients", "sso_api_scopes",
                "sso_api_exchange_clients", "sso_api_client_names")


@pytest.fixture
def world(monkeypatch):
    p = Provider()
    sat.clear_cache()
    monkeypatch.setattr(sat, "_http_get_json", p.fetch)
    for key, value in {
        "ENABLE_SSO": "true", "SSO_DISCOVERY_URL": DISCOVERY, "SSO_PROVIDER_NAME": "TestID",
        "SSO_API_TOKENS_ENABLED": "true", "SSO_API_ALLOWED_CLIENTS": f"{CLIENT}, {MAILR}",
        "SSO_API_SCOPES": "read", "SSO_API_EXCHANGE_CLIENTS": MAILR,
        "SSO_AUTO_REGISTER": "true", "SSO_REQUIRE_VERIFIED_EMAIL": "true",
        "SSO_DEFAULT_NAME_CLAIM": "name", "SSO_API_CLIENT_NAMES": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SSO_ALLOWED_DOMAINS", raising=False)
    with app.app_context():
        saved = {r.key: (r.value, r.setting_type) for r in
                 SystemSetting.query.filter(SystemSetting.key.in_(SETTING_KEYS)).all()}
        for key in SETTING_KEYS:
            kind = "boolean" if key.endswith("enabled") else "string"
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


def _sub(world, label):
    return f"sub-{world['tag']}-{label}"


def _email(world, label):
    return f"{label}.{world['tag']}@example.test"


def _local_user(world, email, sso_subject=None):
    with app.app_context():
        n = f"local_{secrets.token_hex(4)}"
        u = User(username=n, email=email, password="x", sso_subject=sso_subject,
                 sso_provider="TestID" if sso_subject else None)
        db.session.add(u)
        db.session.commit()
        world["local"].append(u.id)
        return u.id


def _linked(world, label):
    """A Speakr account linked to an SSO subject. Returns the subject."""
    sub = _sub(world, label)
    _local_user(world, _email(world, label), sso_subject=sub)
    return sub


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


def _user_count():
    with app.app_context():
        return User.query.count()


# --- No Speakr account ----------------------------------------------------------------

@pytest.mark.parametrize("email", [None, "nobody", 5])
def test_unknown_subject_gets_registration_closed(world, email):
    sub = _sub(world, "closed")
    claims = {"email": _email(world, email) if email == "nobody" else email}
    before = _user_count()
    resp = _exchange(world, _mailr(world, sub, **claims))
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "sso_registration_closed"
    assert resp.get_json()["error"]
    plain = _me(world, world["provider"].token(sub, **claims))
    assert plain.status_code == 401 and plain.get_json() == _invalid_token_answer(world)
    assert _account(sub) is None and _user_count() == before


def test_rfc9068_token_for_an_unknown_subject_is_refused(world):
    sub = _sub(world, "pocket")
    tok = world["provider"].token(sub, typ="at+jwt", aud=[ISSUER], client_id=MAILR)
    resp = _exchange(world, tok)
    assert resp.status_code == 403 and resp.get_json()["code"] == "sso_registration_closed"
    assert _account(sub) is None


def test_unknown_subject_with_a_client_that_may_not_exchange(world):
    sub = _sub(world, "owui")
    resp = _exchange(world, world["provider"].token(sub))   # Open WebUI's client
    assert resp.status_code == 403 and resp.get_json()["code"] == "sso_exchange_not_allowed"
    assert _account(sub) is None


def test_feature_off_gives_401_on_the_exchange(world):
    world["env"].setenv("SSO_API_TOKENS_ENABLED", "false")
    sub = _sub(world, "off")
    assert _exchange(world, _mailr(world, sub)).status_code == 401
    assert _account(sub) is None


def test_email_of_an_unlinked_account_gives_link_required(world):
    sub = _sub(world, "local")
    email = _email(world, "local")
    uid = _local_user(world, email.upper())
    resp = _exchange(world, _mailr(world, sub, email=email))
    assert resp.status_code == 409 and resp.get_json()["code"] == "sso_link_required"
    assert resp.get_json()["error"]
    assert _me(world, world["provider"].token(sub, email=email)).status_code == 401
    with app.app_context():
        local = db.session.get(User, uid)
        assert local.sso_subject is None and local.sso_provider is None
        assert local.email == email.upper() and local.password == "x"
    assert _account(sub) is None


def test_email_of_an_account_linked_to_another_subject_gives_link_required(world):
    sub = _sub(world, "other")
    email = _email(world, "other")
    uid = _local_user(world, email, sso_subject=_sub(world, "someone-else"))
    resp = _exchange(world, _mailr(world, sub, email=email))
    assert resp.status_code == 409 and resp.get_json()["code"] == "sso_link_required"
    with app.app_context():
        assert db.session.get(User, uid).sso_subject == _sub(world, "someone-else")
    assert _account(sub) is None


def test_after_interactive_sign_in_the_exchange_works(world):
    sub = _sub(world, "later")
    email = _email(world, "later")
    uid = _local_user(world, email)
    assert _exchange(world, _mailr(world, sub, email=email)).status_code == 409
    with app.app_context():
        sso_module.create_or_update_sso_user({"sub": sub, "email": email, "email_verified": True})
    resp = _exchange(world, _mailr(world, sub, email=email))
    assert resp.status_code == 201
    assert _account(sub).id == uid
    assert _me(world, resp.get_json()["token"]).status_code == 200


@pytest.mark.parametrize("path", ["/", "/account", "/api/recordings", "/api/v1/users/me"])
def test_no_account_is_created_on_any_route(world, path):
    sub = _sub(world, "outside")
    before = _user_count()
    world["client"].get(path, headers=_bearer(world["provider"].token(sub, email=_email(world, "x"))))
    assert _account(sub) is None and _user_count() == before


def test_validate_without_a_request_gives_none(world):
    sub = _sub(world, "norequest")
    with app.app_context():
        assert sat.validate(world["provider"].token(sub)) is None
    assert _account(sub) is None


# --- Capabilities and /sso/status ------------------------------------------------------

def test_capabilities_has_no_provisioning_flag(world):
    sub = _linked(world, "caps")
    body = world["client"].get("/api/v1/capabilities", headers=_bearer(_mailr(world, sub))).get_json()
    assert body["features"]["sso_token_exchange"] is True
    assert "sso_exchange_provisioning" not in body["features"]


def _status(world, query="", **kwargs):
    return world["client"].get(f"/api/v1/sso/status{query}", **kwargs)


def test_status_without_auth(world):
    resp = _status(world)
    assert resp.status_code == 200
    assert resp.get_json() == {"sso_token_exchange": True, "exchange_client_allowed": None}
    text = resp.get_data(as_text=True)
    for secret in (MAILR, CLIENT, ISSUER, "speakr_version", "version"):
        assert secret not in text


@pytest.mark.parametrize("client_id, allowed", [
    (MAILR, True), (CLIENT, False), ("unknown", False), ("", False),
])
def test_status_with_client_id(world, client_id, allowed):
    body = _status(world, f"?client_id={client_id}").get_json()
    assert body["exchange_client_allowed"] is allowed
    assert set(body) == {"sso_token_exchange", "exchange_client_allowed"}


def test_status_exchange_listed_but_not_allowed_is_false(world):
    world["env"].setenv("SSO_API_EXCHANGE_CLIENTS", f"{MAILR}, only-exchange")
    assert _status(world, "?client_id=only-exchange").get_json()["exchange_client_allowed"] is False


@pytest.mark.parametrize("env, value, expected", [
    ("SSO_API_TOKENS_ENABLED", "false", False),
    ("ENABLE_SSO", "false", False),
    ("SSO_API_EXCHANGE_CLIENTS", "", False),
    ("SSO_AUTO_REGISTER", "false", True),
])
def test_status_follows_the_switches(world, env, value, expected):
    world["env"].setenv(env, value)
    body = _status(world, f"?client_id={MAILR}").get_json()
    assert body["sso_token_exchange"] is expected
    assert body["exchange_client_allowed"] is expected


def test_status_ignores_a_token(world):
    sub = _sub(world, "status")
    for headers in (_bearer(_mailr(world, sub)), _bearer("garbage-token")):
        resp = _status(world, headers=headers)
        assert resp.status_code == 200
    assert _account(sub) is None


def test_status_is_in_the_openapi_document_without_security(world):
    spec = world["client"].get("/api/v1/openapi.json").get_json()
    op = spec["paths"]["/sso/status"]["get"]
    assert op["security"] == [] and op["operationId"] == "get_sso_status"
    props = op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert set(props) == {"sso_token_exchange", "exchange_client_allowed"}


def test_status_has_a_per_ip_rate_limit():
    from src.api.api_v1 import get_sso_status
    assert getattr(get_sso_status, "_rate_limit", None) == "30 per minute"
    with app.test_request_context("/api/v1/sso/status"):
        from src.app import exempt_status_endpoints
        assert exempt_status_endpoints() is False


# --- Client display names -----------------------------------------------------------------

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
    sub = _linked(world, "named")
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
    sub = _linked(world, "listed")
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


# --- /tokens/current names the exchanging client ----------------------------------------

def test_current_token_of_an_exchanged_token_names_its_client(world):
    sub = _linked(world, "current")
    world["env"].setenv("SSO_API_CLIENT_NAMES", f"{MAILR}=Mail Sync")
    plain = _exchange(world, _mailr(world, sub)).get_json()["token"]
    body = world["client"].get("/api/v1/tokens/current", headers=_bearer(plain)).get_json()
    assert body["id"] is not None
    assert body["sso_client_id"] == MAILR and body["sso_client_name"] == "Mail Sync"
    world["env"].setenv("SSO_API_CLIENT_NAMES", "")
    body = world["client"].get("/api/v1/tokens/current", headers=_bearer(plain)).get_json()
    assert body["sso_client_id"] == MAILR and body["sso_client_name"] is None


def test_current_token_of_a_manual_token_has_no_client(world):
    uid = _local_user(world, _email(world, "manual"))
    plain = f"tok-{secrets.token_urlsafe(16)}"
    with app.app_context():
        db.session.add(APIToken(user_id=uid, token_hash=hash_token(plain), name="manual",
                                scopes=json.dumps(["read"])))
        db.session.commit()
    body = world["client"].get("/api/v1/tokens/current", headers=_bearer(plain)).get_json()
    assert body["name"] == "manual"
    assert body["sso_client_id"] is None and body["sso_client_name"] is None


def test_current_token_of_an_sso_access_token_carries_the_name(world):
    sub = _linked(world, "currentsso")
    world["env"].setenv("SSO_API_CLIENT_NAMES", f"{CLIENT}=Open WebUI")
    body = world["client"].get("/api/v1/tokens/current",
                               headers=_bearer(world["provider"].token(sub))).get_json()
    assert body["id"] is None
    assert body["sso_client_id"] == CLIENT and body["sso_client_name"] == "Open WebUI"
