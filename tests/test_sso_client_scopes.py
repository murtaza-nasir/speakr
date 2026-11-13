"""Scope limits per SSO client (sso_api_client_scopes).

A listed client gets its scopes intersected with sso_api_scopes; a client
not listed keeps sso_api_scopes. The fake provider and the users come from
tests/test_sso_access_tokens.py.

SHARED-DB: the setting row is restored afterwards; users and tokens are
removed by the imported world fixture.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_sso_access_tokens import (  # noqa: E402,F401  (fixtures)
    CLIENT, ISSUER, MAILR, _alice, _bearer, _exchange, _invalid_token_answer, _mailr,
    provider, settings, world,
)

from src.app import app, db  # noqa: E402
from src.auth import sso_access_tokens as sat  # noqa: E402
from src.models import APIToken, SystemSetting  # noqa: E402
from src.utils.token_auth import hash_token  # noqa: E402

KEY = "sso_api_client_scopes"


@pytest.fixture
def scoped(world):
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        saved = (row.value, row.setting_type) if row is not None else None
        SystemSetting.set_setting(KEY, None, None, "string")
    world["settings"].setenv("SSO_API_SCOPES", "read,write,upload,share,webhooks")
    world["settings"].setenv("SSO_API_EXCHANGE_CLIENTS", f"{MAILR}, {CLIENT}")
    world["settings"].setenv("SSO_API_CLIENT_SCOPES", f"{CLIENT}=read")
    yield world
    with app.app_context():
        if saved is not None:
            SystemSetting.set_setting(KEY, saved[0], None, saved[1])
        else:
            row = SystemSetting.query.filter_by(key=KEY).first()
            if row is not None:
                db.session.delete(row)
                db.session.commit()


def _rec_patch(world, token):
    return world["client"].patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Changed"},
                                 headers=_bearer(token))


def test_listed_client_is_limited_per_request(scoped):
    _, sub, _ = _alice(scoped)
    owui = scoped["provider"].token(sub)          # aud = CLIENT, listed as read only
    assert scoped["client"].get("/api/v1/users/me", headers=_bearer(owui)).status_code == 200
    resp = _rec_patch(scoped, owui)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "insufficient_scope" and body["token_scopes"] == ["read"]
    current = scoped["client"].get("/api/v1/tokens/current", headers=_bearer(owui)).get_json()
    assert current["scopes"] == ["read"]


def test_unlisted_client_keeps_the_global_scopes(scoped):
    _, sub, _ = _alice(scoped)
    mailr = _mailr(scoped, sub)
    current = scoped["client"].get("/api/v1/tokens/current", headers=_bearer(mailr)).get_json()
    assert current["scopes"] == ["read", "share", "upload", "webhooks", "write"]
    assert _rec_patch(scoped, mailr).status_code == 200


def test_exchange_is_limited_to_the_client_ceiling(scoped):
    uid, sub, _ = _alice(scoped)
    owui = scoped["provider"].token(sub)
    refused = _exchange(scoped, owui, name="too much", scopes=["read", "write"])
    assert refused.status_code == 400
    assert refused.get_json()["allowed_scopes"] == ["read"]
    body = _exchange(scoped, owui, name="default").get_json()
    assert body["scopes"] == ["read"]
    assert _exchange(scoped, owui, name="exact", scopes=["read"]).status_code == 201
    with app.app_context():
        names = {t.name for t in APIToken.query.filter_by(user_id=uid).all()}
        assert names == {"default", "exact"}


def test_ceiling_is_intersected_with_the_global_setting(scoped):
    _, sub, _ = _alice(scoped)
    scoped["settings"].setenv("SSO_API_SCOPES", "read,upload")
    scoped["settings"].setenv("SSO_API_CLIENT_SCOPES", f"{MAILR}=read write upload")
    body = _exchange(scoped, _mailr(scoped, sub), name="sync").get_json()
    assert body["scopes"] == ["read", "upload"]
    refused = _exchange(scoped, _mailr(scoped, sub), name="w", scopes=["write"])
    assert refused.status_code == 400 and refused.get_json()["allowed_scopes"] == ["read", "upload"]


@pytest.mark.parametrize("raw", [
    f"{MAILR}=read+write",
    f"{MAILR}=read|write",
    f" {MAILR} = read  write ",
    f"garbage, =read, {MAILR}=read write\nalso garbage",
    f"{MAILR}=read write full bogus",
])
def test_separators_and_lenient_parsing(scoped, raw):
    _, sub, _ = _alice(scoped)
    scoped["settings"].setenv("SSO_API_CLIENT_SCOPES", raw)
    body = _exchange(scoped, _mailr(scoped, sub), name="sync").get_json()
    assert body["scopes"] == ["read", "write"]


def test_client_scopes_parse(monkeypatch):
    monkeypatch.setenv("SSO_API_CLIENT_SCOPES", "a=read write, b=read+upload|share\nc=full, =read, d, e=")
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        saved = (row.value, row.setting_type) if row is not None else None
        SystemSetting.set_setting(KEY, None, None, "string")
        try:
            assert sat.client_scopes() == {
                "a": frozenset({"read", "write"}),
                "b": frozenset({"read", "upload", "share"}),
                "c": frozenset(),
                "e": frozenset(),
            }
        finally:
            if saved is not None:
                SystemSetting.set_setting(KEY, saved[0], None, saved[1])


@pytest.mark.parametrize("value", ["", "bogus", "full", "delete"])
def test_listed_client_with_no_scope_left_is_refused(scoped, value):
    # "delete" is a known scope, but not in the global sso_api_scopes here.
    uid, sub, _ = _alice(scoped)
    scoped["settings"].setenv("SSO_API_CLIENT_SCOPES", f"{CLIENT}={value}, {MAILR}=read")
    owui = scoped["provider"].token(sub)
    resp = scoped["client"].get("/api/v1/users/me", headers=_bearer(owui))
    assert resp.status_code == 401 and resp.get_json() == _invalid_token_answer(scoped)
    assert _exchange(scoped, owui, name="x").status_code == 401
    # Another client is unaffected.
    assert scoped["client"].get("/api/v1/users/me", headers=_bearer(_mailr(scoped, sub))).status_code == 200


def test_exchange_for_a_second_client_with_no_scope_is_400(scoped):
    # The token names two clients: it is checked as MAILR (allowed scopes),
    # and exchanged for CLIENT, whose ceiling is empty.
    uid, sub, _ = _alice(scoped)
    scoped["settings"].setenv("SSO_API_EXCHANGE_CLIENTS", CLIENT)
    scoped["settings"].setenv("SSO_API_CLIENT_SCOPES", f"{CLIENT}=bogus")
    tok = scoped["provider"].token(sub, aud=[CLIENT, MAILR], azp=MAILR)
    resp = _exchange(scoped, tok, name="x")
    assert resp.status_code == 400
    assert resp.get_json()["allowed_scopes"] == [] and resp.get_json()["code"] == "invalid_parameter"
    with app.app_context():
        assert APIToken.query.filter_by(user_id=uid).count() == 0


def test_exchanged_token_keeps_its_scopes_as_stored(scoped):
    _, sub, _ = _alice(scoped)
    plain = _exchange(scoped, _mailr(scoped, sub), name="sync", scopes=["read", "write"]).get_json()["token"]
    with app.app_context():
        row = APIToken.query.filter_by(token_hash=hash_token(plain)).one()
        assert json.loads(row.scopes) == ["read", "write"]
