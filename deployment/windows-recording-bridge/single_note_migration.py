"""Preview and consolidate owned legacy note versions while the bridge is paused."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from bridge import WorkerLock
from common import atomic_json, maintenance_paused
from publication import owned_note, write_new, write_replace


VERSION_SUFFIX = re.compile(r' — версия [0-9a-f]{10}(?=\.md$)')
PREVIOUS_LINK = re.compile(r'(?m)^- \[Предыдущая версия\]\(<[^>]+>\)\r?\n')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def within_vault(path, vault):
    return Path(path).resolve(strict=False).is_relative_to(Path(vault).resolve(strict=True))


def _plan(db, vault):
    groups = defaultdict(list)
    for row in db.execute('SELECT rowid,* FROM publications ORDER BY job_id,created,rowid'):
        groups[row['job_id']].append(row)
    items, skips = [], []
    for jid, rows in groups.items():
        latest = rows[-1]
        if len(rows) == 1 and not VERSION_SUFFIX.search(Path(latest['path']).name):
            continue
        try:
            if len(rows) > 1 and rows[-2]['created'] == latest['created']:
                raise ValueError('ambiguous_latest_timestamp')
            if any(row['state'] != 'published' for row in rows):
                raise ValueError('publication_pending')
            if not Path(latest['path']).is_file():
                raise ValueError('latest_file_missing')
            canonical = Path(rows[0]['path'])
            canonical = canonical.with_name(VERSION_SUFFIX.sub('', canonical.name))
            paths = list(dict.fromkeys(row['path'] for row in rows))
            if not all(within_vault(p, vault) for p in [*paths, canonical]):
                raise ValueError('path_outside_vault')
            if canonical.exists() and str(canonical) not in paths:
                raise ValueError('canonical_path_collision')
            originals, user_sections = [], set()
            row_by_path = {row['path']: row for row in rows}
            latest_data = None
            latest_notes = None
            for path in paths:
                note = Path(path)
                if not note.exists():
                    continue
                data, section = owned_note(note, jid)
                originals.append({'path': str(note), 'hash': sha(data),
                                  'db_hash': row_by_path[path]['content_hash'],
                                  'hash_drift': sha(data) != row_by_path[path]['content_hash'],
                                  'bridge_id_verified': True,
                                  'user_section_has_content': bool(section.removeprefix('## Мои заметки').strip())})
                if section.removeprefix('## Мои заметки').strip():
                    user_sections.add(section)
                if path == latest['path']:
                    latest_data, latest_notes = data, section
            if len(user_sections) > 1:
                raise ValueError('different_user_sections')
            if latest_data is None:
                raise ValueError('latest_file_missing')
            chosen_notes = next(iter(user_sections)) if user_sections else latest_notes
            text = latest_data.decode('utf-8-sig')
            if PREVIOUS_LINK.findall(text):
                text = PREVIOUS_LINK.sub('', text)
            marker = '\n## Мои заметки'
            if text.count(marker) != 1:
                raise ValueError('ambiguous_user_section')
            data = (text[:text.index(marker) + 1] + chosen_notes).encode('utf-8')
            items.append({'job_id': jid, 'latest_version': latest['version'],
                          'canonical': str(canonical), 'latest_path': latest['path'],
                          'old': originals, 'desired_hash': sha(data),
                          'missing_old_paths': sum(not Path(p).exists() for p in paths),
                          'user_sections_conflict': False,
                          'remote_result_availability': 'not_queried'})
        except (OSError, ValueError, UnicodeError) as exc:
            skips.append({'job_id': jid, 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    return {'eligible': items, 'skipped': skips, 'eligible_count': len(items),
            'skipped_count': len(skips), 'read_only': True}


def preview(root, vault):
    path = (Path(root) / 'state.sqlite3').resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        return _plan(db, vault)
    finally:
        db.close()


def backup_database(db, destination):
    dest = sqlite3.connect(destination)
    try:
        db.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise OSError('Backup SQLite integrity check failed')
    finally:
        dest.close()


def _backup_notes(plan, backup):
    for item in plan['eligible']:
        for old in item['old']:
            data = Path(old['path']).read_bytes()
            if sha(data) != old['hash']:
                raise ValueError('Note changed after preview')
            target = backup / 'notes' / item['job_id'] / (old['hash'] + '.md')
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError('Backup collision')
            else:
                write_new(target, data)
            old['backup'] = str(target.relative_to(backup))


def _desired(item, backup):
    latest = next((entry for entry in item['old'] if entry['path'] == item['latest_path']), None)
    if latest is None:
        raise ValueError('Latest note backup missing')
    latest_data = (backup / latest['backup']).read_bytes()
    if sha(latest_data) != latest['hash']:
        raise ValueError('Latest note backup corrupted')
    _, latest_notes = _owned_backup(latest_data, item['job_id'], backup)
    sections = set()
    for old in item['old']:
        data = (backup / old['backup']).read_bytes()
        if sha(data) != old['hash']:
            raise ValueError('Note backup corrupted')
        _, section = _owned_backup(data, item['job_id'], backup)
        if section.removeprefix('## Мои заметки').strip():
            sections.add(section)
    if len(sections) > 1:
        raise ValueError('Different user sections in backup')
    chosen = next(iter(sections)) if sections else latest_notes
    text = PREVIOUS_LINK.sub('', latest_data.decode('utf-8-sig'))
    marker = '\n## Мои заметки'
    if text.count(marker) != 1:
        raise ValueError('Ambiguous user section in backup')
    data = (text[:text.index(marker) + 1] + chosen).encode('utf-8')
    if sha(data) != item['desired_hash']:
        raise ValueError('Desired hash differs from planned migration')
    return data


def _owned_backup(data, job_id, backup):
    """Validate copied data through the same note parser without touching the vault."""
    temp = backup / ('.verify-' + uuid.uuid4().hex + '.md')
    try:
        write_new(temp, data)
        return owned_note(temp, job_id)
    finally:
        temp.unlink(missing_ok=True)


def _apply_item(db, item, backup, journal_path, journal):
    jid, canonical = item['job_id'], Path(item['canonical'])
    if item['stage'] == 'done':
        return
    vault = Path(journal['vault'])
    if not all(within_vault(p, vault) for p in [canonical, *(old['path'] for old in item['old'])]):
        raise ValueError('Migration path outside vault')
    desired = _desired(item, backup)
    for old in item['old']:
        source = backup / old['backup']
        if sha(source.read_bytes()) != old['hash']:
            raise ValueError('Note backup corrupted')
        path = Path(old['path'])
        if path.exists() and sha(path.read_bytes()) not in (old['hash'], item['desired_hash'] if path == canonical else old['hash']):
            raise ValueError('Note changed after migration preview')
        if not path.exists() and item['stage'] in ('prepared', 'written'):
            raise ValueError('Note disappeared after migration preview')
    if not canonical.is_file() or sha(canonical.read_bytes()) != item['desired_hash']:
        current = next((old for old in item['old'] if old['path'] == str(canonical)), None)
        if canonical.exists():
            if current is None:
                raise ValueError('Canonical path collision')
            before = canonical.read_bytes()
            if sha(before) != current['hash']:
                raise ValueError('Canonical note changed during migration')
            owned_note(canonical, jid)
            write_replace(canonical, desired, expected=before, backup_dir=backup / 'replaced')
        else:
            write_new(canonical, desired)
    if sha(canonical.read_bytes()) != item['desired_hash']:
        raise OSError('Canonical note hash differs after write')
    item['stage'] = 'written'
    atomic_json(journal_path, journal)
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('DELETE FROM publications WHERE job_id=? AND version<>?', (jid, item['latest_version']))
        updated = db.execute("UPDATE publications SET path=?,content_hash=?,state='published' WHERE job_id=? AND version=?",
                             (str(canonical), item['desired_hash'], jid, item['latest_version']))
        if updated.rowcount != 1:
            raise ValueError('Latest publication row missing')
        recovery = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_jobs'").fetchone()
        summary = db.execute('SELECT summary_status FROM recovery_jobs WHERE job_id=?', (jid,)).fetchone() if recovery else None
        restored_state = 'summary_pending' if summary and summary[0] in ('pending', 'manual_required') else 'completed'
        db.execute("UPDATE jobs SET state=?,error=NULL,next_try=0,last_poll=0 WHERE id=? AND state='missing_output'",
                   (restored_state, jid))
        db.commit()
    except Exception:
        db.rollback()
        raise
    item['stage'] = 'db_done'
    atomic_json(journal_path, journal)
    for old in item['old']:
        path = Path(old['path'])
        if path == canonical or not path.exists():
            continue
        if sha(path.read_bytes()) != old['hash']:
            raise ValueError('Obsolete note changed during migration')
        owned_note(path, jid)
        path.unlink()
    item['stage'] = 'done'
    atomic_json(journal_path, journal)


def apply(root, vault, backup=None):
    root, vault = Path(root), Path(vault)
    if not maintenance_paused(root):
        raise RuntimeError('Pause maintenance before single-note migration')
    with WorkerLock(root):
        db = sqlite3.connect(root / 'state.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise OSError('Live SQLite integrity check failed')
            if backup is None:
                plan = _plan(db, vault)
                backup = root / 'backups' / ('single-note-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
                backup.mkdir(parents=True, exist_ok=False)
                backup_database(db, backup / 'state.sqlite3')
                _backup_notes(plan, backup)
                journal = {'vault': str(vault), 'items': [dict(item, stage='prepared') for item in plan['eligible']],
                           'skipped': plan['skipped'], 'state': 'prepared'}
                atomic_json(backup / 'journal.json', journal)
            else:
                backup = Path(backup)
                journal = json.loads((backup / 'journal.json').read_text(encoding='utf-8'))
                if journal['vault'] != str(vault):
                    raise ValueError('Migration vault differs from journal')
            for item in journal['items']:
                _apply_item(db, item, backup, backup / 'journal.json', journal)
            journal['state'] = 'done'
            atomic_json(backup / 'journal.json', journal)
            return {'backup': str(backup), 'migrated': sum(i['stage'] == 'done' for i in journal['items']),
                    'skipped': journal['skipped'], 'sqlite_integrity': db.execute('PRAGMA integrity_check').fetchone()[0]}
        finally:
            db.close()


def main():
    p = argparse.ArgumentParser()
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--preview', action='store_true')
    group.add_argument('--apply', action='store_true')
    group.add_argument('--resume', type=Path)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = p.parse_args()
    cfg = json.loads((args.root / 'config.json').read_text(encoding='utf-8-sig'))
    vault = Path(cfg['vault'])
    result = preview(args.root, vault) if args.preview else apply(args.root, vault, args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
