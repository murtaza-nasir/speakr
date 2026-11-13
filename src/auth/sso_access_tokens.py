"""SSO access tokens as API credentials.

Apps that sign their users in with the same OIDC provider as Speakr (Open
WebUI in its "System OAuth" mode, mailr) can send that user's access token as
``Authorization: Bearer <access token>``. When the feature is on, such a token
is accepted on the API like a scoped API token whose scopes are
``SSO_API_SCOPES``. ``POST /api/v1/tokens/exchange`` trades one for a normal
API token, for apps that work in the background.

Only JWT access tokens are supported; opaque tokens would need the provider's
introspection endpoint. Every check below is required, and any failure gives
the same 401 as an invalid API token; the reason is logged at INFO, never the
token itself.

- ``alg`` is one of ALLOWED_ALGORITHMS (asymmetric only; never ``none`` or HS*).
- The signature is checked against the JWKS from the discovery document's
  ``jwks_uri``. No URL from the token (``jku``, ``x5u``) is ever fetched.
- ``iss`` equals the discovery document's issuer.
- ``exp`` is in the future; ``nbf`` and ``iat`` are not in the future (with
  CLOCK_LEEWAY seconds of leeway).
- The client is in SSO_API_ALLOWED_CLIENTS. For an RFC 9068 access token
  (header ``typ`` ``at+jwt``, as Pocket ID issues) the client is the required
  ``client_id`` claim; ``aud`` there is the granted audience, often only the
  issuer. For any other JWT the client is named by ``aud`` (string or list)
  or ``azp``, and a token carrying ``nonce`` or ``at_hash`` is refused as an
  ID token.
- ``sub`` belongs to an existing, active user linked by SSO (``sso_subject``).
  Email is never used to match.

Provisioning. When ``sub`` matches no user and SSO_API_AUTO_REGISTER,
SSO_AUTO_REGISTER and SSO_API_TOKENS_ENABLED are all on, the account is
created from the provider's userinfo endpoint (the discovery document's
``userinfo_endpoint``, called with the same access token). The claim rules
of interactive sign-in apply (allowed domains, verified email, username and
name claims), with two differences: an existing local account is never
linked by email (refused with sso_link_required), and userinfo without an
email is refused. On the exchange route the token must also name a client
that may exchange before an account is created. Each refusal has a code;
the exchange route answers with it (see refusal_response), every other
route gives the usual 401. Accounts are created only on requests under
/api/v1. A refusal decided from userinfo is kept per (issuer, sub) for five
minutes, a userinfo failure for one minute, so retries do not reach the
provider.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from src.utils.system_settings import apply_hot_settings

logger = logging.getLogger(__name__)

_SYSTEM_SETTING_MAP = {
    "SSO_API_TOKENS_ENABLED": ("sso_api_tokens_enabled", "SSO_API_TOKENS_ENABLED", False),
    "SSO_API_ALLOWED_CLIENTS": ("sso_api_allowed_clients", "SSO_API_ALLOWED_CLIENTS", ""),
    "SSO_API_SCOPES": ("sso_api_scopes", "SSO_API_SCOPES", "read"),
    "SSO_API_EXCHANGE_CLIENTS": ("sso_api_exchange_clients", "SSO_API_EXCHANGE_CLIENTS", ""),
    "SSO_API_AUTO_REGISTER": ("sso_api_auto_register", "SSO_API_AUTO_REGISTER", False),
    "SSO_API_CLIENT_NAMES": ("sso_api_client_names", "SSO_API_CLIENT_NAMES", ""),
}
_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)

# Asymmetric algorithms only: a symmetric (HS*) algorithm would let anyone who
# knows the provider's public key sign tokens (key confusion), and none is no
# signature at all.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                      "ES256", "ES384", "ES512", "EdDSA")

CLOCK_LEEWAY = 60            # seconds of clock skew allowed on exp, nbf and iat
DISCOVERY_TTL = 3600         # seconds the discovery document is kept
JWKS_TTL = 3600              # seconds the key set is kept
JWKS_REFETCH_INTERVAL = 30   # at most one refetch for an unknown kid per this many seconds
HTTP_TIMEOUT = (3.05, 5)     # connect and read timeouts for the provider

_cache_lock = threading.Lock()
_cache = {
    "discovery": None, "discovery_url": None, "discovery_at": 0.0,
    "jwks": None, "jwks_uri": None, "jwks_at": 0.0, "jwks_forced_at": 0.0,
}


# Refusals decided from userinfo, kept per (issuer, sub) so that a client
# that keeps retrying does not call the provider on every request.
REFUSAL_TTL = 300            # seconds for a policy refusal (link, domain, email)
USERINFO_FAILURE_TTL = 60    # seconds for sso_userinfo_failed
REFUSAL_CACHE_MAX = 1024     # entries kept at most
_refusals = {}               # (issuer, sub) -> (code, reason, expiry on time.monotonic())


def clear_cache():
    """Forget the discovery document, the key set and the cached refusals
    (tests, settings changes)."""
    with _cache_lock:
        _cache.update(discovery=None, discovery_url=None, discovery_at=0.0,
                      jwks=None, jwks_uri=None, jwks_at=0.0, jwks_forced_at=0.0)
        _refusals.clear()


def _cached_refusal(key):
    now = time.monotonic()
    with _cache_lock:
        entry = _refusals.get(key)
        if entry is None:
            return None
        if entry[2] <= now:
            del _refusals[key]
            return None
        return entry


def _remember_refusal(key, code, reason):
    ttl = USERINFO_FAILURE_TTL if code == "sso_userinfo_failed" else REFUSAL_TTL
    now = time.monotonic()
    with _cache_lock:
        if key not in _refusals and len(_refusals) >= REFUSAL_CACHE_MAX:
            for stale in [k for k, v in _refusals.items() if v[2] <= now]:
                del _refusals[stale]
            while len(_refusals) >= REFUSAL_CACHE_MAX:
                del _refusals[next(iter(_refusals))]   # the oldest entry
        _refusals[key] = (code, reason, now + ttl)


def _forget_refusal(key):
    with _cache_lock:
        _refusals.pop(key, None)


class SSOTokenError(Exception):
    """A token failed one of the checks. The message is for the log only."""


class SSOProvisionRefused(Exception):
    """A valid token whose subject has no account, and none is created.

    ``code`` is the API error code the exchange route answers with; the
    message is for the log only."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


