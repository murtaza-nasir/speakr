"""A small OpenAI-compatible ASR endpoint for the browser tests.

The Transcription models panel discovers and tests providers with real HTTP
requests. This server answers the paths the connectors probe, so those tests
run without a network:

- ``GET /v1/models`` and ``GET /models``: the model list;
- ``GET /health`` and ``GET /``: a plain 200 for endpoint connectors.

Use it as a context manager; ``url`` is the base URL to put in the modal.

    with FakeAsrServer(models=['small', 'large-v3']) as fake:
        page.fill('#asr-field-base_url', fake.url)

``status`` makes every model path answer that status (401 for a wrong key).
``models=[]`` models a provider that reports no models.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAsrServer:
    def __init__(self, models=None, status=200, auth_required=False):
        self.models = list(models if models is not None else ['small', 'large-v3', 'distil-large-v3'])
        self.status = status
        self.auth_required = auth_required
        self.requests = []
        self._server = None
        self._thread = None

    @property
    def url(self):
        host, port = self._server.server_address
        return f'http://{host}:{port}'

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers)))
                if outer.auth_required and not self.headers.get('Authorization'):
                    return self._json(401, {'error': 'unauthorized'})
                if self.path in ('/health', '/'):
                    return self._json(200, {'status': 'ok'})
                if self.path in ('/v1/models', '/models'):
                    if outer.status != 200:
                        return self._json(outer.status, {'error': 'denied'})
                    return self._json(200, {
                        'object': 'list',
                        'data': [{'id': name, 'object': 'model'} for name in outer.models],
                    })
                return self._json(404, {'error': 'not found'})

            def _json(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False
