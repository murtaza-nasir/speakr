"""Webhook delivery connects only to the addresses that were validated (#694).

The URL host is resolved once, every address is checked, and the POST is
pinned to those addresses: no second DNS lookup takes place, so a hostname
rebound between validation and connection cannot retarget the request. This
holds for hosts on WEBHOOK_INTRANET_HOST_ALLOWLIST as well. The URL host stays
the Host header, the TLS SNI name and the certificate name; redirects are not
followed, and a host that was not pinned fails closed.
"""

import datetime as dt
import http.server
import os
import socket
import ssl
import sys
import threading
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app  # noqa: E402
from src.services import webhook_dispatch as wd  # noqa: E402


class _Receiver:
    """A local HTTP(S) receiver bound to one loopback address."""

    def __init__(self, ip, status=200, location=None, tls_context=None):
        self.received = []
        self.hosts = []
        received, hosts = self.received, self.hosts

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                hosts.append(self.headers.get('Host'))
                self.send_response(status)
                if location:
                    self.send_header('Location', location)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer((ip, 0), Handler)
        if tls_context is not None:
            self.server.socket = tls_context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _dns(monkeypatch, answers):
    """Make getaddrinfo return answers[0] on the first call, answers[1] on the
    second, and so on (the last answer repeats). Returns the call log."""
    calls = []

    def fake(host, port, *args, **kwargs):
        calls.append(host)
        ip = answers[min(len(calls), len(answers)) - 1]
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, '', (ip, port or 0))]

    monkeypatch.setattr(wd.socket, 'getaddrinfo', fake)
    return calls


def _webhook(url):
    wh = MagicMock()
    wh.url = url
    wh.allow_http = True
    wh.secret = 's'
    wh.previous_secret = None
    return wh


def _delivery():
    delivery = MagicMock()
    delivery.payload = '{"x":1}'
    delivery.event_id = 'evt'
    delivery.event_type = 'recording.completed'
    delivery.attempt_count = 0
    return delivery


def test_allowlisted_host_pinned_to_validated_address(monkeypatch):
    """Validation sees 127.0.0.1, a later lookup would give 127.0.0.2: the POST
    goes to 127.0.0.1 only, so the receiver on the rebound address gets nothing."""
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^rebind\.test$')
    rebound = _Receiver('127.0.0.2')
    try:
        _dns(monkeypatch, ['127.0.0.1', '127.0.0.2'])
        with app.app_context():
            status, _preview, error = wd._post_delivery(
                _delivery(), _webhook(f'http://rebind.test:{rebound.port}/hook'))
        assert rebound.received == []
        assert status is None
        assert error
    finally:
        rebound.close()


def test_allowlisted_host_delivers_to_validated_private_ip(monkeypatch):
    """Positive control: an allowlisted intranet host still receives deliveries,
    the original hostname is the Host header, and DNS is consulted once."""
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^rebind\.test$')
    receiver = _Receiver('127.0.0.1')
    try:
        calls = _dns(monkeypatch, ['127.0.0.1'])
        with app.app_context():
            status, preview, error = wd._post_delivery(
                _delivery(), _webhook(f'http://rebind.test:{receiver.port}/hook'))
        assert (status, preview, error) == (200, 'ok', None)
        assert receiver.received == [b'{"x":1}']
        assert receiver.hosts == [f'rebind.test:{receiver.port}']
        assert calls == ['rebind.test']
    finally:
        receiver.close()


def test_non_allowlisted_rebinding_never_reaches_rebound_address(monkeypatch):
    """A public address at validation and 127.0.0.2 afterwards: the connection
    is attempted to the validated public address only."""
    monkeypatch.delenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', raising=False)
    rebound = _Receiver('127.0.0.2')
    attempted = []

    def fake_open(ip, port, timeout, source_address=None, socket_options=None):
        attempted.append(ip)
        raise ConnectionRefusedError('refused in test')

    monkeypatch.setattr(wd, '_open_pinned_socket', fake_open)
    try:
        _dns(monkeypatch, ['8.8.8.8', '127.0.0.2'])
        with app.app_context():
            status, _preview, error = wd._post_delivery(
                _delivery(), _webhook(f'http://rebind.test:{rebound.port}/hook'))
        assert attempted == ['8.8.8.8']
        assert rebound.received == []
        assert status is None and error
    finally:
        rebound.close()


def test_private_address_at_delivery_is_blocked(monkeypatch):
    """Without the allowlist, a host that resolves to a private address at
    delivery is refused before any connection."""
    monkeypatch.delenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', raising=False)
    monkeypatch.setattr(wd, '_open_pinned_socket', MagicMock(side_effect=AssertionError('no connect')))
    _dns(monkeypatch, ['127.0.0.1'])
    with app.app_context():
        status, _preview, error = wd._post_delivery(_delivery(), _webhook('http://rebind.test/hook'))
    assert status is None
    assert error.startswith('blocked at delivery')
    assert '127.0.0.1' in error


