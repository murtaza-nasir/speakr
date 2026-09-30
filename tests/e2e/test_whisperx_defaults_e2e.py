"""End to end: Speakr and whisperx-asr-service as a user sets them up.

Both services run as real processes with their documented configuration and
nothing else:

- whisperx-asr-service: its docker-compose.yml plus the .env block from its
  SETUP_GUIDE (tests/defaults/minimal.env), on stand-in ML packages so no GPU
  or model download is needed. It is started outside this test (CI job, or
  by hand, see tests/e2e/README.md) and reached at WHISPERX_URL.
- Speakr: config/env.whisperx.example, pointed at WHISPERX_URL, run by
  gunicorn exactly as the image runs it, with a fresh SQLite database and
  the example's admin account created the way docker-entrypoint.sh does. The
  example's language model is replaced by a local stand-in, since titles and
  summaries are outside what this test covers.

The test then checks what a new user would see: the startup voice embedding
check succeeds, an upload is transcribed and diarized with speaker
embeddings stored, two files joined at upload become one transcribed
recording, and neither service logged an error.

Runs only with SPEAKR_E2E=1 (the CI job sets it); the normal suite skips it.
"""

import io
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import wave

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tests.e2e.fake_llm_server import FakeLLMServer

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_E2E") != "1",
                                reason="end-to-end test; set SPEAKR_E2E=1 with a whisperx stub server running")

WHISPERX_URL = os.environ.get("WHISPERX_URL", "http://127.0.0.1:9000").rstrip("/")
WHISPERX_LOG = os.environ.get("WHISPERX_LOG", "")
EXAMPLE = os.path.join(ROOT, "config", "env.whisperx.example")
ERROR_LINE = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)")


def read_example():
    values = {}
    with open(EXAMPLE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.split(" #")[0].strip().strip('"').strip("'")
    return values


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wav(seconds, freq):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(16000 * seconds)))
    return buf.getvalue()


@pytest.fixture(scope="module")
def speakr():
    """Speakr as the WhisperX example configures it, running under gunicorn."""
    try:
        httpx.get(f"{WHISPERX_URL}/health", timeout=5).raise_for_status()
    except Exception as e:
        pytest.fail(f"whisperx stub server not reachable at {WHISPERX_URL}: {e}")

    work = tempfile.mkdtemp(prefix="speakr-e2e-")
    port = _free_port()
    with FakeLLMServer() as llm:
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
        env.update(read_example())
        env.update({
            # Where the example's container paths live on this machine.
            "ASR_BASE_URL": WHISPERX_URL,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{work}/transcriptions.db",
            "UPLOAD_FOLDER": f"{work}/uploads",
            "AUTO_EXPORT_DIR": f"{work}/exports",
            "AUTO_PROCESS_WATCH_DIR": f"{work}/auto-process",
            "SECRET_KEY": "e2e-" + uuid.uuid4().hex,
            # The example's language model, replaced (see the module docstring).
            "TEXT_MODEL_BASE_URL": llm.url,
            "TEXT_MODEL_API_KEY": "e2e",
            "PYTHONUNBUFFERED": "1",
            # The example's admin address is rejected by the domain check (see
            # test_the_example_admin_account_is_created); skip the check so the
            # rest of the documented setup can be tested.
            "SKIP_EMAIL_DOMAIN_CHECK": "true",
        })
        for d in ("uploads", "exports", "auto-process"):
            os.makedirs(os.path.join(work, d), exist_ok=True)

        def run(args):
            done = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            if done.returncode != 0:
                pytest.fail(f"{' '.join(args)} failed:\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}")

        # docker-entrypoint.sh: create the database, then the example's admin.
        run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"])
        run([sys.executable, "scripts/docker_create_admin.py"])

        log_path = os.path.join(work, "speakr.log")
        log = open(log_path, "w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "--workers", "1", "--worker-class", "gthread", "--threads", "4",
             "--bind", f"127.0.0.1:{port}", "--timeout", "600", "src.app:app"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(120):
                try:
                    if httpx.get(f"{base}/login", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if proc.poll() is not None:
                    break
                time.sleep(1)
            else:
                pytest.fail("Speakr did not start:\n" + open(log_path).read()[-4000:])
            if proc.poll() is not None:
                pytest.fail("Speakr exited:\n" + open(log_path).read()[-4000:])
            yield {"base": base, "log": log_path, "db": f"{work}/transcriptions.db", "example": env}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            if os.environ.get("KEEP_E2E_LOGS"):
                print(f"Speakr log kept at {log_path}")
            else:
                shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def admin(speakr):
    """A logged-in session as the example's admin account."""
    client = httpx.Client(base_url=speakr["base"], timeout=60, follow_redirects=True)
    page = client.get("/login").text
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page) or re.search(
        r'value="([^"]+)"[^>]*name="csrf_token"', page)
    r = client.post("/login", data={"email": speakr["example"]["ADMIN_EMAIL"],
                                    "password": speakr["example"]["ADMIN_PASSWORD"],
                                    "csrf_token": token.group(1)})
    assert r.status_code == 200 and "/login" not in str(r.url), "could not log in as the example's admin"
    meta = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text)
    client.headers["X-CSRFToken"] = meta.group(1)
    yield client
    client.close()


