import os
import re
from typing import Dict, Optional

from authlib.integrations.flask_client import OAuth
from flask import current_app

from src.database import db
from src.models import User

# Keep a single OAuth client instance
_oauth: Optional[OAuth] = None


def _str_to_bool(value: str) -> bool:
    return str(value or "").lower() == "true"


def get_sso_config() -> Dict[str, Optional[str]]:
    """Load SSO configuration from environment variables."""
    return {
        "enabled": _str_to_bool(os.environ.get("ENABLE_SSO", "false")),
        "provider_name": os.environ.get("SSO_PROVIDER_NAME", "SSO"),
        "client_id": os.environ.get("SSO_CLIENT_ID"),
        "client_secret": os.environ.get("SSO_CLIENT_SECRET"),
        "discovery_url": os.environ.get("SSO_DISCOVERY_URL"),
        "redirect_uri": os.environ.get("SSO_REDIRECT_URI"),
        "auto_register": _str_to_bool(os.environ.get("SSO_AUTO_REGISTER", "true")),
        "allowed_domains": os.environ.get("SSO_ALLOWED_DOMAINS"),
        "username_claim": os.environ.get("SSO_DEFAULT_USERNAME_CLAIM", "preferred_username"),
        "name_claim": os.environ.get("SSO_DEFAULT_NAME_CLAIM", "name"),
        "disable_password_login": _str_to_bool(os.environ.get("SSO_DISABLE_PASSWORD_LOGIN", "false")),
    }


def is_sso_enabled() -> bool:
    cfg = get_sso_config()
    return bool(
        cfg["enabled"]
        and cfg["client_id"]
        and cfg["client_secret"]
        and cfg["discovery_url"]
        and cfg["redirect_uri"]
    )


def init_sso_client(app) -> Optional[OAuth]:
    """Initialize OAuth client if SSO is enabled."""
    global _oauth
    if not is_sso_enabled():
        return None

    if _oauth:
        return _oauth

    cfg = get_sso_config()
    oauth = OAuth(app)
    oauth.register(
        name="sso",
        server_metadata_url=cfg["discovery_url"],
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        client_kwargs={"scope": "openid email profile"},
    )
    _oauth = oauth
    app.logger.info("SSO client initialized with discovery URL %s", cfg["discovery_url"])
    return _oauth


def get_sso_client() -> Optional[OAuth]:
    """Return initialized OAuth client or None."""
    return _oauth


def is_domain_allowed(email: Optional[str]) -> bool:
    """Check if email domain is allowed for auto-registration."""
    if not email:
        return False
    cfg = get_sso_config()
    domains_env = cfg["allowed_domains"]
    if not domains_env:
        return True  # no restriction

    allowed = [d.strip().lower() for d in domains_env.split(",") if d.strip()]
    if not allowed:
        return True

    parts = email.lower().rsplit("@", 1)
    if len(parts) != 2:
        return False
    domain = parts[1]
    return domain in allowed


def _sanitize_username(candidate: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "", candidate or "")
    return sanitized or "user"


def generate_unique_username(preferred: Optional[str]) -> str:
    """Generate a unique username based on preferred value."""
    base = _sanitize_username(preferred or "user")
    base = base[:20]

    suffix = 0
    candidate = base
    while User.query.filter_by(username=candidate).first():
        suffix += 1
        candidate = f"{base[:18]}{suffix:02d}"
    return candidate


def create_or_update_sso_user(userinfo: Dict[str, str]) -> User:
    """Create or update a user from SSO (OIDC) claims."""
    cfg = get_sso_config()
    subject = userinfo.get("sub")
    email = userinfo.get("email")
    username_claim = cfg["username_claim"]
    name_claim = cfg["name_claim"]

    if not subject:
        raise ValueError("SSO userinfo does not include 'sub'")

    if not cfg["auto_register"] and not User.query.filter_by(sso_subject=subject).first():
        raise PermissionError("SSO auto-registration is disabled")

    if email and not is_domain_allowed(email):
        raise PermissionError("Email domain is not allowed for SSO sign-up")

    # Existing by subject
    user = User.query.filter_by(sso_subject=subject).first()
    if user:
        _update_profile_fields(user, userinfo, name_claim)
        db.session.commit()
        return user

    # Existing by email: attach SSO
    if email:
        existing_email_user = User.query.filter_by(email=email).first()
        if existing_email_user:
            existing_email_user.sso_provider = cfg["provider_name"]
            existing_email_user.sso_subject = subject
            _update_profile_fields(existing_email_user, userinfo, name_claim)
            db.session.commit()
            return existing_email_user

    # Create new user
    preferred_username = userinfo.get(username_claim) or (email.split("@")[0] if email else None)
    username = generate_unique_username(preferred_username)
    name_value = userinfo.get(name_claim) if name_claim else userinfo.get("name")

    user = User(
        username=username,
        email=email or f"{subject}@placeholder.local",
        password=None,
        sso_provider=cfg["provider_name"],
        sso_subject=subject,
        name=name_value,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _update_profile_fields(user: User, userinfo: Dict[str, str], name_claim: Optional[str]) -> None:
    """Update optional profile fields from SSO claims."""
    if not user.email and userinfo.get("email"):
        user.email = userinfo["email"]
    if name_claim and userinfo.get(name_claim):
        user.name = userinfo[name_claim]


def update_user_profile_from_claims(user: User, userinfo: Dict[str, str]) -> None:
    """Expose profile update for external callers (e.g., account linking)."""
    cfg = get_sso_config()
    _update_profile_fields(user, userinfo, cfg["name_claim"])


def link_sso_to_existing_user(user: User, provider: str, subject: str) -> User:
    """Link SSO identity to an existing user account."""
    if user.sso_subject and user.sso_subject != subject:
        raise ValueError("Account already linked to another SSO identity")

    user.sso_provider = provider
    user.sso_subject = subject
    db.session.commit()
    return user

