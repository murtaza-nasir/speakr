#!/usr/bin/env python3
"""Tests for src/auth/sso.py — SSO/OIDC sign-in and auto-provisioning.

This module has zero prior coverage and is high-risk: a bug here can mean
account takeover (linking an SSO identity to the wrong local user) or
unauthorized auto-provisioning (creating accounts for disallowed domains).

Functions covered:
  - _str_to_bool
  - get_sso_config / is_sso_enabled  (env-driven, read live per call)
  - is_domain_allowed
  - _sanitize_username
  - generate_unique_username  (DB-backed: collision suffixing)
  - create_or_update_sso_user (DB-backed: create / link / dedup / authz)

Run (pytest, isolated temp DB from conftest.py):
    HOME=/tmp /tmp/speakr_testvenv/bin/python -m pytest tests/test_sso.py -q
"""

import os
import sys
import uuid
import contextlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import User  # noqa: E402
from src.auth import sso  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Env vars get_sso_config() reads. We snapshot/restore these around each test
# so the suite stays isolated and tests can set them freely.
_SSO_ENV_KEYS = [
    "ENABLE_SSO", "SSO_PROVIDER_NAME", "SSO_CLIENT_ID", "SSO_CLIENT_SECRET",
    "SSO_DISCOVERY_URL", "SSO_REDIRECT_URI", "SSO_AUTO_REGISTER",
    "SSO_ALLOWED_DOMAINS", "SSO_DEFAULT_USERNAME_CLAIM",
    "SSO_DEFAULT_NAME_CLAIM", "SSO_DISABLE_PASSWORD_LOGIN",
    "SSO_REQUIRE_VERIFIED_EMAIL",
]


@contextlib.contextmanager
def env(**overrides):
    """Temporarily set/unset SSO env vars; restore exactly on exit.

    Pass a value to set it; pass None to ensure the var is unset.
    """
    saved = {k: os.environ.get(k) for k in _SSO_ENV_KEYS}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


@pytest.fixture
def app_ctx():
    with app.app_context():
        yield


@pytest.fixture
def cleanup_users(app_ctx):
    """Track usernames/emails/subjects created in a test and delete them after."""
    created = {"usernames": set(), "emails": set(), "subjects": set()}
    yield created
    q = User.query.filter(
        db.or_(
            User.username.in_(created["usernames"] or [""]),
            User.email.in_(created["emails"] or [""]),
            User.sso_subject.in_(created["subjects"] or [""]),
        )
    )
    for u in q.all():
        db.session.delete(u)
    db.session.commit()


def _mk_user(cleanup, username=None, email=None, sso_subject=None, password="x"):
    username = username or ("u_" + uuid.uuid4().hex[:14])
    email = email or (uuid.uuid4().hex[:10] + "@example.com")
    u = User(username=username, email=email, password=password, sso_subject=sso_subject)
    db.session.add(u)
    db.session.commit()
    cleanup["usernames"].add(username)
    cleanup["emails"].add(email)
    if sso_subject:
        cleanup["subjects"].add(sso_subject)
    return u


# ---------------------------------------------------------------------------
# _str_to_bool
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("0", False), ("1", False), ("yes", False),
    ("", False), (None, False),
])
def test_str_to_bool(value, expected):
    assert sso._str_to_bool(value) is expected


# ---------------------------------------------------------------------------
# get_sso_config / is_sso_enabled — read env live per call
# ---------------------------------------------------------------------------

def test_get_sso_config_reads_env_live_per_call():
    with env(SSO_PROVIDER_NAME="Okta", ENABLE_SSO="true"):
        cfg1 = sso.get_sso_config()
        assert cfg1["provider_name"] == "Okta"
        assert cfg1["enabled"] is True
        # mutate env after first read; a fresh call must reflect it (no caching)
        os.environ["SSO_PROVIDER_NAME"] = "Keycloak"
        os.environ["ENABLE_SSO"] = "false"
        cfg2 = sso.get_sso_config()
        assert cfg2["provider_name"] == "Keycloak"
        assert cfg2["enabled"] is False


