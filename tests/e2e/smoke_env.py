"""Settings for the browser smoke test: config/env.whisperx.example plus the
few changes a page-load test needs.

Used two ways:
- ``python tests/e2e/smoke_env.py > smoke.env`` writes a file for
  ``docker run --env-file`` (the image's entrypoint then creates the database
  and the example's admin account);
- ``run_speakr.py`` imports ``smoke_settings()`` to start Speakr from source.

Changes to the example, and why:
- DISABLE_VOICE_EMBEDDING_CHECK=true: no ASR service runs in this test, and
  the check would only log a warning about it.
- ENABLE_INQUIRE_MODE=true: so the Inquire page is loaded as well.
- SECRET_KEY: a fixed test value.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE = os.path.join(ROOT, "config", "env.whisperx.example")

OVERRIDES = {
    "DISABLE_VOICE_EMBEDDING_CHECK": "true",
    "ENABLE_INQUIRE_MODE": "true",
    "SECRET_KEY": "browser-smoke-test",
}


def read_example(path=EXAMPLE):
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.split(" #")[0].strip().strip('"').strip("'")
    return values


def smoke_settings():
    values = read_example()
    values.update(OVERRIDES)
    return values


if __name__ == "__main__":
    for key, value in smoke_settings().items():
        sys.stdout.write(f"{key}={value}\n")
