"""Exercise single-note migration on a disposable copy of installed SQLite/notes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from single_note_migration import apply, preview


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(root):
    root = Path(root)
    cfg = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    live_vault = Path(cfg['vault']).resolve()
    with tempfile.TemporaryDirectory(prefix='speakr-single-note-snapshot-') as temp:
        temp = Path(temp)
        state = temp / 'automation'
        state.mkdir()
        vault = temp / 'vault'
        vault.mkdir()
        source = sqlite3.connect((root / 'state.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
        copied = sqlite3.connect(state / 'state.sqlite3')
        try:
            source.backup(copied)
        finally:
            source.close()
        copied.row_factory = sqlite3.Row
        live_hashes = {}
        for row in copied.execute('SELECT job_id,version,path FROM publications').fetchall():
            original = Path(row['path'])
            relative = original.relative_to(live_vault)
            target = vault / relative
            if original.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
                live_hashes[str(original)] = hash_file(original)
            copied.execute('UPDATE publications SET path=? WHERE job_id=? AND version=?',
                           (str(target), row['job_id'], row['version']))
        copied.commit()
        copied.close()
        (state / 'maintenance.json').write_text(json.dumps({'paused': True}), encoding='utf-8')
        before = preview(state, vault)
        result = apply(state, vault)
        after = preview(state, vault)
        unchanged = all(Path(path).is_file() and hash_file(Path(path)) == digest
                        for path, digest in live_hashes.items())
        if not unchanged:
            raise AssertionError('Installed note changed during snapshot validation')
        if result['migrated'] != before['eligible_count'] or after['eligible_count'] != 0:
            raise AssertionError('Migration did not consolidate every eligible snapshot job')
        if result['sqlite_integrity'] != 'ok':
            raise AssertionError('Snapshot SQLite integrity failed')
        return {'before_eligible': before['eligible_count'], 'before_skipped': before['skipped_count'],
                'after_eligible': after['eligible_count'], 'after_skipped': after['skipped_count'],
                'migrated': result['migrated'], 'sqlite_integrity': result['sqlite_integrity'],
                'live_files_unchanged_during_check': unchanged}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root), ensure_ascii=False, indent=2))