def test_get_sso_config_defaults():
    with env(SSO_PROVIDER_NAME=None, SSO_DEFAULT_USERNAME_CLAIM=None,
             SSO_DEFAULT_NAME_CLAIM=None, ENABLE_SSO=None, SSO_AUTO_REGISTER=None):
        cfg = sso.get_sso_config()
        assert cfg["provider_name"] == "SSO"
        assert cfg["username_claim"] == "preferred_username"
        assert cfg["name_claim"] == "name"
        assert cfg["enabled"] is False
        # SSO_AUTO_REGISTER defaults to "true"
        assert cfg["auto_register"] is True


def test_is_sso_enabled_requires_all_fields():
    full = dict(
        ENABLE_SSO="true", SSO_CLIENT_ID="cid", SSO_CLIENT_SECRET="secret",
        SSO_DISCOVERY_URL="https://idp/.well-known/openid-configuration",
        SSO_REDIRECT_URI="https://app/callback",
    )
    with env(**full):
        assert sso.is_sso_enabled() is True

    # Disabled flag → False even when everything else present
    with env(**{**full, "ENABLE_SSO": "false"}):
        assert sso.is_sso_enabled() is False

    # Missing client secret → False
    with env(**{**full, "SSO_CLIENT_SECRET": None}):
        assert sso.is_sso_enabled() is False

    # Missing discovery url → False
    with env(**{**full, "SSO_DISCOVERY_URL": None}):
        assert sso.is_sso_enabled() is False


# ---------------------------------------------------------------------------
# is_domain_allowed
# ---------------------------------------------------------------------------

def test_is_domain_allowed_none_and_malformed_email():
    with env(SSO_ALLOWED_DOMAINS="example.com"):
        assert sso.is_domain_allowed(None) is False
        assert sso.is_domain_allowed("") is False
        # no "@" → not allowed
        assert sso.is_domain_allowed("not-an-email") is False


def test_is_domain_allowed_empty_allowlist_allows_all():
    # Unset → no restriction
    with env(SSO_ALLOWED_DOMAINS=None):
        assert sso.is_domain_allowed("anyone@anywhere.com") is True
    # Empty string → no restriction
    with env(SSO_ALLOWED_DOMAINS=""):
        assert sso.is_domain_allowed("anyone@anywhere.com") is True
    # Whitespace/commas only → parses to empty allowed list → allow all
    with env(SSO_ALLOWED_DOMAINS="  , , "):
        assert sso.is_domain_allowed("anyone@anywhere.com") is True


def test_is_domain_allowed_matches_listed_domain_case_insensitively():
    with env(SSO_ALLOWED_DOMAINS="Example.com, Corp.io"):
        assert sso.is_domain_allowed("alice@example.com") is True
        assert sso.is_domain_allowed("BOB@EXAMPLE.COM") is True
        assert sso.is_domain_allowed("carol@corp.io") is True


def test_is_domain_allowed_rejects_unlisted_domain():
    with env(SSO_ALLOWED_DOMAINS="example.com"):
        assert sso.is_domain_allowed("mallory@evil.com") is False
        # the listed domain appearing only in the local part must NOT pass
        assert sso.is_domain_allowed("example.com@evil.com") is False


def test_is_domain_allowed_subdomain_is_not_implied():
    # The code does an exact membership test on the full domain after the last
    # "@", so a subdomain of an allowed domain is NOT automatically allowed.
    with env(SSO_ALLOWED_DOMAINS="example.com"):
        assert sso.is_domain_allowed("alice@eng.example.com") is False
    # But listing the subdomain explicitly works.
    with env(SSO_ALLOWED_DOMAINS="eng.example.com"):
        assert sso.is_domain_allowed("alice@eng.example.com") is True


def test_is_domain_allowed_uses_last_at_for_domain():
    # rsplit("@", 1) → an address with two "@" is rejected (len(parts)!=2 only
    # when there's no "@"; with two "@", rsplit limits to 1 split so the domain
    # is what's after the LAST "@"). Verify that real behavior.
    with env(SSO_ALLOWED_DOMAINS="example.com"):
        # "a@b@example.com" → domain "example.com" → allowed
        assert sso.is_domain_allowed("a@b@example.com") is True
        # "a@b@evil.com" → domain "evil.com" → rejected
        assert sso.is_domain_allowed("a@b@evil.com") is False


# ---------------------------------------------------------------------------
# _sanitize_username
# ---------------------------------------------------------------------------

