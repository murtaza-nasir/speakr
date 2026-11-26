"""A small stand-in for audio.cpp's audiocpp_server, for the browser tests.

It answers the routes the audio.cpp connector uses, after the server's API
reference (app/server/api.md, v0.9.6):

- ``GET /health`` and ``GET /v1/models``;
- ``POST /v1/audio/transcriptions/details``: reads the multipart upload,
  keeps the ``model``, ``language`` and ``file`` fields in ``requests``, and
  answers with two speaker turns in sample offsets and their ``sample_rate``.

    with FakeAudioCppServer() as fake:
        page.fill('#asr-field-base_url', fake.url)
"""

import json
import threading
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 16000
TURNS = [
    {"start_sample": 0, "end_sample": 3 * RATE, "speaker_id": "speaker_0", "confidence": 0.9,
     "text": "Good morning, this is the first speaker."},
    {"start_sample": 3 * RATE, "end_sample": 6 * RATE, "speaker_id": "speaker_1", "confidence": 0.8,
     "text": "And this is the second speaker answering."},
]


def details_response():
    return {
        "text": " ".join(turn["text"] for turn in TURNS),
        "language": "en",
        "speaker_turns": TURNS,
        "sample_rate": RATE,
        "timing": {"wall_ms": 12.5, "audio_duration_ms": 6000.0, "rtf": 0.002},
    }


class FakeAudioCppServer:
    def __init__(self, models=("vibevoice-asr", "parakeet-tdt")):
        self.models = list(models)
        self.requests = []
        self._server = None
        self._thread = None

    @property
    def url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {"status": "ok", "backend": "cpu", "models": len(outer.models)})
                if self.path.startswith("/v1/models"):
                    return self._json(200, {"object": "list", "data": [
                        {"id": model, "object": "model", "configured": True} for model in outer.models]})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/v1/audio/transcriptions/details":
                    return self._json(404, {"error": "not found"})
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                head = f"Content-Type: {self.headers.get('Content-Type')}\r\n\r\n".encode()
                message = BytesParser(policy=default_policy).parsebytes(head + raw)
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = part.get_payload(decode=True)
                outer.requests.append(fields)
                model = (fields.get("model") or b"").decode()
                if model not in outer.models:
                    return self._json(400, {"error": {"message": f"unknown model '{model}'"}})
                return self._json(200, details_response())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
