"""Start Speakr from source the way the image runs it, for the browser smoke test.

    python tests/e2e/run_speakr.py --port 8899 --workdir /tmp/speakr-smoke

Settings come from smoke_env.py (config/env.whisperx.example plus a few
test changes). Like docker-entrypoint.sh, the database and the example's
admin account are created first, then two recordings with transcripts
(seed_recordings.py); gunicorn then runs in the foreground until it is
stopped. Run it in the background and wait for /login to answer.

    python tests/e2e/run_speakr.py --port 8899 --control-port 8898

With --control-port, gunicorn runs as a child process that a test can stop
and start again, so that the browser meets a server that is away, as when
the connection or the server is down (tests/e2e/test_offline_recorder_ui.py).
A small HTTP server on 127.0.0.1 at that port answers:

    POST /stop    gunicorn is stopped; its port no longer accepts connections
    POST /start   gunicorn is started again; answered once /login responds
    GET  /state   {"running": true|false}

The database and the session secret stay, so the browser's session is valid
again after a start.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from smoke_env import smoke_settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workdir", default="/tmp/speakr-smoke")
    parser.add_argument("--control-port", type=int, default=0,
                        help="run gunicorn as a child that POST /stop and /start on this port control")
    args = parser.parse_args()

    work = args.workdir
    for d in ("uploads", "exports", "auto-process", "instance"):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    env.update(smoke_settings())
    env.update({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{work}/instance/transcriptions.db",
        "UPLOAD_FOLDER": f"{work}/uploads",
        "AUTO_EXPORT_DIR": f"{work}/exports",
        "AUTO_PROCESS_WATCH_DIR": f"{work}/auto-process",
        "PYTHONUNBUFFERED": "1",
        # The browser tests all come from 127.0.0.1, so the per-IP limits
        # (1000 requests an hour, ten sign-ins a minute) are reached by the
        # suite itself and answer 429 to whichever test comes next. No browser
        # test relies on a real limit (test_share_window_ui fakes its 429 with
        # page.route); the limits are covered by the Python tests.
        "RATELIMIT_ENABLED": "false",
    })

    def run(cmd):
        subprocess.run(cmd, cwd=ROOT, env=env, check=True)

    run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"])
    run([sys.executable, "scripts/docker_create_admin.py"])
    # Recordings for the browser tests (no ASR service runs here)
    run([sys.executable, "tests/e2e/seed_recordings.py"])
    gunicorn = [sys.executable, "-m", "gunicorn", "--workers", "2", "--worker-class", "gthread",
                "--threads", "4", "--bind", f"{args.host}:{args.port}", "--timeout", "600",
                "--chdir", ROOT, "src.app:app"]
    if not args.control_port:
        os.execvpe(sys.executable, gunicorn, env)
    Controlled(gunicorn, env, f"http://{args.host}:{args.port}/login").serve(args.control_port)


class Controlled:
    """gunicorn as a child process, stopped and started on request."""

    def __init__(self, cmd, env, probe_url):
        self.cmd, self.env, self.probe_url = cmd, env, probe_url
        self.proc = None
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=120):
        with self.lock:
            if not self.running():
                # A process group of its own, so that the workers go with it.
                self.proc = subprocess.Popen(self.cmd, cwd=ROOT, env=self.env, start_new_session=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(self.probe_url, timeout=2) as response:
                        if response.status < 500:
                            return True
                except Exception:
                    pass
                if not self.running():
                    return False
                time.sleep(0.25)
            return False

    def stop(self, timeout=30):
        with self.lock:
            if not self.running():
                return True
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
            return True

    def serve(self, port):
        controlled = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/state":
                    self._answer(200, {"running": controlled.running()})
                else:
                    self._answer(404, {"error": "not found"})

            def do_POST(self):
                if self.path == "/stop":
                    self._answer(200, {"running": not controlled.stop()})
                elif self.path == "/start":
                    ok = controlled.start()
                    self._answer(200 if ok else 500, {"running": controlled.running(), "answering": ok})
                else:
                    self._answer(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        def shutdown(*_):
            controlled.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        self.start()
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
