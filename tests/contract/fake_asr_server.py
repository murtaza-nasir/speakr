"""A stand-in for whisperx-asr-service's HTTP API, for Speakr's tests.

It runs on a real socket in a background thread, so Speakr's asr_endpoint
connector talks to it exactly as it talks to the service. Every /asr request
is recorded and answered with the model decision the real service would make
(tests/contract/whisperx_model_rules.py): a request the service would refuse
gets the same status and error body.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tests.contract.whisperx_model_rules import CANONICAL_MODELS, ModelRejected, ServiceModelRules

EMBEDDING_DIM = 256


def _embedding(seed):
    """A fixed, unit-scale voice embedding per speaker label."""
    return [(((i * 31 + seed * 17) % 97) / 97.0) - 0.5 for i in range(EMBEDDING_DIM)]


class FakeASRServer:
    """Context manager: ``with FakeASRServer(rules) as asr: asr.url``."""

    def __init__(self, rules=None):
        self.rules = rules or ServiceModelRules()
        self.requests = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- lifecycle --------------------------------------------------------
    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/health", "/"):
                    return self._json(200, {"status": "healthy"})
                if path == "/v1/models":
                    return self._json(200, {"object": "list", "data": [
                        {"id": name, "object": "model"} for name in ("whisper-1",) + CANONICAL_MODELS
                    ]})
                return self._json(404, {"detail": "Not Found"})

            def do_POST(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if url.path != "/asr":
                    return self._json(404, {"detail": "Not Found"})
                query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
                record = {
                    "model_sent": "model" in query,
                    "model": query.get("model"),
                    "params": query,
                    "filename": _filename(body),
                }
                try:
                    record["resolved"] = fake.rules.resolve(query.get("model"))
                    record["status"] = 200
                except ModelRejected as rejected:
                    record["resolved"] = None
                    record["status"] = rejected.status
                    record["detail"] = rejected.detail
                with fake._lock:
                    fake.requests.append(record)
                if record["status"] != 200:
                    return self._json(record["status"], {"detail": record["detail"]})
                return self._json(200, _transcript(query))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def reset(self):
        with self._lock:
            self.requests.clear()


def _filename(body):
    """The uploaded file name from a multipart body, for readable failures."""
    marker = b'filename="'
    start = body.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = body.find(b'"', start)
    return body[start:end].decode(errors="replace") if end > start else None


def _transcript(query):
    """A small diarized transcript with one speaker, as the service returns it."""
    segments = [
        {"start": 0.0, "end": 2.0, "text": "Hello from the test service.", "speaker": "SPEAKER_00",
         "words": [{"word": "Hello", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"}]},
        {"start": 2.0, "end": 4.0, "text": "This is a second sentence.", "speaker": "SPEAKER_00",
         "words": [{"word": "This", "start": 2.0, "end": 2.3, "speaker": "SPEAKER_00"}]},
    ]
    body = {
        "text": " ".join(s["text"] for s in segments),
        "language": query.get("language") or "en",
        "segments": segments,
    }
    if str(query.get("return_speaker_embeddings", "")).lower() == "true":
        body["speaker_embeddings"] = {"SPEAKER_00": _embedding(0)}
    return body
