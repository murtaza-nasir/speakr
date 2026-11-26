"""A pending SSO account link belongs to the signed-in user who asked for it
(GHSA-w7wf-8p3w-rmj7).

A link attempt that was cancelled at the provider, or abandoned, must not bind
the SSO identity of whoever signs in next in the same browser to the account
that started it.
"""
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from flask import redirect

from src.app import app, db
from src.models import User


def _user(subject=None):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"ssol_{s}", email=f"ssol_{s}@local.test", password="x",
             sso_provider="Test" if subject else None, sso_subject=subject)
    db.session.add(u)
    db.session.commit()
    return u.id


@contextmanager
def _world():
    """Two accounts: the victim (no SSO) and the attacker (with an SSO subject)."""
    prev = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        victim = _user()
        attacker_subject = f"attacker-{uuid.uuid4().hex[:8]}"
        attacker = _user()
    try:
        yield victim, attacker, attacker_subject
    finally:
        app.config["WTF_CSRF_ENABLED"] = prev
        with app.app_context():
            for uid in (victim, attacker):
                user = db.session.get(User, uid)
                if user:
                    db.session.delete(user)
            db.session.commit()


def _patches(subject, sign_in_as=None, fail=False):
    oauth = MagicMock()
    oauth.sso.authorize_redirect.side_effect = lambda **kw: redirect("https://idp.example/authorize")
    if fail:
        oauth.sso.authorize_access_token.side_effect = RuntimeError("access_denied")
    else:
        oauth.sso.authorize_access_token.return_value = {"access_token": "t"}
    return [
        patch("src.api.auth.is_sso_enabled", return_value=True),
        patch("src.api.auth.get_sso_client", return_value=oauth),
        patch("src.api.auth.get_sso_config",
              return_value={"redirect_uri": "http://localhost/cb", "provider_name": "Test"}),
        patch("src.api.auth.resolve_userinfo", return_value={"sub": subject}),
        patch("src.api.auth.create_or_update_sso_user",
              side_effect=lambda info: db.session.get(User, sign_in_as)),
    ]


@contextmanager
def _patched(*args, **kwargs):
    ps = _patches(*args, **kwargs)
    for p in ps:
        p.start()
    try:
        yield
    finally:
        for p in ps:
            p.stop()


def _sign_in_session(client, uid):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True


def _subject(uid):
    with app.app_context():
        return db.session.get(User, uid).sso_subject


def test_cancelled_link_is_cleared_and_not_completed_by_the_next_sign_in():
    with _world() as (victim, attacker, attacker_subject):
        client = app.test_client()
        _sign_in_session(client, victim)
        with _patched(attacker_subject, fail=True):
            client.post("/auth/sso/link")
            # Cancelled at the provider.
            client.get("/auth/sso/callback?error=access_denied")
        with client.session_transaction() as sess:
            assert "sso_link_user_id" not in sess
        client.post("/logout")
        with _patched(attacker_subject, sign_in_as=attacker):
            client.get("/auth/sso/login")
            client.get("/auth/sso/callback")
        assert _subject(victim) is None


def test_abandoned_link_does_not_survive_logout():
    with _world() as (victim, attacker, attacker_subject):
        client = app.test_client()
        _sign_in_session(client, victim)
        with _patched(attacker_subject):
            client.post("/auth/sso/link")  # tab closed: no callback
        with client.session_transaction() as sess:
            assert sess.get("sso_link_user_id") == victim
        client.post("/logout")
        with client.session_transaction() as sess:
            assert "sso_link_user_id" not in sess
            assert "sso_next" not in sess
            assert "sso_remember" not in sess
        with _patched(attacker_subject, sign_in_as=attacker):
            client.get("/auth/sso/login")
            client.get("/auth/sso/callback")
        assert _subject(victim) is None


def test_stale_link_without_a_signed_in_user_is_ignored():
    """Even if the value reaches the callback, no one is signed in to own it."""
    with _world() as (victim, attacker, attacker_subject):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["sso_link_user_id"] = victim
        with _patched(attacker_subject, sign_in_as=attacker):
            response = client.get("/auth/sso/callback")
        assert response.status_code == 302
        assert _subject(victim) is None
        with client.session_transaction() as sess:
            assert sess.get("_user_id") == str(attacker)


def test_stale_link_of_another_user_is_ignored():
    with _world() as (victim, attacker, attacker_subject):
        client = app.test_client()
        _sign_in_session(client, attacker)
        with client.session_transaction() as sess:
            sess["sso_link_user_id"] = victim
        with _patched(attacker_subject, sign_in_as=attacker):
            client.get("/auth/sso/callback")
        assert _subject(victim) is None


def test_link_by_the_signed_in_user_still_completes():
    with _world() as (victim, attacker, attacker_subject):
        client = app.test_client()
        _sign_in_session(client, victim)
        own_subject = f"victim-{uuid.uuid4().hex[:8]}"
        with _patched(own_subject):
            client.post("/auth/sso/link")
            response = client.get("/auth/sso/callback")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/account")
        assert _subject(victim) == own_subject
