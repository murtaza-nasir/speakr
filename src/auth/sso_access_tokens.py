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
  Nobody is registered here, and email is never used to match.
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


def clear_cache():
    """Forget the discovery document and the key set (tests, settings changes)."""
    with _cache_lock:
        _cache.update(discovery=None, discovery_url=None, discovery_at=0.0,
                      jwks=None, jwks_uri=None, jwks_at=0.0, jwks_forced_at=0.0)


class SSOTokenError(Exception):
    """A token failed one of the checks. The message is for the log only."""


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


def looks_like_jwt(value):
    return isinstance(value, str) and value.count(".") == 2 and len(value) < 16384


def _http_get_json(url):
    """GET a JSON document from the provider, with bounded timeouts."""
    import requests
    resp = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False,
                        headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


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
            raise SSOTokenError("no user is linked to this subject")
        if not getattr(user, "is_active", True):
            raise SSOTokenError(f"user {user.id} is not active")
        return SSOAccessToken(user, client_id, scopes, claims, matched_clients)
    except SSOTokenError as exc:
        logger.info("SSO access token refused: %s", exc)
    except Exception as exc:  # never let a provider problem become a 500
        logger.info("SSO access token refused: unexpected %s", type(exc).__name__)
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