# The status of each refusal on the exchange route.
REFUSAL_STATUS = {
    "sso_registration_closed": 403,
    "sso_exchange_not_allowed": 403,
    "sso_link_required": 409,
    "sso_domain_not_allowed": 403,
    "sso_email_not_verified": 403,
    "sso_email_missing": 403,
    "sso_userinfo_failed": 502,
}


class SSOAccessToken:
    """A validated SSO access token, in the shape of an APIToken.

    It is not stored. current_api_token(), require_scope, the scope error
    answer and the rate limits read the same attributes as on an APIToken.
    The rate-limit key is per user ("token:sso-<user id>"), and the account
    budget is shared with that user's API tokens.
    """

    is_sso = True
    revoked = False
    last_used_at = None

    def __init__(self, user, client_id, scopes, claims, clients=None):
        self.user = user
        # Every allowed client the token names in aud or azp.
        self.clients = frozenset(clients or {client_id})
        self.user_id = user.id
        self.id = f"sso-{user.id}"
        self.client_id = client_id
        self.name = f"SSO ({client_id})"
        self._scopes = frozenset(scopes)
        self.subject = claims.get("sub")
        self.created_at = _utc_naive(claims.get("iat"))
        self.expires_at = _utc_naive(claims.get("exp"))

    @property
    def scope_set(self):
        return self._scopes

    @property
    def scope_list(self):
        return sorted(self._scopes)

    def is_expired(self):
        return self.expires_at is not None and self.expires_at < datetime.utcnow()

    def is_valid(self):
        return not self.is_expired()


def _utc_naive(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)


def _split(value):
    return [part for part in str(value or "").replace(",", " ").split() if part]


def allowed_clients():
    return set(_split(_self.SSO_API_ALLOWED_CLIENTS))


def exchange_clients():
    """Clients whose tokens may be exchanged: in SSO_API_EXCHANGE_CLIENTS and
    in SSO_API_ALLOWED_CLIENTS. Empty means that no exchange is allowed."""
    return set(_split(_self.SSO_API_EXCHANGE_CLIENTS)) & allowed_clients()


def exchange_client_for(sso_token):
    """The client to record for an exchange of this token, or None when the
    token names no client that may exchange. azp is preferred."""
    allowed = exchange_clients() & set(sso_token.clients)
    if not allowed:
        return None
    return sso_token.client_id if sso_token.client_id in allowed else sorted(allowed)[0]


