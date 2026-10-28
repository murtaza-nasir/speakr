"""Which peers may set X-Forwarded-* headers (#498, #501).

ProxyFix trusts TRUSTED_PROXY_HOPS forwarded headers from whoever connects.
With no reverse proxy in front of Speakr, that is the client itself, which can
then choose its own address for rate limits and the scheme and host the app
sees. TRUSTED_PROXIES (comma-separated addresses or CIDR ranges) restricts
the headers to requests whose TCP peer is one of those proxies. When it is
unset, behaviour is unchanged: the headers are trusted from any peer.
"""

import ipaddress
import logging
import os

from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)


def parse_trusted_proxies(raw):
    """Networks from a comma-separated list; invalid entries are logged and skipped."""
    networks = []
    for item in (raw or '').split(','):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXIES: ignoring %r, which is not an IP address or CIDR range", item)
    return networks


def _peer_is_trusted(peer, networks):
    try:
        address = ipaddress.ip_address((peer or '').split('%', 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


class TrustedProxyMiddleware:
    """ProxyFix for listed peers only; plain ProxyFix when no list is set.

    The environment is read on each request so that tests can change it.
    """

    def __init__(self, wsgi_app):
        self.app = wsgi_app
        self._cache = {}

    def _proxy_fix(self, hops):
        fixer = self._cache.get(hops)
        if fixer is None:
            fixer = ProxyFix(self.app, x_for=hops, x_proto=hops, x_host=hops, x_prefix=hops)
            self._cache[hops] = fixer
        return fixer

    def _networks(self, raw):
        key = ('networks', raw)
        if key not in self._cache:
            self._cache[key] = parse_trusted_proxies(raw)
        return self._cache[key]

    def __call__(self, environ, start_response):
        try:
            hops = int(os.environ.get('TRUSTED_PROXY_HOPS', '1'))
        except ValueError:
            hops = 1
        if hops <= 0:
            return self.app(environ, start_response)
        raw = os.environ.get('TRUSTED_PROXIES', '').strip()
        if raw and not _peer_is_trusted(environ.get('REMOTE_ADDR'), self._networks(raw)):
            return self.app(environ, start_response)
        return self._proxy_fix(hops)(environ, start_response)


def log_proxy_trust():
    """One startup line saying whose forwarded headers are trusted."""
    try:
        hops = int(os.environ.get('TRUSTED_PROXY_HOPS', '1'))
    except ValueError:
        logger.warning("TRUSTED_PROXY_HOPS is not a number; using 1")
        hops = 1
    if hops > 0 and not os.environ.get('TRUSTED_PROXIES', '').strip():
        logger.warning(
            "X-Forwarded-For/-Proto/-Host are trusted from any client (TRUSTED_PROXY_HOPS=%d, "
            "TRUSTED_PROXIES unset). Behind a reverse proxy, set TRUSTED_PROXIES to its address "
            "or network; with no proxy in front of Speakr, set TRUSTED_PROXY_HOPS=0.", hops)
