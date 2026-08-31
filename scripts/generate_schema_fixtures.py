#!/usr/bin/env python3
"""Generate the historical schema fixtures used by tests/test_upgrade_path.py.

Each fixture is the real schema of a tagged release, dumped by checking that tag
out into a temporary worktree and letting its own models build a database. The
result is written as plain SQL so the tests need no old dependencies, no network
and no Docker, and cannot drift as this script changes.

This is a maintenance tool, not part of the test run. You only need it when
adding a version to cover, and the existing fixtures should not be regenerated
casually: they are a record of what those releases actually produced.

    python scripts/generate_schema_fixtures.py                  # default tags
    python scripts/generate_schema_fixtures.py v0.9.0-alpha ... # specific tags

Old releases need packages the current environment may not have. Anything
missing is installed into a throwaway directory rather than your virtualenv.
Heavy optional dependencies that only need to be importable are stubbed.
"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, 'tests', 'fixtures', 'schemas')
PYTHON = sys.executable

DEFAULT_TAGS = [
    'v0.5.8-alpha',    # pre can_share_publicly; the shape that wedged #379
    'v0.7.0-alpha',    # SSO arrives, and with it the password-nullable migration
    'v0.8.21-alpha',
    'v0.9.7-alpha',
    'v0.10.3-alpha',   # the release before the migration was rewritten
]

# import name -> pip package, where they differ
PACKAGES = {
    'jwt': 'PyJWT', 'dateutil': 'python-dateutil', 'dotenv': 'python-dotenv',
    'yaml': 'PyYAML', 'PIL': 'Pillow', 'magic': 'python-magic',
    'sqlalchemy': 'SQLAlchemy', 'bs4': 'beautifulsoup4', 'jose': 'python-jose',
    'Crypto': 'pycryptodome', 'pkg_resources': 'setuptools',
}

# Only needs to import; never called during schema creation. Installing the real
# thing would pull in the whole scientific stack for nothing.
STUBS = {
    'sklearn/__init__.py': '',
    'sklearn/metrics/__init__.py': '',
    'sklearn/metrics/pairwise.py':
        'def cosine_similarity(*a, **k):\n'
        '    raise NotImplementedError("stub for schema generation")\n',
}

BUILD_DB = "from src.app import app, db\nwith app.app_context():\n    db.create_all()\nprint('OK')\n"


def write_stubs(deps_dir):
    for path, body in STUBS.items():
        full = os.path.join(deps_dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as fh:
            fh.write(body)


def generate(tag, deps_dir):
    worktree = tempfile.mkdtemp(prefix=f'speakr_{tag}_')
    data_dir = tempfile.mkdtemp(prefix='speakr_schemagen_')
    db_path = os.path.join(data_dir, 'schema.db')

    subprocess.run(['git', '-C', REPO, 'worktree', 'add', '-q', '--detach', worktree, tag],
                   check=True)
    env = dict(
        os.environ,
        SQLALCHEMY_DATABASE_URI='sqlite:///' + db_path,
        UPLOAD_FOLDER=os.path.join(data_dir, 'uploads'),
        SECRET_KEY='x', TEXT_MODEL_API_KEY='x', TRANSCRIPTION_API_KEY='x', OPENAI_API_KEY='x',
        TRANSCRIPTION_BASE_URL='https://api.openai.com/v1',
        TEXT_MODEL_BASE_URL='https://api.openai.com/v1',
        USE_ASR_ENDPOINT='false', ENABLE_INQUIRE_MODE='false',
        ENABLE_AUTO_PROCESSING='false', JOB_QUEUE_WORKERS='0', SUMMARY_QUEUE_WORKERS='0',
        PYTHONPATH=deps_dir + os.pathsep + worktree, PYTHONDONTWRITEBYTECODE='1',
    )

    try:
        for _ in range(25):
            result = subprocess.run([PYTHON, '-c', BUILD_DB], cwd=worktree, env=env,
                                    capture_output=True, text=True, timeout=300)
            if 'OK' in result.stdout:
                break
            missing = re.search(r"No module named '([\w.]+)'", result.stderr)
            if not missing:
                print(f'  {tag}: FAILED\n{result.stderr[-2000:]}')
                return False
            module = missing.group(1).split('.')[0]
            package = PACKAGES.get(module, module)
            print(f'    installing {package}')
            subprocess.run([PYTHON, '-m', 'pip', 'install', '-q', '--target', deps_dir, package],
                           capture_output=True)
        else:
            print(f'  {tag}: gave up resolving imports')
            return False

        con = sqlite3.connect(db_path)
        objects = con.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name").fetchall()
        user_columns = [row[1] for row in con.execute('PRAGMA table_info(user)')]
        con.close()

        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, tag + '.sql')
        with open(path, 'w') as fh:
            fh.write(f"-- Schema of Speakr {tag}, generated from that tag's own models.\n"
                     f"-- Regenerate with scripts/generate_schema_fixtures.py. Do not hand-edit.\n"
                     f"-- user table: {len(user_columns)} columns\n\n")
            fh.write(';\n\n'.join(row[0].strip() for row in objects) + ';\n')

        print(f'  {tag}: {len(objects)} objects, user has {len(user_columns)} columns')
        return True
    finally:
        subprocess.run(['git', '-C', REPO, 'worktree', 'remove', '--force', worktree],
                       capture_output=True)
        shutil.rmtree(data_dir, ignore_errors=True)


def main():
    tags = sys.argv[1:] or DEFAULT_TAGS
    deps_dir = tempfile.mkdtemp(prefix='speakr_olddeps_')
    write_stubs(deps_dir)
    print(f'resolving old dependencies into {deps_dir}')

    failed = []
    try:
        for tag in tags:
            print(tag)
            if not generate(tag, deps_dir):
                failed.append(tag)
    finally:
        shutil.rmtree(deps_dir, ignore_errors=True)

    if failed:
        print(f'\nfailed: {", ".join(failed)}')
        return 1
    print(f'\nwrote {len(tags)} fixture(s) to {OUT_DIR}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
