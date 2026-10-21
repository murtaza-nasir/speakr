"""Use the SQLite backup API, never a copy of WAL files."""
import argparse
from pathlib import Path
import sqlite3
import json

p = argparse.ArgumentParser()
p.add_argument('--root', required=True)
p.add_argument('--destination')
p.add_argument('--list-failed', action='store_true')
a = p.parse_args()
source = Path(a.root) / 'state.sqlite3'
if source.exists():
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as db:
        if a.list_failed:
            print(json.dumps([r[0] for r in db.execute("SELECT id FROM jobs WHERE state='failed'")]))
        else:
            if not a.destination:
                p.error('--destination is required for backup')
            with sqlite3.connect(a.destination) as dest:
                db.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
elif a.list_failed:
    print('[]')
