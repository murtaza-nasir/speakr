"""The admin account from the environment is created with the example settings.

Every example configuration sets ADMIN_EMAIL=admin@example.com. example.com
publishes a null MX record, so a deliverability check rejected the address and
the container stopped at startup. The admin address is now checked for format
only; self-registration keeps the full check.
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(email):
    work = tempfile.mkdtemp(prefix="speakr-admin-")
    try:
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        env.update({
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{work}/t.db",
            "UPLOAD_FOLDER": f"{work}/uploads",
            "SECRET_KEY": "admin-test",
            "TEXT_MODEL_API_KEY": "test-key",
            "TRANSCRIPTION_API_KEY": "test-key",
            "TRANSCRIPTION_BASE_URL": "https://api.openai.com/v1",
            "DISABLE_VOICE_EMBEDDING_CHECK": "true",
            "ENABLE_AUTO_PROCESSING": "false",
            "ADMIN_USERNAME": "admin",
            "ADMIN_EMAIL": email,
            "ADMIN_PASSWORD": "changeme-please",
        })
        subprocess.run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"],
                       cwd=ROOT, env=env, check=True, capture_output=True, timeout=300)
        return subprocess.run([sys.executable, "scripts/docker_create_admin.py"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=300)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_example_admin_address_is_accepted():
    done = _run("admin@example.com")
    assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]


def test_a_malformed_admin_address_is_still_rejected():
    done = _run("not-an-address")
    assert done.returncode == 1
    assert "Invalid email" in done.stdout
