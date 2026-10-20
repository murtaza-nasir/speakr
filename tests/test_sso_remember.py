"""An SSO sign-in sets the remember-me cookie only when "Remember me" was
ticked on the sign-in page, as a password sign-in does (#437)."""
import uuid
from unittest.mock import MagicMock, patch

from flask import redirect

from src.app import app, db
from src.models import User


def _user():
    s = uuid.uuid4().hex[:8]
    u = User(username=f"ssor_{s}", email=f"ssor_{s}@local.test", password="x",
             sso_provider="Test", sso_subject=f"sub-{s}")
    db.session.add(u)
    db.session.commit()
    return u


def _sign_in(query):
    """Start the SSO sign-in with the given query string and complete it."""
    client = app.test_client()
    oauth = MagicMock()
    oauth.sso.authorize_redirect.side_effect = lambda **kw: redirect("https://idp.example/authorize")
    oauth.sso.authorize_access_token.return_value = {"access_token": "t"}
    with app.app_context():
        user = _user()
        uid = user.id
    try:
        with patch("src.api.auth.is_sso_enabled", return_value=True), \
                patch("src.api.auth.get_sso_client", return_value=oauth), \
                patch("src.api.auth.get_sso_config", return_value={"redirect_uri": "http://localhost/cb"}), \
                patch("src.api.auth.resolve_userinfo", return_value={"sub": "x"}), \
                patch("src.api.auth.create_or_update_sso_user",
                      side_effect=lambda info: db.session.get(User, uid)):
            start = client.get("/auth/sso/login" + query)
            assert start.status_code == 302
            done = client.get("/auth/sso/callback")
            assert done.status_code == 302
            return done.headers.getlist("Set-Cookie")
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, uid))
            db.session.commit()


def _remember_cookie(cookies):
    name = app.config.get("REMEMBER_COOKIE_NAME", "remember_token")
    return [c for c in cookies if c.startswith(name + "=") and "Expires=Thu, 01 Jan 1970" not in c]


def test_sso_sign_in_does_not_remember_by_default():
    assert _remember_cookie(_sign_in("")) == []


def test_sso_sign_in_remembers_when_ticked():
    assert len(_remember_cookie(_sign_in("?remember=1"))) == 1


def test_sign_in_page_offers_remember_me_with_sso():
    with patch("src.api.auth.is_sso_enabled", return_value=True):
        html = app.test_client().get("/login").get_data(as_text=True)
    assert 'name="remember" value="1"' in html