def _wait_for(check, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    pytest.fail(f"timed out waiting for {what}")


def _wait_until_done(client, recording_id):
    def done():
        status = client.get(f"/recording/{recording_id}/status").json().get("status")
        return status if status in ("COMPLETED", "FAILED") else None
    return _wait_for(done, 180, f"recording {recording_id}")


def _row(speakr, recording_id):
    con = sqlite3.connect(speakr["db"])
    try:
        con.row_factory = sqlite3.Row
        return con.execute("SELECT status, transcription, speaker_embeddings, error_message FROM recording"
                           " WHERE id = ?", (recording_id,)).fetchone()
    finally:
        con.close()


def test_the_startup_voice_embedding_check_succeeds(speakr, admin):
    def verdict():
        status = admin.get("/admin/voice-embeddings/status").json()
        return status if status.get("status") in ("ok", "changed") else None
    status = _wait_for(verdict, 90, "the startup voice embedding check")
    assert status["status"] == "ok", status


def test_an_upload_is_transcribed_with_speakers_and_embeddings(speakr, admin):
    r = admin.post("/upload", files={"file": ("meeting.wav", _wav(12, 330), "audio/wav")})
    assert r.status_code == 202, r.text
    recording_id = r.json()["id"]
    assert _wait_until_done(admin, recording_id) == "COMPLETED", dict(_row(speakr, recording_id))
    row = _row(speakr, recording_id)
    assert "transcribed by large-v3" in row["transcription"], row["transcription"][:500]
    embeddings = json.loads(row["speaker_embeddings"] or "{}")
    assert embeddings and all(len(v) == 256 for v in embeddings.values()), "no speaker embeddings stored"


def test_two_files_joined_at_upload_become_one_recording(speakr, admin):
    group, joined = uuid.uuid4().hex, None
    for index in range(2):
        r = admin.post("/upload", data={"join_group": group, "join_index": str(index), "join_count": "2"},
                       files={"file": (f"part{index}.wav", _wav(6, 300 + 150 * index), "audio/wav")})
        assert r.status_code == 202, r.text
        joined = r.json().get("id") or joined
    assert joined, "the join produced no recording"
    assert _wait_until_done(admin, joined) == "COMPLETED", dict(_row(speakr, joined))
    assert "transcribed by large-v3" in _row(speakr, joined)["transcription"]


def test_neither_service_logged_an_error(speakr, admin):
    """Runs last in this module, after everything above has used both services."""
    with open(speakr["log"], encoding="utf-8", errors="replace") as f:
        speakr_errors = [line.rstrip() for line in f if ERROR_LINE.search(line)]
    assert not speakr_errors, "Speakr logged errors:\n" + "\n".join(speakr_errors[:40])
    if WHISPERX_LOG and os.path.exists(WHISPERX_LOG):
        with open(WHISPERX_LOG, encoding="utf-8", errors="replace") as f:
            service_errors = [line.rstrip() for line in f if ERROR_LINE.search(line)]
        assert not service_errors, "whisperx-asr-service logged errors:\n" + "\n".join(service_errors[:40])


@pytest.mark.xfail(strict=True, reason=(
    "config/env.whisperx.example sets ADMIN_EMAIL=admin@example.com; example.com publishes a null MX, so "
    "scripts/docker_create_admin.py rejects it and docker-entrypoint.sh (set -e) stops. Remove this marker "
    "once the example uses an address that passes, or sets SKIP_EMAIL_DOMAIN_CHECK."))
def test_the_example_admin_account_is_created():
    """The example's admin settings, exactly as a user copies them."""
    work = tempfile.mkdtemp(prefix="speakr-e2e-admin-")
    try:
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        env.update(read_example())
        env.update({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{work}/t.db", "UPLOAD_FOLDER": f"{work}/uploads",
                    "AUTO_EXPORT_DIR": f"{work}/exports", "AUTO_PROCESS_WATCH_DIR": f"{work}/auto",
                    "SECRET_KEY": "e2e", "DISABLE_VOICE_EMBEDDING_CHECK": "true",
                    "ASR_BASE_URL": WHISPERX_URL})
        subprocess.run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"],
                       cwd=ROOT, env=env, check=True, capture_output=True, timeout=300)
        done = subprocess.run([sys.executable, "scripts/docker_create_admin.py"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout[-1000:]
    finally:
        shutil.rmtree(work, ignore_errors=True)