def granted_scopes():
    """The scopes an SSO token carries: the known names in SSO_API_SCOPES.

    'full' is never granted to an SSO token; unknown names are ignored.
    """
    from src.models.api_token import TOKEN_SCOPES
    requested = set(_split(_self.SSO_API_SCOPES))
    unknown = requested - set(TOKEN_SCOPES)
    if unknown:
        logger.warning("SSO_API_SCOPES lists unknown scopes, ignored: %s", ", ".join(sorted(unknown)))
    return frozenset(requested & set(TOKEN_SCOPES))


def is_enabled():
    """True when SSO tokens may be checked at all."""
    from src.auth.sso import get_sso_config
    cfg = get_sso_config()
    return bool(_self.SSO_API_TOKENS_ENABLED and cfg["enabled"] and cfg["discovery_url"])


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes")


def auto_register_enabled():
    """True when an unknown subject with a valid token gets an account:
    SSO_API_AUTO_REGISTER and SSO_AUTO_REGISTER both on, and the feature on."""
    from src.auth.sso import get_sso_config
    return bool(_truthy(_self.SSO_API_AUTO_REGISTER) and get_sso_config()["auto_register"]
                and is_enabled())


def exchange_available():
    """True when an exchange can succeed for some client."""
    return bool(is_enabled() and exchange_clients())


def exchange_provisioning_available():
    """True when an exchange for a person with no account creates the account."""
    return bool(exchange_available() and auto_register_enabled())


def client_names():
    """SSO_API_CLIENT_NAMES as {client_id: name}.

    Pairs are ``client_id=Name``, separated by commas or new lines. Pairs
    without ``=``, with an empty side, or with an over-long side are ignored.
    """
    names = {}
    raw = str(_self.SSO_API_CLIENT_NAMES or "")
    for part in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
        if "=" not in part:
            continue
        client_id, name = (side.strip() for side in part.split("=", 1))
        if not client_id or not name or len(client_id) > 255:
            continue
        names[client_id] = name[:CLIENT_NAME_MAX]
    return names


CLIENT_NAME_MAX = 60


def client_name(client_id):
    """The display name of an SSO client, or None when it has none."""
    if not client_id:
        return None
    try:
        return client_names().get(client_id)
    except Exception:
        return None


def looks_like_jwt(value):
    return isinstance(value, str) and value.count(".") == 2 and len(value) < 16384


