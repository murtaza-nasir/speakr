"""A minimal OpenAI-compatible chat endpoint for the end-to-end test.

The WhisperX example configures a language model for titles and summaries.
The end-to-end test is about the transcription path, so the model is
replaced by this server, which answers every chat completion with a fixed
title or summary (or an empty JSON object when JSON is requested). That keeps
Speakr's log free of errors that have nothing to do with the ASR service.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLMServer:
    def __init__(self):
        self.requests = 0
        self._server = None
        self._thread = None

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._send(200, {"object": "list", "data": [{"id": "test-model", "object": "model"}]})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
                fake.requests += 1
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, {"error": "not found"})
                wants_json = bool(request.get("response_format"))
                content = "{}" if wants_json else "Test meeting summary."
                if request.get("stream"):
                    chunk = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": "test-model",
                             "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}
                    end = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
                    body = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode()
                    return self._send(200, body, "text/event-stream")
                return self._send(200, {
                    "id": "c1", "object": "chat.completion", "created": int(time.time()), "model": "test-model",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                })

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"
