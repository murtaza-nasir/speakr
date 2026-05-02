#!/usr/bin/env python3
"""
Regression test for the v0.8.16-alpha embedding-identifier migration.

Pre-v0.8.16 instances stored only the model name under the legacy
`embedding_model_name` SystemSetting key. The new format is
`{provider}::{model}` under `embedding_identifier`. The compatibility check
in src/init_db.py wraps any legacy value as `local::<value>` before comparing,
so a default upgrade does not produce a false-positive "embeddings changed"
warning.

This test reproduces three upgrade scenarios:

  1. Fresh install (no legacy, no new key)        -> first-run record only.
  2. v0.8.15 upgrade with same default config      -> silent migration, no warn.
  3. v0.8.15 upgrade after legitimate config change -> warning fires.

Run with: docker exec speakr-dev python /app/tests/test_embedding_identifier_compat.py
"""

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


PASSED = 0
FAILED = 0


def run(name, func):
    global PASSED, FAILED
    try:
        func()
        print(f"  ok {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  FAIL {name}: {e}")
        FAILED += 1
    except Exception as e:
        print(f"  FAIL {name}: EXCEPTION - {e}")
        FAILED += 1


def reset_settings(stored_identifier=None, legacy_model_name=None):
    from src.app import app, db
    from src.models import SystemSetting
    with app.app_context():
        for k in ('embedding_identifier', 'embedding_model_name'):
            row = SystemSetting.query.filter_by(key=k).first()
            if row:
                db.session.delete(row)
        db.session.commit()
        if stored_identifier:
            SystemSetting.set_setting(key='embedding_identifier', value=stored_identifier, setting_type='string')
        if legacy_model_name:
            SystemSetting.set_setting(key='embedding_model_name', value=legacy_model_name, setting_type='string')


def reload_embeddings_with(**env):
    saved = {k: os.environ.get(k) for k in ('EMBEDDING_BASE_URL', 'EMBEDDING_API_KEY', 'EMBEDDING_MODEL', 'EMBEDDING_DIMENSIONS')}
    for k in saved:
        os.environ.pop(k, None)
    for k, v in env.items():
        if v is not None:
            os.environ[k] = v
    import src.services.embeddings as emb
    importlib.reload(emb)
    return emb, saved


def restore_env(saved):
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v
    import src.services.embeddings as emb
    importlib.reload(emb)


def run_compat_check():
    """Replays the inline migration block from src/init_db.py."""
    from src.app import app
    from src.models import SystemSetting, TranscriptChunk
    from src.services.embeddings import EMBEDDING_IDENTIFIER

    with app.app_context():
        current = EMBEDDING_IDENTIFIER
        stored = SystemSetting.get_setting('embedding_identifier', None)
        legacy = SystemSetting.get_setting('embedding_model_name', None)
        migrated_from_legacy = False
        if stored is None and legacy:
            stored = f"local::{legacy}"
            migrated_from_legacy = True

        if stored is None:
            outcome = 'first-run'
        elif stored == current:
            outcome = 'silent-migration' if migrated_from_legacy else 'no-change'
        else:
            outcome = 'warn-mismatch'

        return {
            'stored': stored,
            'current': current,
            'migrated_from_legacy': migrated_from_legacy,
            'outcome': outcome,
        }


def test_fresh_install_first_run():
    _, saved = reload_embeddings_with(EMBEDDING_MODEL='all-MiniLM-L6-v2')
    try:
        reset_settings()
        result = run_compat_check()
        assert result['outcome'] == 'first-run', f"expected first-run, got {result}"
    finally:
        restore_env(saved)


def test_legacy_default_silent_upgrade():
    _, saved = reload_embeddings_with(EMBEDDING_MODEL='all-MiniLM-L6-v2')
    try:
        reset_settings(legacy_model_name='all-MiniLM-L6-v2')
        result = run_compat_check()
        assert result['outcome'] == 'silent-migration', f"expected silent-migration, got {result}"
        assert result['stored'] == 'local::all-MiniLM-L6-v2'
        assert result['current'] == 'local::all-MiniLM-L6-v2'
    finally:
        restore_env(saved)


def test_legacy_then_real_change_warns():
    _, saved = reload_embeddings_with(EMBEDDING_MODEL='all-mpnet-base-v2')
    try:
        reset_settings(legacy_model_name='all-MiniLM-L6-v2')
        result = run_compat_check()
        assert result['outcome'] == 'warn-mismatch', f"expected warn-mismatch, got {result}"
        assert result['stored'] == 'local::all-MiniLM-L6-v2'
        assert result['current'] == 'local::all-mpnet-base-v2'
    finally:
        restore_env(saved)


def test_legacy_then_switch_to_api_warns():
    _, saved = reload_embeddings_with(
        EMBEDDING_BASE_URL='https://api.openai.com/v1',
        EMBEDDING_MODEL='text-embedding-3-small',
    )
    try:
        reset_settings(legacy_model_name='all-MiniLM-L6-v2')
        result = run_compat_check()
        assert result['outcome'] == 'warn-mismatch', f"expected warn-mismatch, got {result}"
        assert result['stored'] == 'local::all-MiniLM-L6-v2'
        assert result['current'] == 'https://api.openai.com/v1::text-embedding-3-small'
    finally:
        restore_env(saved)


def test_already_migrated_no_change():
    _, saved = reload_embeddings_with(EMBEDDING_MODEL='all-MiniLM-L6-v2')
    try:
        reset_settings(stored_identifier='local::all-MiniLM-L6-v2')
        result = run_compat_check()
        assert result['outcome'] == 'no-change', f"expected no-change, got {result}"
    finally:
        restore_env(saved)


def restore_dev_state():
    from src.app import app, db
    from src.models import SystemSetting
    with app.app_context():
        legacy = SystemSetting.query.filter_by(key='embedding_model_name').first()
        if legacy:
            db.session.delete(legacy)
        from src.services.embeddings import EMBEDDING_IDENTIFIER
        SystemSetting.set_setting(
            key='embedding_identifier',
            value=EMBEDDING_IDENTIFIER,
            description='Identifier of the embedding provider and model that produced the stored chunk vectors. Used to detect dimensionality and semantic-space mismatches at startup.',
            setting_type='string',
        )


def main():
    print("=== embedding identifier upgrade compatibility ===\n")
    run("fresh install records identifier on first run", test_fresh_install_first_run)
    run("default upgrade migrates legacy key silently (no warning)", test_legacy_default_silent_upgrade)
    run("real model change after upgrade still warns", test_legacy_then_real_change_warns)
    run("switch from local to API after upgrade warns", test_legacy_then_switch_to_api_warns)
    run("already-migrated identifier reports no-change", test_already_migrated_no_change)

    restore_dev_state()

    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
