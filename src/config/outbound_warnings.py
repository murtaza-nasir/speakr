"""Startup warnings for content sent over plain http to another machine.

Audio goes to the transcription service; transcripts, notes and questions go
to the text model, the chat model, the translation model and the embedding
service. With an http:// address, that content crosses the network in clear
text. One line per service is logged at startup when its address uses http://
and the host is on another machine.

The rule (``is_local_host``): no warning for

* a loopback host: ``localhost``, a name ending in ``.localhost``, 127.0.0.0/8
  and ``::1``;
* a single-label name (no dot), which is how a service on the same Docker
  Compose network is addressed (``http://whisperx:9000``);
* ``host.docker.internal`` and ``gateway.docker.internal``, the Docker host
  itself.

Every other host warns, private address ranges included (10.0.0.0/8,
172.16.0.0/12, 192.168.0.0/16): the content still crosses a network there.
The line names the service, the variable and the host. It never holds the
path, the query, a user name or a key.

Webhook addresses are set per webhook by each user, and an http address is
refused there unless the user allows it for that webhook; there is no global
webhook address to check at startup.
"""

import ipaddress
import os
from urllib.parse import urlparse

# (service as named in the log, environment variable)
SERVICE_URLS = (
    ('transcription service', 'ASR_BASE_URL'),
    ('transcription service', 'TRANSCRIPTION_BASE_URL'),
    ('text model', 'TEXT_MODEL_BASE_URL'),
    ('chat model', 'CHAT_MODEL_BASE_URL'),
    ('translation model', 'TRANSLATION_MODEL_BASE_URL'),
    ('embedding service', 'EMBEDDING_BASE_URL'),
)

_DOCKER_HOST_NAMES = frozenset({'host.docker.internal', 'gateway.docker.internal'})


def is_local_host(host):
    """Whether traffic to this host stays on the machine or its Compose network."""
    host = (host or '').strip().strip('[]').rstrip('.').lower()
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    if host in _DOCKER_HOST_NAMES:
        return True
    return '.' not in host


def plain_http_host(url):
    """The host of an http:// address on another machine, else None."""
    value = (url or '').split('#')[0].strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() != 'http' or not host:
        return None
    return None if is_local_host(host) else host


def plain_http_warnings(environ=None):
    """One warning line per service whose address is plain http to another machine."""
    environ = os.environ if environ is None else environ
    lines = []
    for service, variable in SERVICE_URLS:
        host = plain_http_host(environ.get(variable))
        if host:
            lines.append(
                f"The {service} ({variable}) is reached over plain http at host {host}: "
                f"content is sent there unencrypted. Use an https address, or keep the "
                f"service on this machine or on the same Docker network.")
    return lines


def log_plain_http_warnings(app, environ=None):
    lines = plain_http_warnings(environ)
    for line in lines:
        app.logger.warning(line)
    return lines
