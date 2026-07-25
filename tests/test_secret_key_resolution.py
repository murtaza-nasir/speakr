"""Unit tests for SECRET_KEY resolution (security hardening).

The app used to fall back to a hardcoded constant when SECRET_KEY was unset.
That constant signs session cookies and password-reset tokens, so a known
value lets anyone forge an admin session. resolve_secret_key refuses the
default, uses a real env key as-is, and otherwise generates+persists a strong
per-deployment key.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.security import resolve_secret_key, INSECURE_SECRET_KEY


def test_real_env_key_is_used_verbatim():
    key, action = resolve_secret_key('a-real-strong-key', 'sqlite:////tmp/x.db')
    assert key == 'a-real-strong-key'
    assert action == 'env'


def test_insecure_default_is_refused():
    with pytest.raises(ValueError) as exc:
        resolve_secret_key(INSECURE_SECRET_KEY, 'sqlite:////tmp/x.db')
    assert 'insecure' in str(exc.value).lower()


def test_unset_generates_and_persists_key(tmp_path):
    key_file = str(tmp_path / 'secret_key')
    key, action = resolve_secret_key('', 'sqlite:////ignored.db', key_file=key_file)
    assert action == 'generated'
    assert len(key) >= 32
    # Persisted with the exact value and owner-only permissions.
    assert os.path.exists(key_file)
    with open(key_file) as fh:
        assert fh.read().strip() == key
    assert (os.stat(key_file).st_mode & 0o777) == 0o600


def test_second_call_reads_the_persisted_key(tmp_path):
    key_file = str(tmp_path / 'secret_key')
    first, a1 = resolve_secret_key(None, 'sqlite:///x.db', key_file=key_file)
    second, a2 = resolve_secret_key(None, 'sqlite:///x.db', key_file=key_file)
    assert a1 == 'generated'
    assert a2 == 'persisted'
    assert first == second  # stable across restarts


def test_none_env_var_is_treated_as_unset(tmp_path):
    key, action = resolve_secret_key(None, 'sqlite:///x.db',
                                     key_file=str(tmp_path / 'k'))
    assert action == 'generated'
    assert key


def test_key_file_derived_from_sqlite_instance_dir(tmp_path):
    # No explicit key_file: it should land next to the sqlite database.
    db = tmp_path / 'instance' / 'transcriptions.db'
    db.parent.mkdir(parents=True)
    key, action = resolve_secret_key('', f'sqlite:////{db}')
    assert action == 'generated'
    assert (tmp_path / 'instance' / 'secret_key').exists()


def test_ephemeral_when_persistence_fails(tmp_path):
    # key_file points into a path that cannot be created (a file as a dir).
    blocker = tmp_path / 'blocker'
    blocker.write_text('x')
    key, action = resolve_secret_key('', 'sqlite:///x.db',
                                     key_file=str(blocker / 'sub' / 'secret_key'))
    assert action == 'ephemeral'
    assert key  # still returns a usable key, never the insecure default
    assert key != INSECURE_SECRET_KEY


def test_generated_keys_are_unique():
    seen = set()
    for _ in range(5):
        with tempfile.TemporaryDirectory() as d:
            key, _ = resolve_secret_key('', 'sqlite:///x.db',
                                        key_file=os.path.join(d, 'k'))
            seen.add(key)
    assert len(seen) == 5
