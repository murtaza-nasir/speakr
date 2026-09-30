"""Start Speakr from source the way the image runs it, for the browser smoke test.

    python tests/e2e/run_speakr.py --port 8899 --workdir /tmp/speakr-smoke

Settings come from smoke_env.py (config/env.whisperx.example plus a few
test changes). Like docker-entrypoint.sh, the database and the example's
admin account are created first; gunicorn then runs in the foreground until
it is stopped. Run it in the background and wait for /login to answer.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from smoke_env import smoke_settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--workdir", default="/tmp/speakr-smoke")
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
    })

    def run(cmd):
        subprocess.run(cmd, cwd=ROOT, env=env, check=True)

    run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"])
    run([sys.executable, "scripts/docker_create_admin.py"])
    os.execvpe(sys.executable, [sys.executable, "-m", "gunicorn", "--workers", "2", "--worker-class", "gthread",
                                "--threads", "4", "--bind", f"{args.host}:{args.port}", "--timeout", "600",
                                "--chdir", ROOT, "src.app:app"], env)


if __name__ == "__main__":
    main()
