"""Read-only verification of a completed single-note production migration."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publication import owned_note


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(root, backup):
    root, backup = Path(root), Path(backup)
    journal = json.loads((backup / 'journal.json').read_text(encoding='utf-8'))
    assert journal['state'] == 'done'
    with sqlite3.connect(root / 'state.sqlite3') as live, sqlite3.connect(backup / 'state.sqlite3') as before:
        assert live.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        restored = 0
        updated_since_migration = 0
        preserved_user_sections = 0
        for item in journal['items']:
            assert item['stage'] == 'done'
            jid = item['job_id']
            pubs = live.execute('SELECT version,path,content_hash,state FROM publications WHERE job_id=?', (jid,)).fetchall()
            assert len(pubs) == 1, jid
            version, path, content_hash, state = pubs[0]
            assert path == item['canonical'] and state == 'published', jid
            assert digest(path) == content_hash, jid
            _, current_user_section = owned_note(path, jid)
            updated_since_migration += version != item['latest_version']
            original_user_sections = []
            for old in item['old']:
                assert digest(backup / old['backup']) == old['hash'], old['path']
                _, old_user_section = owned_note(backup / old['backup'], jid)
                if old_user_section.removeprefix('## Мои заметки').strip():
                    original_user_sections.append(old_user_section)
                if old['path'] != item['canonical']:
                    assert not Path(old['path']).exists(), old['path']
            if original_user_sections:
                assert all(section == original_user_sections[0] for section in original_user_sections), jid
                assert current_user_section == original_user_sections[0], jid
                preserved_user_sections += 1
            prior = before.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()
            current = live.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()
            if prior and prior[0] == 'missing_output':
                if current and current[0] in ('completed', 'summary_pending'):
                    restored += 1
        skipped = {item['job_id'] for item in journal['skipped']}
        for jid in skipped:
            old = before.execute('SELECT version,path,content_hash,state FROM publications WHERE job_id=? ORDER BY version', (jid,)).fetchall()
            now = live.execute('SELECT version,path,content_hash,state FROM publications WHERE job_id=? ORDER BY version', (jid,)).fetchall()
            assert old == now, jid
        multiple = {jid for jid, n in live.execute('SELECT job_id,COUNT(*) FROM publications GROUP BY job_id HAVING COUNT(*)>1')}
        assert multiple <= skipped
    return {'migrated': len(journal['items']), 'skipped_unchanged': len(skipped),
            'jobs_updated_since_migration': updated_since_migration,
            'preserved_user_sections_with_content': preserved_user_sections,
            'restored_missing_output_now_active': restored, 'remaining_multi_row_jobs': len(multiple),
            'sqlite_integrity': 'ok'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.backup), ensure_ascii=False, indent=2))
