"""Pytest configuration for the Speakr test suite.

This file is imported by pytest BEFORE it collects any test module, so the
environment it sets here is in place before any `from src.app import app`
binds the Flask app to a database.

Goal: isolation. Point the app at a throwaway temporary SQLite database so
the test suite never reads or mutates the developer's real
`instance/transcriptions.db`. Running the suite with `pytest` therefore
needs no services and is safe on any machine / in CI.

Note: the legacy standalone invocation (`python tests/test_foo.py`) does NOT
load conftest.py, so this changes nothing for that workflow — it only affects
`pytest` runs.
"""

import os
import tempfile

# --- Isolated database (set BEFORE src.app is imported) ---------------------
# Use a real file (not :memory:) because the app and tests open multiple
# connections / app contexts, which an in-memory SQLite DB would not share.
_TEST_DIR = tempfile.mkdtemp(prefix="speakr_pytest_")
_TEST_DB_PATH = os.path.join(_TEST_DIR, "test.db")
os.environ["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_TEST_DB_PATH}"

# The app os.makedirs(UPLOAD_FOLDER) at import time; default is /data/uploads
# which isn't writable in CI / outside Docker. Point it at the temp dir.
os.environ["UPLOAD_FOLDER"] = os.path.join(_TEST_DIR, "uploads")

# --- Quiet, deterministic defaults for tests --------------------------------
# Only set if the caller hasn't already chosen a value.
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")   # no black-hole file monitor
os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")    # avoid config hard-fails
# initialize_config() hard-exits at import if no transcription service is set.
# The app loads .env via load_dotenv(), so a developer with a populated .env
# passes, but a clean checkout / CI has none. Supply harmless defaults so the
# suite is self-contained (mirrors TEXT_MODEL_API_KEY above); tests exercising
# real transcription behaviour override these.
os.environ.setdefault("TRANSCRIPTION_API_KEY", "test-key")
os.environ.setdefault("TRANSCRIPTION_BASE_URL", "https://api.openai.com/v1")

# Run ZERO background job-queue workers during tests. These counts are read at
# job_queue import time, so they must be set before src.app is imported. With
# workers running, enqueue() auto-starts daemon threads that claim and process
# test-created jobs in the background — racing tests that exercise the queue
# directly (e.g. test_job_queue_race_condition) and spraying FileNotFound
# errors as they try to transcribe nonexistent test files. Tests that need to
# exercise claiming drive _claim_next_job themselves.
os.environ["JOB_QUEUE_WORKERS"] = "0"
os.environ["SUMMARY_QUEUE_WORKERS"] = "0"
# NB: do NOT force WEBHOOK_GLOBAL_ENABLED=false here — the webhook suite needs
# delivery enabled (it mocks the actual HTTP POST), so leave the default (true).

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _initialize_test_database():
    """Create the full schema in the throwaway DB once per session."""
    from src.app import app
    from src.init_db import initialize_database

    with app.app_context():
        initialize_database(app)

    yield

    # Best-effort cleanup of the temp DB file after the session.
    for path in (_TEST_DB_PATH, _TEST_DB_PATH + "-wal", _TEST_DB_PATH + "-shm"):
        try:
            os.remove(path)
        except OSError:
            pass