def test_sanitize_username_keeps_allowed_chars():
    assert sso._sanitize_username("john.doe_1-2") == "john.doe_1-2"


def test_sanitize_username_strips_disallowed_chars():
    assert sso._sanitize_username("john@doe.com") == "johndoe.com"
    assert sso._sanitize_username("a b c!#$%") == "abc"
    # unicode / spaces stripped
    assert sso._sanitize_username("Renée Müller") == "ReneMller"


def test_sanitize_username_empty_falls_back_to_user():
    assert sso._sanitize_username("") == "user"
    assert sso._sanitize_username(None) == "user"
    # all-disallowed → stripped to empty → "user"
    assert sso._sanitize_username("!!!@@@###") == "user"


# ---------------------------------------------------------------------------
# generate_unique_username  (DB-backed)
# ---------------------------------------------------------------------------

def test_generate_unique_username_no_collision_returns_sanitized_base(cleanup_users):
    base = "uniq" + uuid.uuid4().hex[:8]
    got = sso.generate_unique_username(base + "@x.com")
    assert got == (base + "x.com")[:20]


def test_generate_unique_username_none_falls_back_to_user(cleanup_users):
    # If "user" itself is free this returns "user"; if taken it suffixes. Either
    # way it must start with the sanitized "user" base.
    got = sso.generate_unique_username(None)
    assert got == "user" or got.startswith("user")


def test_generate_unique_username_appends_suffix_on_collision(cleanup_users):
    base = "coll" + uuid.uuid4().hex[:6]   # < 18 chars, no truncation games
    # Occupy the base username.
    _mk_user(cleanup_users, username=base)
    got = sso.generate_unique_username(base)
    assert got != base
    assert got == f"{base}01"
    # Register so the next call must skip to 02.
    _mk_user(cleanup_users, username=got)
    got2 = sso.generate_unique_username(base)
    assert got2 == f"{base}02"


def test_generate_unique_username_result_is_actually_unique(cleanup_users):
    base = "x" + uuid.uuid4().hex[:6]
    _mk_user(cleanup_users, username=base)
    got = sso.generate_unique_username(base)
    # Must not collide with an existing row.
    assert User.query.filter_by(username=got).first() is None


# ---------------------------------------------------------------------------
# create_or_update_sso_user  (DB-backed) — the high-risk surface
# ---------------------------------------------------------------------------

def _track(cleanup, user):
    cleanup["usernames"].add(user.username)
    cleanup["emails"].add(user.email)
    if user.sso_subject:
        cleanup["subjects"].add(user.sso_subject)


def test_create_requires_sub(cleanup_users):
    with env(SSO_ALLOWED_DOMAINS=None):
        with pytest.raises(ValueError):
            sso.create_or_update_sso_user({"email": "noone@example.com"})


