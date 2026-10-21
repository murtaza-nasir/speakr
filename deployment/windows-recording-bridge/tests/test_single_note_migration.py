import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bridge as fixtures
from common import enqueue
from publication import render
from single_note_migration import apply, preview


class SingleNoteMigrationTests(unittest.TestCase):
    setUp = fixtures.BridgeTests.setUp
    tearDown = fixtures.BridgeTests.tearDown
    run_media = fixtures.BridgeTests.run_media
    file = fixtures.BridgeTests.file
    rows = fixtures.BridgeTests.rows
    notes = fixtures.BridgeTests.notes
    cycle_ready = fixtures.BridgeTests.cycle_ready

    def legacy_versions(self, different_notes=False):
        enqueue([self.file(self.outside)], self.root)
        self.cycle_ready()
        old = self.notes()[0]
        jid = self.rows()[0]['id']
        row = dict(self.rows()[0])
        if different_notes:
            old.write_text(old.read_text(encoding='utf-8') + 'Старая личная запись\n', encoding='utf-8')
        latest = old.with_name(old.stem + ' — версия ' + 'b' * 10 + '.md')
        detail = self.api.detail(row['recording_id'])
        text = render(row, detail, {'segments': [], 'raw': 'Исправленный текст'}, 'b' * 64,
                      self.cfg['speakr_url'])
        text = text.replace('## Мои заметки', '- [Предыдущая версия](<file:///old.md>)\n\n## Мои заметки')
        if different_notes:
            text += 'Другая личная запись\n'
        latest.write_text(text, encoding='utf-8')
        self.bridge.db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?)',
                               (jid, 'b' * 64, str(latest), hashlib.sha256(latest.read_bytes()).hexdigest(),
                                'published', self.clock() + 1))
        self.bridge.db.commit()
        return old, latest, jid

    def test_preview_is_read_only_and_apply_collapses_versions(self):
        old, latest, jid = self.legacy_versions()
        self.bridge.db.execute("UPDATE jobs SET state='missing_output',error='old note removed' WHERE id=?", (jid,))
        self.bridge.db.commit()
        before = [tuple(row) for row in self.bridge.db.execute('SELECT * FROM publications ORDER BY created')]
        plan = preview(self.root, self.vault)
        self.assertEqual(plan['eligible_count'], 1)
        self.assertEqual(plan['skipped_count'], 0)
        self.assertEqual(plan['eligible'][0]['old'][0]['bridge_id_verified'], True)
        self.assertEqual(plan['eligible'][0]['old'][0]['hash_drift'], False)
        self.assertEqual([tuple(row) for row in self.bridge.db.execute('SELECT * FROM publications ORDER BY created')], before)
        self.assertTrue(latest.exists())
        (self.root / 'maintenance.json').write_text(json.dumps({'paused': True}), encoding='utf-8')
        outcome = apply(self.root, self.vault)
        self.assertEqual(outcome['migrated'], 1)
        self.assertEqual(outcome['sqlite_integrity'], 'ok')
        self.assertEqual(self.notes(), [old])
        self.assertIn('Исправленный текст', old.read_text(encoding='utf-8'))
        self.assertNotIn('Предыдущая версия', old.read_text(encoding='utf-8'))
        rows = self.bridge.db.execute('SELECT * FROM publications WHERE job_id=?', (jid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], str(old))
        self.assertEqual(rows[0]['content_hash'], hashlib.sha256(old.read_bytes()).hexdigest())
        self.assertEqual(self.bridge.db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()[0], 'completed')
        backup = Path(outcome['backup'])
        self.assertEqual(len(list((backup / 'notes').rglob('*.md'))), 2)
        self.assertEqual(apply(self.root, self.vault, backup)['migrated'], 1)

    def test_different_user_sections_are_skipped_without_file_changes(self):
        old, latest, _ = self.legacy_versions(different_notes=True)
        before = (old.read_bytes(), latest.read_bytes())
        plan = preview(self.root, self.vault)
        self.assertEqual(plan['eligible_count'], 0)
        self.assertEqual(plan['skipped'][0]['reason'], 'different_user_sections')
        (self.root / 'maintenance.json').write_text(json.dumps({'paused': True}), encoding='utf-8')
        outcome = apply(self.root, self.vault)
        self.assertEqual(outcome['migrated'], 0)
        self.assertEqual((old.read_bytes(), latest.read_bytes()), before)

    def test_equal_version_timestamps_are_skipped(self):
        old, latest, jid = self.legacy_versions()
        created = self.bridge.db.execute('SELECT MIN(created) FROM publications WHERE job_id=?', (jid,)).fetchone()[0]
        self.bridge.db.execute('UPDATE publications SET created=? WHERE job_id=?', (created, jid))
        self.bridge.db.commit()
        plan = preview(self.root, self.vault)
        self.assertEqual(plan['eligible_count'], 0)
        self.assertEqual(plan['skipped'][0]['reason'], 'ambiguous_latest_timestamp')
        self.assertTrue(old.is_file() and latest.is_file())

    def test_missing_latest_and_foreign_canonical_are_skipped(self):
        old, latest, _ = self.legacy_versions()
        latest_bytes = latest.read_bytes()
        latest.unlink()
        self.assertEqual(preview(self.root, self.vault)['skipped'][0]['reason'], 'latest_file_missing')
        latest.write_bytes(latest_bytes)
        old.unlink()
        old.write_text('foreign file', encoding='utf-8')
        self.assertEqual(preview(self.root, self.vault)['eligible_count'], 0)
        self.assertEqual(preview(self.root, self.vault)['skipped'][0]['reason'], 'Published note has no frontmatter')
        self.assertEqual(old.read_text(encoding='utf-8'), 'foreign file')

    def test_crash_after_database_update_can_resume_cleanup(self):
        old, latest, jid = self.legacy_versions()
        (self.root / 'maintenance.json').write_text(json.dumps({'paused': True}), encoding='utf-8')
        unlink = Path.unlink
        failed = [False]

        def crash(path, *args, **kwargs):
            if path == latest and not failed[0]:
                failed[0] = True
                raise OSError('injected cleanup crash')
            return unlink(path, *args, **kwargs)

        with patch('single_note_migration.Path.unlink', crash):
            with self.assertRaisesRegex(OSError, 'injected cleanup crash'):
                apply(self.root, self.vault)
        backup = next((self.root / 'backups').glob('single-note-*'))
        self.assertTrue(latest.exists())
        result = apply(self.root, self.vault, backup)
        self.assertEqual(result['migrated'], 1)
        self.assertFalse(latest.exists())
        self.assertEqual(len(self.bridge.db.execute('SELECT * FROM publications WHERE job_id=?', (jid,)).fetchall()), 1)

    def test_crash_after_canonical_replace_can_resume_database_update(self):
        old, latest, jid = self.legacy_versions()
        (self.root / 'maintenance.json').write_text(json.dumps({'paused': True}), encoding='utf-8')
        from single_note_migration import write_replace

        def crash(path, text, expected=None, backup_dir=None):
            write_replace(path, text, expected=expected, backup_dir=backup_dir)
            raise OSError('injected replace crash')

        with patch('single_note_migration.write_replace', side_effect=crash):
            with self.assertRaisesRegex(OSError, 'injected replace crash'):
                apply(self.root, self.vault)
        backup = next((self.root / 'backups').glob('single-note-*'))
        self.assertIn('Исправленный текст', old.read_text(encoding='utf-8'))
        self.assertTrue(latest.exists())
        result = apply(self.root, self.vault, backup)
        self.assertEqual(result['migrated'], 1)
        self.assertFalse(latest.exists())
        self.assertEqual(len(self.bridge.db.execute('SELECT * FROM publications WHERE job_id=?', (jid,)).fetchall()), 1)


if __name__ == '__main__':
    unittest.main()