def _http_get_json(url):
    """GET a JSON document from the provider, with bounded timeouts."""
    import requests
    resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False,
                        headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _http_get_userinfo(url, access_token):
    """GET the userinfo document with the access token, with bounded timeouts
    and no redirects. Returns (status code, parsed JSON or None)."""
    import requests
    resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False,
                        headers={"Accept": "application/json",
                                 "Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _discovery():
    from src.auth.sso import get_sso_config
    url = get_sso_config()["discovery_url"]
    now = time.monotonic()
    with _cache_lock:
        if (_cache["discovery"] is not None and _cache["discovery_url"] == url
                and now - _cache["discovery_at"] < DISCOVERY_TTL):
            return _cache["discovery"]
    try:
        doc = _http_get_json(url)
    except Exception as exc:
        raise SSOTokenError(f"discovery document could not be fetched: {type(exc).__name__}")
    if not isinstance(doc, dict) or not doc.get("issuer") or not doc.get("jwks_uri"):
        raise SSOTokenError("discovery document has no issuer or jwks_uri")
    with _cache_lock:
        _cache.update(discovery=doc, discovery_url=url, discovery_at=now)
    return doc


def _key_set(jwks_uri, force=False):
    """The provider's keys, cached. ``force`` refetches, at most once per
    JWKS_REFETCH_INTERVAL, for a token whose kid is not in the cached set."""
    from joserfc.jwk import KeySet
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["jwks"] if _cache["jwks_uri"] == jwks_uri else None
        fresh = cached is not None and now - _cache["jwks_at"] < JWKS_TTL
        if cached is not None and (not force and fresh):
            return cached, False
        if force and cached is not None:
            if now - _cache["jwks_forced_at"] < JWKS_REFETCH_INTERVAL:
                return cached, False
            _cache["jwks_forced_at"] = now
    try:
        data = _http_get_json(jwks_uri)
        key_set = KeySet.import_key_set(data)
    except Exception as exc:
        raise SSOTokenError(f"JWKS could not be fetched or read: {type(exc).__name__}")
    with _cache_lock:
        _cache.update(jwks=key_set, jwks_uri=jwks_uri, jwks_at=now)
    return key_set, True


def _find_key(key_set, kid):
    if kid is None:
        # A token without kid is accepted only when the set holds one key.
        return key_set.keys[0] if len(key_set.keys) == 1 else None
    for key in key_set.keys:
        if key.kid == kid:
            return key
    return None


def _is_rfc9068(header):
    """True for a JWT access token profile header (RFC 9068 section 2.1)."""
    typ = header.get("typ")
    return isinstance(typ, str) and typ.strip().lower() in ("at+jwt", "application/at+jwt")


def _verify(value):
    """Check every claim of a JWT access token. Returns (claims, client_id, clients)."""
    from joserfc import jws, jwt
    from joserfc.errors import JoseError

    clients = allowed_clients()
    if not clients:
        raise SSOTokenError("SSO_API_ALLOWED_CLIENTS is empty")
    try:
        header = jws.extract_compact(value.encode("ascii")).headers()
    except Exception:
        raise SSOTokenError("not a JWS compact token")
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise SSOTokenError(f"algorithm {alg!r} is not allowed")
    if "crit" in header:
        raise SSOTokenError("critical header parameters are not supported")

    doc = _discovery()
    jwks_uri = doc["jwks_uri"]
    key_set, fetched = _key_set(jwks_uri)
    kid = header.get("kid")
    key = _find_key(key_set, kid)
    if key is None and not fetched:
        key_set, _ = _key_set(jwks_uri, force=True)
        key = _find_key(key_set, kid)
    if key is None:
        raise SSOTokenError(f"no key for kid {kid!r}")

    try:
        token = jwt.decode(value, key, algorithms=[alg])
    except JoseError as exc:
        raise SSOTokenError(f"signature check failed: {exc.error}")
    except Exception as exc:
        raise SSOTokenError(f"token could not be decoded: {type(exc).__name__}")

    claims = token.claims
    registry = jwt.JWTClaimsRegistry(
        leeway=CLOCK_LEEWAY,
        iss={"essential": True, "value": doc["issuer"]},
        exp={"essential": True},
        sub={"essential": True},
    )
    try:
        registry.validate(claims)
    except JoseError as exc:
        raise SSOTokenError(f"claim check failed: {exc.description or exc.error}")
    if not isinstance(claims.get("exp"), (int, float)) or isinstance(claims.get("exp"), bool):
        raise SSOTokenError("exp is not a number")

    if _is_rfc9068(header):
        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise SSOTokenError("at+jwt token has no client_id")
        if client_id not in clients:
            raise SSOTokenError(f"client not allowed (client_id={client_id!r})")
        return claims, client_id, {client_id}
    if "nonce" in claims or "at_hash" in claims:
        raise SSOTokenError("ID token, not an access token (nonce or at_hash present)")

    aud = claims.get("aud")
    if isinstance(aud, str):
        audiences = [aud]
    elif isinstance(aud, list):
        audiences = [a for a in aud if isinstance(a, str)]
    else:
        audiences = []
    azp = claims.get("azp") if isinstance(claims.get("azp"), str) else None
    matched = [a for a in audiences if a in clients]
    if matched:
        client_id = azp if azp in clients else matched[0]
    elif azp in clients:
        client_id = azp
    else:
        raise SSOTokenError(f"client not allowed (aud={audiences!r}, azp={azp!r})")
    return claims, client_id, set(matched) | ({azp} if azp in clients else set())


def _userinfo(value, subject):
    """The provider's claims for this access token; sub must match the token."""
    endpoint = _discovery().get("userinfo_endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise SSOProvisionRefused("sso_userinfo_failed", "discovery document has no userinfo_endpoint")
    try:
        status, claims = _http_get_userinfo(endpoint, value)
    except Exception as exc:
        raise SSOProvisionRefused("sso_userinfo_failed",
                                  f"userinfo endpoint could not be reached: {type(exc).__name__}")
    if status != 200:
        raise SSOProvisionRefused("sso_userinfo_failed", f"userinfo endpoint answered {status}")
    if not isinstance(claims, dict):
        raise SSOProvisionRefused("sso_userinfo_failed", "userinfo is not a JSON object")
    if claims.get("sub") != subject:
        raise SSOProvisionRefused("sso_userinfo_failed", "userinfo sub does not match the token sub")
    return claims


def _provision(value, subject, issuer):
    """Create the account for a valid token whose subject has none.

    Raises SSOProvisionRefused with the reason when no account is created.
    A refusal decided from userinfo, or a userinfo failure, is kept for
    REFUSAL_TTL or USERINFO_FAILURE_TTL seconds; within that time the same
    refusal is given without calling the provider."""
    if not auto_register_enabled():
        raise SSOProvisionRefused("sso_registration_closed", "no user is linked to this subject")
    key = (issuer, subject)
    cached = _cached_refusal(key)
    if cached is not None:
        raise SSOProvisionRefused(cached[0], f"{cached[1]} (cached)")
    try:
        user = _provision_from_userinfo(value, subject)
    except SSOProvisionRefused as exc:
        _remember_refusal(key, exc.code, str(exc))
        raise
    _forget_refusal(key)
    return user


def _provision_from_userinfo(value, subject):
    from src.auth.sso import create_sso_account, get_sso_config, is_domain_allowed
    from src.models import User
    claims = _userinfo(value, subject)
    email = User.normalize_email(claims.get("email")) if isinstance(claims.get("email"), str) else None
    if not email:
        raise SSOProvisionRefused("sso_email_missing", "userinfo has no email")
    if not is_domain_allowed(email):
        raise SSOProvisionRefused("sso_domain_not_allowed", "email domain is not allowed")
    if get_sso_config()["require_verified_email"] and not _truthy(claims.get("email_verified")):
        raise SSOProvisionRefused("sso_email_not_verified", "email is not verified")
    existing = User.find_by_email(email)
    if existing is not None and existing.sso_subject == subject:
        return existing  # created by a concurrent first request
    if existing is not None:
        # Never linked by email here; the person signs in to Speakr once.
        raise SSOProvisionRefused("sso_link_required",
                                  f"user {existing.id} has this email and is not linked to this subject")
    user = create_sso_account(claims, email)
    logger.info("Account %s created from an SSO access token", user.id)
    return user


def _may_provision_here():
    """Accounts are created only on requests under /api/v1."""
    from flask import has_request_context, request
    return has_request_context() and request.path.startswith("/api/v1/")


def _is_exchange_request():
    from flask import has_request_context, request
    return has_request_context() and request.endpoint == "api_v1.exchange_sso_token"


def _record_refusal(code):
    from flask import has_request_context, request
    if has_request_context():
        request.environ["_speakr_sso_refusal"] = code


def refusal_code():
    """The provisioning refusal of this request's SSO token, or None."""
    from flask import has_request_context, request
    if not has_request_context():
        return None
    return request.environ.get("_speakr_sso_refusal")


def refusal_response():
    """The (body, status) for a provisioning refusal on the exchange route,
    or None when the request had none."""
    code = refusal_code()
    if code is None:
        return None
    from src.utils.localization import api_error
    return api_error(code), REFUSAL_STATUS.get(code, 403)


def validate(value):
    """The SSOAccessToken for a bearer value, or None. Never raises."""
    if not looks_like_jwt(value):
        return None
    try:
        if not is_enabled():
            return None
        claims, client_id, matched_clients = _verify(value)
        scopes = granted_scopes()
        if not scopes:
            raise SSOTokenError("SSO_API_SCOPES grants no known scope")
        from src.models import User
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise SSOTokenError("sub is missing")
        user = User.query.filter_by(sso_subject=subject).first()
        if user is None:
            if not _may_provision_here():
                raise SSOTokenError("no user is linked to this subject")
            if _is_exchange_request() and not (exchange_clients() & set(matched_clients)):
                # Nothing is created for a client that may not exchange.
                raise SSOProvisionRefused("sso_exchange_not_allowed",
                                          f"client {client_id!r} may not exchange")
            user = _provision(value, subject, claims.get("iss"))
        if not getattr(user, "is_active", True):
            raise SSOTokenError(f"user {user.id} is not active")
        return SSOAccessToken(user, client_id, scopes, claims, matched_clients)
    except SSOTokenError as exc:
        logger.info("SSO access token refused: %s", exc)
    except SSOProvisionRefused as exc:
        logger.info("SSO access token refused (%s): %s", exc.code, exc)
        _record_refusal(exc.code)
    except Exception as exc:  # never let a provider problem become a 500
        logger.info("SSO access token refused: unexpected %s", type(exc).__name__)
        try:
            from src.database import db
            db.session.rollback()
        except Exception:
            pass
    return None


def token_for_request(value):
    """validate(), once per request and value (kept in the WSGI environ)."""
    from flask import has_request_context, request
    if not has_request_context():
        return validate(value)
    cache = request.environ.setdefault("_speakr_sso_tokens", {})
    if value not in cache:
        cache[value] = validate(value)
    return cache[value]