def test_create_new_user_from_claims(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    email = "new" + uuid.uuid4().hex[:8] + "@example.com"
    preferred = "pref" + uuid.uuid4().hex[:6]
    with env(SSO_ALLOWED_DOMAINS=None, SSO_PROVIDER_NAME="Keycloak",
             SSO_DEFAULT_USERNAME_CLAIM="preferred_username",
             SSO_DEFAULT_NAME_CLAIM="name", SSO_AUTO_REGISTER="true"):
        user = sso.create_or_update_sso_user({
            "sub": sub, "email": email,
            "preferred_username": preferred, "name": "Alice Example",
        })
        _track(cleanup_users, user)
        assert user.id is not None
        assert user.sso_subject == sub
        assert user.sso_provider == "Keycloak"
        assert user.email == email
        assert user.name == "Alice Example"
        assert user.username == preferred[:20]
        # SSO users have no local password.
        assert user.password is None


def test_second_call_same_subject_updates_not_duplicates(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    email = "dup" + uuid.uuid4().hex[:8] + "@example.com"
    with env(SSO_ALLOWED_DOMAINS=None, SSO_DEFAULT_NAME_CLAIM="name"):
        u1 = sso.create_or_update_sso_user({"sub": sub, "email": email, "name": "First"})
        _track(cleanup_users, u1)
        first_id = u1.id
        # Second call, same subject, changed name → updates the same row.
        u2 = sso.create_or_update_sso_user({"sub": sub, "email": email, "name": "Second"})
        _track(cleanup_users, u2)
        assert u2.id == first_id
        assert u2.name == "Second"
        assert User.query.filter_by(sso_subject=sub).count() == 1


def test_existing_email_user_gets_sso_linked(cleanup_users):
    # A pre-existing password user with this email should be LINKED to the SSO
    # identity rather than having a duplicate account created.
    email = "link" + uuid.uuid4().hex[:8] + "@example.com"
    existing = _mk_user(cleanup_users, email=email, password="hashed")
    existing_id = existing.id
    sub = "sub-" + uuid.uuid4().hex
    cleanup_users["subjects"].add(sub)
    with env(SSO_ALLOWED_DOMAINS=None, SSO_PROVIDER_NAME="Keycloak"):
        linked = sso.create_or_update_sso_user({"sub": sub, "email": email, "name": "Linked"})
        assert linked.id == existing_id
        assert linked.sso_subject == sub
        assert linked.sso_provider == "Keycloak"
        assert User.query.filter_by(email=email).count() == 1


def test_disallowed_domain_is_rejected(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    with env(SSO_ALLOWED_DOMAINS="example.com", SSO_AUTO_REGISTER="true"):
        with pytest.raises(PermissionError):
            sso.create_or_update_sso_user({
                "sub": sub, "email": "attacker@evil.com", "name": "Mallory",
            })
    # No account should have been provisioned.
    assert User.query.filter_by(sso_subject=sub).first() is None


def test_auto_register_disabled_blocks_new_users(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    with env(SSO_ALLOWED_DOMAINS=None, SSO_AUTO_REGISTER="false"):
        with pytest.raises(PermissionError):
            sso.create_or_update_sso_user({
                "sub": sub, "email": "newbie@example.com", "name": "Newbie",
            })
    assert User.query.filter_by(sso_subject=sub).first() is None


def test_auto_register_disabled_still_allows_known_subject(cleanup_users):
    # Existing SSO user (known subject) must still be able to log in even when
    # auto-registration is turned off.
    sub = "sub-" + uuid.uuid4().hex
    email = "known" + uuid.uuid4().hex[:8] + "@example.com"
    existing = _mk_user(cleanup_users, email=email, sso_subject=sub, password=None)
    with env(SSO_ALLOWED_DOMAINS=None, SSO_AUTO_REGISTER="false", SSO_DEFAULT_NAME_CLAIM="name"):
        user = sso.create_or_update_sso_user({"sub": sub, "email": email, "name": "Known"})
        assert user.id == existing.id
        assert user.name == "Known"


def test_username_claim_is_respected(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    upn = "upn" + uuid.uuid4().hex[:6]
    with env(SSO_ALLOWED_DOMAINS=None, SSO_DEFAULT_USERNAME_CLAIM="upn"):
        user = sso.create_or_update_sso_user({
            "sub": sub, "email": "claimuser@example.com",
            "upn": upn, "preferred_username": "should-not-be-used",
        })
        _track(cleanup_users, user)
        assert user.username == upn[:20]


def test_username_falls_back_to_email_localpart(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    local = "local" + uuid.uuid4().hex[:6]
    with env(SSO_ALLOWED_DOMAINS=None):
        # No username claim provided → use email local part.
        user = sso.create_or_update_sso_user({"sub": sub, "email": f"{local}@example.com"})
        _track(cleanup_users, user)
        assert user.username == local[:20]


def test_no_email_uses_placeholder(cleanup_users):
    sub = "sub-" + uuid.uuid4().hex
    with env(SSO_ALLOWED_DOMAINS="example.com"):
        # No email claim at all → domain check is skipped, placeholder email set.
        user = sso.create_or_update_sso_user({"sub": sub, "preferred_username": "noemail" + sub[:6]})
        _track(cleanup_users, user)
        assert user.email == f"{sub}@placeholder.local"


# ---------------------------------------------------------------------------
# SSO_REQUIRE_VERIFIED_EMAIL (opt-in; default off = backwards compatible)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False),
    ("true", True), ("True", True), ("1", True), ("yes", True),
    ("false", False), ("no", False), ("", False), (None, False), (0, False),
])
def test_claim_is_truthy(value, expected):
    assert sso._claim_is_truthy(value) is expected


def test_require_verified_email_defaults_off_and_links_unverified(cleanup_users):
    """Default (flag unset): an unverified/absent email_verified still links to
    an existing-by-email account — the original behaviour, preserved."""
    with env(SSO_ALLOWED_DOMAINS=None, SSO_REQUIRE_VERIFIED_EMAIL=None):
        existing = _mk_user(cleanup_users, email="legacy@example.com")
        assert existing.sso_subject is None
        sub = "sub-" + uuid.uuid4().hex[:10]
        user = sso.create_or_update_sso_user({"sub": sub, "email": "legacy@example.com"})
        _track(cleanup_users, user)
        # Linked to the existing account despite no email_verified claim.
        assert user.id == existing.id
        assert user.sso_subject == sub


def test_require_verified_email_on_rejects_unverified_link(cleanup_users):
    """Flag on: linking to an existing account by an UNVERIFIED email is refused
    (the account-takeover vector), and the existing account is left untouched."""
    with env(SSO_ALLOWED_DOMAINS=None, SSO_REQUIRE_VERIFIED_EMAIL="true"):
        existing = _mk_user(cleanup_users, email="victim@example.com")
        sub = "sub-" + uuid.uuid4().hex[:10]
        for claims in (
            {"sub": sub, "email": "victim@example.com"},                       # absent
            {"sub": sub, "email": "victim@example.com", "email_verified": False},
            {"sub": sub, "email": "victim@example.com", "email_verified": "false"},
        ):
            with pytest.raises(PermissionError):
                sso.create_or_update_sso_user(claims)
        db.session.expire_all()
        # The existing account was NOT hijacked.
        refreshed = db.session.get(User, existing.id)
        assert refreshed.sso_subject is None


def test_require_verified_email_on_allows_verified_link(cleanup_users):
    """Flag on + email_verified true (bool or string): linking proceeds."""
    with env(SSO_ALLOWED_DOMAINS=None, SSO_REQUIRE_VERIFIED_EMAIL="true"):
        existing = _mk_user(cleanup_users, email="ok@example.com")
        sub = "sub-" + uuid.uuid4().hex[:10]
        user = sso.create_or_update_sso_user(
            {"sub": sub, "email": "ok@example.com", "email_verified": True}
        )
        _track(cleanup_users, user)
        assert user.id == existing.id
        assert user.sso_subject == sub


def test_require_verified_email_on_rejects_new_user_with_unverified_email(cleanup_users):
    """Flag on: provisioning a NEW account with an unverified email is refused
    too (no account is created)."""
    with env(SSO_ALLOWED_DOMAINS=None, SSO_REQUIRE_VERIFIED_EMAIL="true"):
        sub = "sub-" + uuid.uuid4().hex[:10]
        email = uuid.uuid4().hex[:10] + "@example.com"
        with pytest.raises(PermissionError):
            sso.create_or_update_sso_user({"sub": sub, "email": email, "email_verified": False})
        assert User.query.filter_by(sso_subject=sub).first() is None


def test_require_verified_email_on_does_not_affect_already_linked_subject(cleanup_users):
    """Flag on: a user already linked by subject keeps logging in even if the
    IdP omits email_verified (no email trust is involved on that path)."""
    with env(SSO_ALLOWED_DOMAINS=None, SSO_REQUIRE_VERIFIED_EMAIL="true"):
        sub = "sub-" + uuid.uuid4().hex[:10]
        linked = _mk_user(cleanup_users, email="linked@example.com", sso_subject=sub)
        user = sso.create_or_update_sso_user({"sub": sub, "email": "linked@example.com"})
        _track(cleanup_users, user)
        assert user.id == linked.id


def test_require_verified_email_on_allows_no_email_claim(cleanup_users):
    """Flag on: a login with no email claim is unaffected (nothing to verify);
    a placeholder email is assigned as before."""
    with env(SSO_ALLOWED_DOMAINS=None, SSO_REQUIRE_VERIFIED_EMAIL="true"):
        sub = "sub-" + uuid.uuid4().hex[:10]
        user = sso.create_or_update_sso_user({"sub": sub, "preferred_username": "noemail" + sub[:6]})
        _track(cleanup_users, user)
        assert user.email == f"{sub}@placeholder.local"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