def test_peer_outside_pinned_addresses_is_refused(monkeypatch):
    """Should the socket end anywhere but a pinned address, the connection is
    dropped before the request is sent."""
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^rebind\.test$')
    receiver = _Receiver('127.0.0.2')
    real_open = wd._open_pinned_socket

    def misdirected(ip, port, timeout, source_address=None, socket_options=None):
        return real_open('127.0.0.2', port, timeout, source_address, socket_options)

    monkeypatch.setattr(wd, '_open_pinned_socket', misdirected)
    try:
        with pytest.raises(wd._WebhookSSRFError, match='unvalidated address'):
            wd._http_post(f'http://rebind.test:{receiver.port}/hook',
                          pinned_ips=['127.0.0.1'], data=b'secret')
        assert receiver.received == []
    finally:
        receiver.close()


def test_pinned_connection_tries_each_validated_address(monkeypatch):
    """A host with several validated addresses falls back to the next one when
    the first refuses, as a normal resolver would."""
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^multi\.test$')
    receiver = _Receiver('127.0.0.1')
    try:
        resp = wd._http_post(f'http://multi.test:{receiver.port}/hook',
                             pinned_ips=['127.0.0.3', '127.0.0.1'], data=b'x')
        assert resp.status_code == 200
        assert receiver.received == [b'x']
    finally:
        receiver.close()


def test_redirect_is_not_followed(monkeypatch):
    """A 3xx is returned as the delivery status; its Location is never fetched,
    even when a caller asks for redirects."""
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^(hop|other)\.test$')
    target = _Receiver('127.0.0.2')
    hop = _Receiver('127.0.0.1', status=307, location=f'http://other.test:{target.port}/x')
    try:
        _dns(monkeypatch, ['127.0.0.2'])
        resp = wd._http_post(f'http://hop.test:{hop.port}/hook', pinned_ips=['127.0.0.1'],
                             data=b'x', allow_redirects=True)
        assert resp.status_code == 307
        assert hop.received == [b'x']
        assert target.received == []
    finally:
        hop.close()
        target.close()


def test_unpinned_host_fails_closed():
    """A session pinned for one host refuses to connect to any other host."""
    session = wd.ssrf_guarded_session(pins={'a.test': ['127.0.0.1']}, allow_private=True)
    with pytest.raises(wd._WebhookSSRFError, match='no validated address'):
        session.post('http://b.test:9/hook', data=b'x', timeout=2)


def test_dns_failure_is_a_retryable_network_error(monkeypatch):
    """No resolution means no pinned address and no connection: a normal
    network error, not a block, and no fresh lookup by the transport."""
    calls = []

    def failing(host, port, *args, **kwargs):
        calls.append(host)
        raise socket.gaierror('no such host')

    monkeypatch.setattr(wd.socket, 'getaddrinfo', failing)
    with app.app_context():
        status, _preview, error = wd._post_delivery(_delivery(), _webhook('http://gone.test/hook'))
    assert status is None
    assert error and not error.startswith('blocked at delivery')
    assert calls == ['gone.test']
    assert wd._is_retryable_status(status)


def _self_signed(tmp_path, hostname):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / 'cert.pem'
    key_path = tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def test_https_pinned_connection_verifies_certificate_for_hostname(monkeypatch, tmp_path):
    """TLS to a pinned IP still sends the URL hostname as SNI and verifies the
    certificate against it: a certificate for the hostname is accepted, and the
    same server is refused under a different hostname."""
    monkeypatch.setenv('WEBHOOK_INTRANET_HOST_ALLOWLIST', r'^(pinned|wrong)\.test$')
    cert, key = _self_signed(tmp_path, 'pinned.test')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    sni_seen = []
    context.sni_callback = lambda sock, name, ctx: sni_seen.append(name)
    receiver = _Receiver('127.0.0.1', tls_context=context)
    try:
        resp = wd._http_post(f'https://pinned.test:{receiver.port}/hook',
                             pinned_ips=['127.0.0.1'], data=b'x', verify=cert)
        assert resp.status_code == 200
        assert receiver.received == [b'x']
        assert sni_seen[0] == 'pinned.test'
        assert receiver.hosts == [f'pinned.test:{receiver.port}']

        with pytest.raises(Exception) as exc:
            wd._http_post(f'https://wrong.test:{receiver.port}/hook',
                          pinned_ips=['127.0.0.1'], data=b'y', verify=cert)
        assert 'certificate' in str(exc.value).lower() or 'hostname' in str(exc.value).lower()
        assert receiver.received == [b'x']
    finally:
        receiver.close()
