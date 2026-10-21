import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge, WorkerLock, short_error
from backfill import candidates, preview
from common import enqueue, enqueue_control, media_paths, sha256
from publication import render, version_hash, write_new
from speakr_api import LocalRedirect, SpeakrAPI


class Clock:
    def __init__(self): self.now = 10000
    def __call__(self): return self.now
    def advance(self, n=121): self.now += n


class API:
    def __init__(self):
        self.rows = {}
        self.uploads = 0
        self.retries = 0
        self.upload_requests = []
        self.retry_requests = []
        self.throw_after_upload = False
        self.result = {'segments': [{'speaker': 'SPEAKER_00', 'sentence': 'Проверка', 'start_time': 0, 'end_time': 1}], 'raw': ''}
    def recordings(self): return list(self.rows.values())
    def find(self, name): return next((r for r in self.rows.values() if r['original_filename'] == name), None)
    def upload(self, path, filename, title, mtime, **kwargs):
        self.uploads += 1
        self.upload_requests.append({'path': path, 'filename': filename, 'title': title, 'mtime': mtime, **kwargs})
        rid = self.uploads
        self.rows[rid] = {'id': rid, 'status': 'COMPLETED', 'title': title, 'original_filename': filename}
        if self.throw_after_upload: raise TimeoutError('Lost reply')
        return self.rows[rid]
    def detail(self, rid): return dict(self.rows[rid])
    def transcript(self, rid): return self.result
    def retry(self, rid, **kwargs):
        self.retries += 1
        self.retry_requests.append({'rid': rid, **kwargs})
        self.rows[rid]['status'] = 'COMPLETED'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'; self.source.mkdir()
        self.outside = self.base / 'outside'; self.outside.mkdir()
        self.vault = self.base / 'vault'; self.vault.mkdir()
        self.root = self.base / 'runtime'
        self.clock, self.api = Clock(), API()
        self.cfg = {'sources': [str(self.source)], 'vault_root': str(self.vault), 'vault': str(self.vault / 'Расшифровки'),
                    'stable_seconds': 120, 'completed_poll_seconds': 0, 'speakr_url': 'http://127.0.0.1:8899',
                    'ffprobe': 'ffprobe', 'ffmpeg': 'ffmpeg', 'auto_start_docker': False}
        self.bridge = Bridge(self.cfg, self.root, self.api, self.clock)
        self.fake_run = patch('bridge.run', side_effect=self.run_media).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def run_media(self, args, timeout=0):
        if str(args[0]) == 'ffprobe':
            return type('Result', (), {'stdout': b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"2"}}'})()
        Path(args[-1]).write_bytes(b'converted:' + Path(args[args.index('-i') + 1]).read_bytes())
        return type('Result', (), {'stdout': b''})()

    def file(self, directory=None, name='voice.m4a', contents=b'audio'):
        path = (directory or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def rows(self): return self.bridge.db.execute('SELECT * FROM jobs').fetchall()
    def cycle_ready(self):
        self.bridge.cycle()
        self.clock.advance()
        self.bridge.cycle()
        self.bridge.cycle()
    def notes(self): return list(self.vault.rglob('*.md'))

    def test_acr_upload_keeps_origin_date_and_two_speakers(self):
        acr = self.outside / 'ACRPhone'
        self.cfg['exact_two_speaker_roots'] = [str(acr)]
        self.cfg['acr_timestamp_roots'] = [str(acr)]
        source = self.file(acr, 'caller-1-1790348475507.m4a')
        os.utime(source, (1790355675, 1790355675))
        enqueue([source], self.root)
        self.cycle_ready()
        req = self.api.upload_requests[0]
        self.assertEqual((req.get('min_speakers'), req.get('max_speakers')), (2, 2))
        self.assertEqual(req['title'], source.name)
        self.assertEqual(req.get('meeting_date'), '2026-09-25T15:01:15.507Z')
        self.assertIn(source.name, req.get('notes', ''))
        note = self.notes()[0].read_text(encoding='utf-8')
        self.assertIn('acr_filename_epoch_inferred_call_start', note)
        self.assertIn('source_original_filename', note)
        self.assertIn(source.name, note)

    def test_acr_without_epoch_still_requests_two_speakers_and_uses_mtime(self):
        acr = self.outside / 'ACRPhone'
        self.cfg['exact_two_speaker_roots'] = [str(acr)]
        self.cfg['acr_timestamp_roots'] = [str(acr)]
        path = self.file(acr, 'caller-bad.m4a')
        enqueue([path], self.root)
        self.cycle_ready()
        request = self.api.upload_requests[0]
        self.assertEqual((request['min_speakers'], request['max_speakers']), (2, 2))
        self.assertEqual(request['meeting_date'], self.bridge.source_rows(self.rows()[0]['id'])[0]['source_mtime_utc'])
        self.assertIn('source_mtime_fallback', self.notes()[0].read_text(encoding='utf-8'))

    def test_other_source_does_not_force_speaker_count(self):
        path = self.file(self.outside)
        enqueue([path], self.root)
        self.cycle_ready()
        self.assertNotIn('min_speakers', self.api.upload_requests[0])
        self.assertNotIn('max_speakers', self.api.upload_requests[0])

    def test_acr_retry_keeps_two_speaker_contract(self):
        acr = self.outside / 'ACRPhone'
        self.cfg['exact_two_speaker_roots'] = [str(acr)]
        source = self.file(acr)
        enqueue([source], self.root)
        self.cycle_ready()
        rid = self.rows()[0]['recording_id']
        self.api.rows[rid]['status'] = 'FAILED'
        self.bridge.cycle()
        enqueue([source], self.root)
        self.bridge.cycle(); self.bridge.cycle()
        self.assertEqual(self.api.retry_requests[0], {'rid': rid, 'min_speakers': 2, 'max_speakers': 2})

    def test_root_sibling_does_not_get_acr_policy(self):
        acr = self.outside / 'ACRPhone'
        self.cfg['exact_two_speaker_roots'] = [str(acr)]
        source = self.file(self.outside / 'ACRPhone-old')
        enqueue([source], self.root)
        self.cycle_ready()
        self.assertNotIn('min_speakers', self.api.upload_requests[0])

    def test_duplicate_new_source_adds_provenance_without_second_upload(self):
        first = self.file(self.outside, 'first.m4a', b'same-bytes')
        enqueue([first], self.root)
        self.cycle_ready()
        original_note = self.notes()[0]
        original_bytes = original_note.read_bytes()
        second = self.file(self.outside, 'second.m4a', b'same-bytes')
        os.utime(second, (1000000000, 1000000000))
        enqueue([second], self.root)
        self.bridge.cycle()
        self.clock.advance()
        self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(len(self.notes()), 1)
        updated = original_note.read_text(encoding='utf-8')
        self.assertNotEqual(original_note.read_bytes(), original_bytes)
        self.assertIn('second.m4a', updated)
        self.assertIn('2001-09-09', updated)
        self.assertIn('source_aliases:', updated)

    def test_replaced_primary_after_capture_has_no_verified_original_link_in_new_version(self):
        first = self.file(self.outside, 'first.m4a', b'original')
        enqueue([first], self.root)
        self.cycle_ready()
        old = self.notes()[0]
        old_bytes = old.read_bytes()
        first.write_bytes(b'replaced')
        alias = self.file(self.outside, 'alias.m4a', b'original')
        enqueue([alias], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        latest = self.bridge.db.execute('SELECT path FROM publications ORDER BY created DESC LIMIT 1').fetchone()['path']
        updated = Path(latest).read_text(encoding='utf-8')
        self.assertIn('source_changed', updated)
        self.assertNotIn('[Оригинал]', updated)
        self.assertIn('alias.m4a', updated)
        self.assertNotEqual(old.read_bytes(), old_bytes)

    def test_renamed_primary_after_capture_is_marked_missing_in_new_version(self):
        first = self.file(self.outside, 'first.m4a', b'original')
        enqueue([first], self.root)
        self.cycle_ready()
        first.rename(self.outside / 'moved-away.m4a')
        self.api.result = {'segments': [], 'raw': 'новая версия'}
        self.clock.advance(); self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        self.assertTrue(any('source_status: "missing"' in p.read_text(encoding='utf-8') for p in self.notes()))

    def test_snapshot_insert_failure_rolls_back_job_and_stage(self):
        source = self.file(self.outside)
        enqueue([source], self.root)
        self.bridge.cycle(); self.clock.advance()
        with patch.object(self.bridge, 'add_source_snapshot', side_effect=OSError('injected')):
            self.bridge.cycle()
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(list((self.root / 'staging').glob('*.m4a')), [])
        self.assertEqual(self.api.uploads, 0)

    def test_backfill_preview_is_read_only_and_apply_keeps_edited_note(self):
        source = self.file(self.outside)
        enqueue([source], self.root)
        self.cycle_ready()
        old = self.notes()[0]
        old.write_text(old.read_text(encoding='utf-8') + '\nМоя правка', encoding='utf-8')
        old_bytes = old.read_bytes()
        jid = self.rows()[0]['id']
        self.bridge.db.execute('DELETE FROM job_source_provenance WHERE job_id=?', (jid,))
        self.bridge.db.commit()
        before = (self.root / 'state.sqlite3').read_bytes()
        with patch('backfill.verified_snapshot', side_effect=lambda path, mtime, digest, cfg:
                   self.bridge.capture_verified_source(path, mtime, digest)):
            plan = preview(self.cfg, self.root)
        self.assertEqual(plan['count'], 1)
        self.assertTrue(plan['jobs'][0]['previous_note_edited'])
        self.assertEqual((self.root / 'state.sqlite3').read_bytes(), before)
        self.assertEqual(old.read_bytes(), old_bytes)
        result = self.bridge.apply_provenance_backfill()
        self.assertEqual(result['captured'], 1)
        self.assertEqual(self.api.uploads, 1)
        self.assertNotEqual(old.read_bytes(), old_bytes)
        self.assertIn('Моя правка', old.read_text(encoding='utf-8'))
        self.assertEqual(len(self.notes()), 1)
        again = self.bridge.apply_provenance_backfill()
        self.assertEqual(again['captured'], 0)
        self.assertEqual(len(self.notes()), 1)

    def test_completed_legacy_alias_waits_for_explicit_backfill(self):
        first = self.file(self.outside, 'old.m4a', b'duplicate')
        enqueue([first], self.root)
        self.cycle_ready()
        jid = self.rows()[0]['id']
        old = self.notes()[0]
        self.bridge.db.execute('DELETE FROM job_source_provenance WHERE job_id=?', (jid,))
        row = dict(self.rows()[0])
        detail = self.api.detail(row['recording_id'])
        transcript = self.api.transcript(row['recording_id'])
        legacy_version = version_hash(detail, transcript)
        old.write_text(render(row, detail, transcript, legacy_version, self.cfg['speakr_url']), encoding='utf-8')
        old_bytes = old.read_bytes()
        self.bridge.db.execute('UPDATE publications SET version=?,content_hash=? WHERE job_id=?',
                               (legacy_version, hashlib.sha256(old_bytes).hexdigest(), jid))
        self.bridge.db.commit()
        alias = self.file(self.outside, 'renamed.m4a', b'duplicate')
        enqueue([alias], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle(); self.bridge.cycle()
        self.assertEqual(self.bridge.source_rows(jid), [])
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.notes()[0].read_bytes(), old_bytes)
        self.assertEqual(self.api.uploads, 1)
        planned = candidates(self.bridge.db, self.cfg, self.bridge.capture_verified_source)
        self.assertEqual(len(planned[0]['sources']), 2)
        result = self.bridge.apply_provenance_backfill()
        self.assertEqual(result['captured'], 1)
        self.assertEqual(len(self.notes()), 1)
        self.assertIn('renamed.m4a', old.read_text(encoding='utf-8'))

    def test_changed_legacy_source_never_borrows_new_file_dates_or_links(self):
        source = self.file(self.outside)
        enqueue([source], self.root)
        self.cycle_ready()
        jid = self.rows()[0]['id']
        self.bridge.db.execute('DELETE FROM job_source_provenance WHERE job_id=?', (jid,))
        self.bridge.db.commit()
        source.write_bytes(b'replaced file')
        result = self.bridge.apply_provenance_backfill()
        self.assertEqual(result['captured'], 1)
        fact = self.bridge.source_rows(jid)[0]
        self.assertEqual(fact['source_status'], 'source_changed')
        self.assertIsNone(fact['media_creation_raw'])
        self.assertIsNone(fact['filesystem_created_utc'])
        new_note = sorted(self.notes(), key=lambda p: p.stat().st_mtime)[-1].read_text(encoding='utf-8')
        self.assertIn('source_changed', new_note)
        self.assertNotIn('[Оригинал]', new_note)

    def test_baseline_ignores_old_new_file_with_old_mtime_is_processed(self):
        old = self.file()
        digest = sha256(old)
        self.bridge.cycle(process=False)
        new = self.file(name='nested/новый.m4a', contents=b'new')
        os.utime(new, (1, 1))
        self.cycle_ready()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(sha256(old), digest)
        self.assertIn('Проверка', self.notes()[0].read_text(encoding='utf-8'))

    def test_growing_file_waits_and_changed_old_file_becomes_candidate(self):
        path = self.file(); self.bridge.cycle(process=False)
        path.write_bytes(b'bigger')
        self.bridge.cycle(); self.clock.advance(60)
        path.write_bytes(b'longer again'); self.bridge.cycle()
        self.clock.advance(61); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 0)
        self.clock.advance(61); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)

    def test_manual_old_outside_mixed_overlap_does_not_add_root(self):
        self.file(); self.bridge.cycle(process=False)
        media = self.file(self.outside, 'Папка & 100%/слышно.m4a')
        self.file(self.outside, 'Папка & 100%/readme.txt')
        rid, _ = enqueue([self.outside, media, self.outside], self.root)
        self.cycle_ready()
        report = json.loads((self.root / 'reports' / (rid + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(len(report['items']), 2)
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(self.cfg['sources'], [str(self.source)])
        self.file(self.outside, 'later.m4a', b'later'); self.clock.advance(); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)

    def test_manual_bypasses_baseline_and_duplicate_manual_auto_coalesce(self):
        old = self.file(); self.bridge.cycle(process=False)
        enqueue([old], self.root)
        self.file(name='newcopy.m4a')
        self.cycle_ready(); self.bridge.cycle()
        enqueue([old], self.root); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_pause_persists_and_manual_still_works_after_restart(self):
        self.bridge.cycle(process=False); self.bridge.watch(False)
        automatic = self.file(contents=b'automatic')
        manual = self.file(self.outside, contents=b'manual')
        enqueue([manual], self.root)
        self.bridge.close(); self.bridge = Bridge(self.cfg, self.root, self.api, self.clock)
        self.cycle_ready()
        self.assertEqual(self.api.uploads, 1)
        self.assertFalse(self.bridge.status()['watch_enabled'])
        self.bridge.watch(True); self.cycle_ready()
        self.assertEqual(self.api.uploads, 2)

    def test_lost_upload_reply_reconciles_without_second_upload_after_restart(self):
        path = self.file(self.outside); enqueue([path], self.root)
        self.api.throw_after_upload = True
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()
        self.assertEqual(self.rows()[0]['state'], 'submitting')
        self.bridge.close(); self.bridge = Bridge(self.cfg, self.root, self.api, self.clock)
        self.bridge.cycle(); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(self.rows()[0]['state'], 'completed')

    def test_manual_promotes_auto_candidate_while_watch_paused(self):
        self.bridge.cycle(process=False)
        path = self.file()
        self.bridge.cycle(process=False)
        self.bridge.watch(False)
        enqueue([path], self.root)
        self.cycle_ready()
        self.assertEqual(self.api.uploads, 1)

    def test_uncertain_missing_upload_is_never_blindly_resent(self):
        path = self.file(self.outside); enqueue([path], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.prepare()
        self.bridge.db.execute("UPDATE jobs SET state='submitting'"); self.bridge.db.commit()
        self.bridge.cycle()
        self.assertEqual(self.rows()[0]['state'], 'reconciliation_required')
        self.assertEqual(self.api.uploads, 0)

    def test_temporary_source_failure_recovers_after_five_failures(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle(); self.clock.advance()
        with patch('bridge.sha256', side_effect=OSError('drive offline')):
            for _ in range(6):
                self.bridge.cycle(); self.clock.advance(1000)
        self.bridge.cycle(); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)

    def test_completed_edit_updates_same_file_and_preserves_user_notes(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        first = self.notes()[0]
        first.write_text(first.read_text(encoding='utf-8') + 'Моя важная правка', encoding='utf-8')
        saved = first.read_bytes()
        self.api.result = {'segments': [], 'raw': 'Изменённый текст'}
        self.clock.advance(); self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        self.assertNotEqual(first.read_bytes(), saved)
        self.assertIn('Изменённый текст', first.read_text(encoding='utf-8'))
        self.assertIn('Моя важная правка', first.read_text(encoding='utf-8'))
        self.assertNotIn('Предыдущая версия', first.read_text(encoding='utf-8'))
        self.assertEqual(len(list((self.root / 'backups' / 'note-revisions').rglob('*.md'))), 1)
        self.bridge.cycle(); self.assertEqual(len(self.notes()), 1)

    def test_replacement_crash_after_file_write_recovers_without_second_note(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        first = self.notes()[0]
        self.api.result = {'segments': [], 'raw': 'Новый результат'}
        from publication import write_replace

        def crash(path, text, expected=None, backup_dir=None):
            write_replace(path, text, expected=expected, backup_dir=backup_dir)
            raise OSError('crash after replace')

        with patch('bridge.write_replace', side_effect=crash):
            self.clock.advance(); self.bridge.cycle()
        self.assertEqual(self.notes(), [first])
        self.assertIn('Новый результат', first.read_text(encoding='utf-8'))
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM publications WHERE state='publishing'").fetchone()[0], 1)
        self.clock.advance(60); self.bridge.cycle()
        self.assertEqual(self.notes(), [first])
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM publications').fetchone()[0], 1)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM publications WHERE state='published'").fetchone()[0], 1)

    def test_concurrent_edit_is_backed_up_and_held_for_review(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        note = self.notes()[0]
        self.api.result = {'segments': [], 'raw': 'Новый результат'}
        from publication import _replace_with_backup

        def concurrent_edit(path, temp, replaced):
            path.write_bytes(path.read_bytes() + b'concurrent user edit')
            return _replace_with_backup(path, temp, replaced)

        with patch('publication._replace_with_backup', side_effect=concurrent_edit):
            self.clock.advance(); self.bridge.cycle()
        self.assertEqual(self.rows()[0]['state'], 'note_conflict')
        backups = list((self.root / 'backups' / 'note-revisions').rglob('*.replaced'))
        self.assertEqual(len(backups), 1)
        self.assertIn(b'concurrent user edit', backups[0].read_bytes())
        saved = note.read_bytes()
        self.clock.advance(1000); self.bridge.cycle()
        self.assertEqual(note.read_bytes(), saved)

    def test_foreign_note_owner_blocks_replacement(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        first = self.notes()[0]
        changed = first.read_text(encoding='utf-8').replace(self.rows()[0]['id'], 'other-job')
        first.write_text(changed, encoding='utf-8')
        self.api.result = {'segments': [], 'raw': 'Не должно появиться'}
        self.clock.advance(); self.bridge.cycle()
        self.assertEqual(self.notes(), [first])
        self.assertEqual(first.read_text(encoding='utf-8'), changed)
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM publications').fetchone()[0], 1)

    def test_failed_job_recovers_after_manual_speaker_edit_without_retrying_asr(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        first = self.notes()[0]
        first.write_text(first.read_text(encoding='utf-8') + '\nМоя правка', encoding='utf-8')
        saved = first.read_bytes()
        row = self.rows()[0]
        rid = row['recording_id']
        self.cfg['completed_poll_seconds'] = 300
        self.api.rows[rid]['status'] = 'FAILED'
        self.bridge.db.execute("UPDATE jobs SET state='failed',last_poll=0 WHERE id=?", (row['id'],))
        self.bridge.db.commit()

        with patch.object(self.api, 'detail', wraps=self.api.detail) as detail:
            self.bridge.poll()
            self.assertEqual(detail.call_count, 1)
            self.assertEqual(self.rows()[0]['state'], 'failed')
            self.api.rows[rid]['status'] = 'COMPLETED'
            self.api.rows[rid]['participants'] = 'Анна'
            self.api.result = {'segments': [{'speaker': 'Анна', 'sentence': 'Проверка',
                                             'start_time': 0, 'end_time': 1}], 'raw': ''}
            self.clock.advance(299)
            self.bridge.poll()
            self.assertEqual(detail.call_count, 1)
            self.clock.advance(1)
            self.bridge.poll()
            self.assertEqual(detail.call_count, 2)

        self.assertEqual(self.rows()[0]['state'], 'completed')
        self.assertEqual(self.api.retries, 0)
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(len(self.notes()), 1)
        self.assertNotEqual(first.read_bytes(), saved)
        self.assertIn('Моя правка', first.read_text(encoding='utf-8'))
        self.assertIn('**Анна:**', first.read_text(encoding='utf-8'))
        self.bridge.poll()
        self.assertEqual(len(self.notes()), 1)

    def test_failed_job_follows_manual_remote_processing_to_completion(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        row = self.rows()[0]
        rid = row['recording_id']
        self.bridge.db.execute("UPDATE jobs SET state='failed',last_poll=0 WHERE id=?", (row['id'],))
        self.bridge.db.commit()
        self.api.rows[rid]['status'] = 'PROCESSING'
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'accepted')
        self.api.rows[rid]['status'] = 'COMPLETED'
        self.api.result = {'segments': [], 'raw': 'Новая ручная обработка'}
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'completed')
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.api.retries, 0)

    def test_failed_job_keeps_retry_after_publication_error(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        row = self.rows()[0]
        self.bridge.db.execute("UPDATE jobs SET state='failed',last_poll=0 WHERE id=?", (row['id'],))
        self.bridge.db.commit()
        self.api.result = {'segments': [], 'raw': 'Новая версия'}
        original_root = self.cfg['vault_root']
        self.cfg['vault_root'] = str(self.base / 'unavailable')
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'failed')
        self.assertEqual(len(self.notes()), 1)
        self.cfg['vault_root'] = original_root
        self.clock.advance(60)
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'completed')
        self.assertEqual(len(self.notes()), 1)

    def test_remote_missing_job_is_not_automatically_republished(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        row = self.rows()[0]
        self.bridge.db.execute("UPDATE jobs SET state='remote_missing' WHERE id=?", (row['id'],))
        self.bridge.db.commit()
        self.api.result = {'segments': [], 'raw': 'Новая версия'}
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'remote_missing')
        self.assertEqual(len(self.notes()), 1)

    def test_failed_job_with_missing_remote_record_does_not_publish(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        row = self.rows()[0]
        self.bridge.db.execute("UPDATE jobs SET state='failed',last_poll=0 WHERE id=?", (row['id'],))
        self.bridge.db.commit()
        error = urllib.error.HTTPError('http://127.0.0.1/recording', 404, 'missing', {}, None)
        try:
            with patch.object(self.api, 'detail', side_effect=error):
                self.bridge.poll()
        finally:
            error.close()
        self.assertEqual(self.rows()[0]['state'], 'remote_missing')
        self.assertEqual(len(self.notes()), 1)

    def test_unchanged_completed_poll_does_not_rehash_source(self):
        enqueue([self.file(self.outside)], self.root)
        self.cycle_ready()
        with patch('bridge.sha256', side_effect=AssertionError('source rehashed')) as digest:
            self.clock.advance()
            self.bridge.poll()
        digest.assert_not_called()
        self.assertIsNone(self.rows()[0]['error'])
        self.assertEqual(len(self.notes()), 1)

    def test_deleted_note_not_resurrected(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        self.notes()[0].unlink(); self.bridge.cycle()
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.rows()[0]['state'], 'missing_output')

    def test_missing_vault_retries_without_losing_completed_recording(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()
        self.vault.rmdir(); self.bridge.cycle()
        self.assertFalse(self.vault.exists())
        self.assertEqual(self.rows()[0]['state'], 'accepted')
        self.vault.mkdir(); self.clock.advance(); self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.api.uploads, 1)

    def test_silence_creates_note_raw_is_not_silence(self):
        self.api.result = {'segments': [], 'raw': ''}
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        self.assertIn('Речь не обнаружена', self.notes()[0].read_text(encoding='utf-8'))
        self.api.result = {'segments': [], 'raw': 'Настоящий текст'}; self.bridge.cycle()
        contents = [p.read_text(encoding='utf-8') for p in self.notes()]
        self.assertTrue(any('Настоящий текст' in c for c in contents))

    def test_no_audio_is_visible_and_not_submitted(self):
        enqueue([self.file(self.outside)], self.root)
        self.fake_run.side_effect = lambda *a, **k: type('R', (), {'stdout': b'{"streams":[]}'})()
        self.cycle_ready()
        self.assertEqual(self.api.uploads, 0)
        self.assertEqual(self.bridge.db.execute('SELECT error FROM observed').fetchone()[0], 'no_audio_stream')

    def test_corrupt_media_retained_and_manual_retry_after_repair(self):
        path = self.file(self.outside, contents=b'corrupt')
        enqueue([path], self.root)
        self.bridge.cycle()
        self.fake_run.side_effect = subprocess.CalledProcessError(1, ['ffprobe'], stderr=b'private')
        for _ in range(3):
            self.clock.advance(1000)
            self.bridge.cycle()
        self.assertEqual(path.read_bytes(), b'corrupt')
        self.assertEqual(self.api.uploads, 0)
        self.assertEqual(self.bridge.db.execute('SELECT kind FROM observed').fetchone()[0], 'failed')
        self.fake_run.side_effect = self.run_media
        path.write_bytes(b'repaired')
        enqueue([path], self.root)
        self.cycle_ready()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(self.rows()[0]['state'], 'completed')

    def test_service_offline_before_upload_recovers_after_worker_restart(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle()
        self.clock.advance()
        with patch.object(self.api, 'recordings', side_effect=ConnectionRefusedError('service offline')):
            self.bridge.cycle()
        self.assertEqual(self.api.uploads, 0)
        self.assertEqual(self.rows()[0]['state'], 'ready')
        self.bridge.close()
        self.bridge = Bridge(self.cfg, self.root, self.api, self.clock)
        self.bridge.cycle(); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(self.rows()[0]['state'], 'completed')

    def test_unavailable_source_does_not_initialize_or_block_other_source(self):
        missing = self.base / 'unavailable'
        self.bridge.close(); self.cfg['sources'].append(str(missing))
        self.bridge = Bridge(self.cfg, self.root, self.api, self.clock)
        self.bridge.cycle(process=False)
        states = self.bridge.status()['sources']
        self.assertEqual(states[0]['initialized'], 1)
        self.assertEqual(states[1]['initialized'], 0)
        self.assertIn('unavailable', states[1]['error'])

    def test_bad_and_empty_selection_reported(self):
        rid, _ = enqueue([self.outside, self.base / 'missing.m4a'], self.root)
        self.bridge.cycle()
        report = json.loads((self.root / 'reports' / (rid + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(len(report['items']), 2)
        self.assertTrue(all(i['status'] == 'skipped' for i in report['items']))

    def test_failed_manual_repeat_retries_existing_remote(self):
        path = self.file(self.outside); enqueue([path], self.root); self.cycle_ready()
        row = self.rows()[0]
        self.api.rows[row['recording_id']]['status'] = 'FAILED'
        self.bridge.cycle(); enqueue([path], self.root)
        self.bridge.cycle(); self.bridge.cycle()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(self.api.retries, 1)

    def test_publication_crash_after_rename_recovers_same_file(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()
        real = write_new
        def crash(path, text):
            real(path, text)
            raise OSError('crash after rename')
        with patch('bridge.write_new', side_effect=crash): self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        self.clock.advance(); self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.rows()[0]['state'], 'completed')

    def test_concurrent_target_creation_does_not_overwrite(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()
        first = True
        def race(path, text):
            nonlocal first
            if first:
                first = False
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('user data', encoding='utf-8')
            write_new(path, text)
        with patch('bridge.write_new', side_effect=race): self.bridge.cycle()
        self.assertEqual(len(self.notes()), 2)
        self.assertTrue(any(p.read_text(encoding='utf-8') == 'user data' for p in self.notes()))

    def test_first_publication_collision_survives_worker_restart(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()

        def interrupted(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('foreign user file', encoding='utf-8')
            raise OSError('injected crash before collision retry')

        with patch('bridge.write_new', side_effect=interrupted):
            self.bridge.cycle()
        self.assertEqual(self.bridge.db.execute("SELECT COUNT(*) FROM publications WHERE state='publishing'").fetchone()[0], 1)
        self.clock.advance(60); self.bridge.cycle()
        self.assertEqual(len(self.notes()), 2)
        self.assertTrue(any(p.read_text(encoding='utf-8') == 'foreign user file' for p in self.notes()))
        self.assertEqual(self.bridge.db.execute("SELECT COUNT(*) FROM publications WHERE state='published'").fetchone()[0], 1)

    def test_single_instance_lock(self):
        with WorkerLock(self.root):
            with self.assertRaises(RuntimeError):
                with WorkerLock(self.root): pass

    def test_control_request_is_durable_while_worker_lock_is_held(self):
        enqueue([self.file(self.outside)], self.root); self.cycle_ready()
        jid = self.rows()[0]['id']
        self.notes()[0].unlink()
        self.bridge.cycle()
        with WorkerLock(self.root):
            cid = enqueue_control('re-export', jid, self.root)
            self.bridge.controls()
        self.bridge.cycle()
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(json.loads((self.root / 'controls' / (cid + '.json')).read_text())['status'], 'done')

    def test_missing_transcript_does_not_mark_existing_recording_deleted(self):
        enqueue([self.file(self.outside)], self.root)
        self.bridge.cycle(); self.clock.advance(); self.bridge.cycle()
        real_api = SpeakrAPI({'speakr_url': self.cfg['speakr_url']})
        error = urllib.error.HTTPError('local', 404, 'missing', {}, None)
        with patch.object(real_api, 'request', side_effect=[error, {'status': 'COMPLETED', 'transcription': 'text'}]):
            with patch.object(self.api, 'transcript', side_effect=real_api.transcript): self.bridge.cycle()
        error.close()
        self.assertEqual(self.rows()[0]['state'], 'accepted')
        self.clock.advance(); self.bridge.cycle()
        self.assertEqual(self.rows()[0]['state'], 'completed')


class APITests(unittest.TestCase):
    def make(self): return SpeakrAPI({'speakr_url': 'http://127.0.0.1:8899', 'env_file': 'unused'})

    def test_upload_multipart_origin_and_speaker_fields(self):
        api = self.make()
        api.csrf = 'test'
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / 'bridge_x.m4a'
            media.write_bytes(b'fake-media')
            captured = {}
            def opened(request, timeout):
                captured['body'] = request.data.read()
                captured['headers'] = dict(request.headers)
                return io.BytesIO(b'{"id":7}')
            with patch.object(api.opener, 'open', side_effect=opened):
                result = api.upload(media, media.name, 'original name.m4a', 1,
                                    meeting_date='2026-09-25T15:01:15.507Z',
                                    notes='Источник записи', min_speakers=2, max_speakers=2)
        self.assertEqual(result['id'], 7)
        body = captured['body']
        for field in (b'name="title"\r\n\r\noriginal name.m4a',
                      b'name="meeting_date"\r\n\r\n2026-09-25T15:01:15.507Z',
                      b'name="min_speakers"\r\n\r\n2',
                      b'name="max_speakers"\r\n\r\n2',
                      b'name="notes"\r\n\r\n'):
            self.assertIn(field, body)
        self.assertIn(b'filename="bridge_x.m4a"', body)

    def test_retry_json_speaker_fields(self):
        api = self.make()
        with patch.object(api, 'request', return_value={'status': 'QUEUED'}) as request:
            api.retry(7, min_speakers=2, max_speakers=2)
        self.assertEqual(request.call_args.args[1]['min_speakers'], 2)
        self.assertEqual(request.call_args.args[1]['max_speakers'], 2)

    def test_pagination_finds_second_page(self):
        api = self.make()
        with patch.object(api, 'request', side_effect=[{'recordings': [], 'pagination': {'has_next': True}},
            {'recordings': [{'id': 9, 'original_filename': 'target'}], 'pagination': {'has_next': False}}]) as call:
            self.assertEqual(api.find('target')['id'], 9)
            self.assertIn('page=2', call.call_args.args[0])

    def test_page_error_is_not_absence(self):
        api = self.make()
        with patch.object(api, 'request', side_effect=[{'recordings': [], 'pagination': {'has_next': True}}, OSError('offline')]):
            with self.assertRaises(OSError): api.find('target')

    def test_transcript_404_completed_empty_only(self):
        api = self.make()
        error = urllib.error.HTTPError('local', 404, 'missing', {}, None)
        with patch.object(api, 'request', side_effect=[error, {'status': 'COMPLETED', 'transcription': None}]):
            self.assertEqual(api.transcript(1)['segments'], [])
        with patch.object(api, 'request', side_effect=[error, error]):
            with self.assertRaises(urllib.error.HTTPError): api.transcript(1)
        with patch.object(api, 'request', side_effect=[error, {'status': 'COMPLETED', 'transcription': 'real'}]):
            with self.assertRaises(RuntimeError): api.transcript(1)
        error.close()

    def test_server_error_not_silence(self):
        api = self.make()
        error = urllib.error.HTTPError('local', 500, 'bad', {}, None)
        with patch.object(api, 'request', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError): api.transcript(1)
        error.close()

    def test_raw_contract_and_external_origin(self):
        api = self.make()
        with patch.object(api, 'request', return_value={'segments': [], 'raw': 'text'}):
            self.assertEqual(api.transcript(1)['raw'], 'text')
        with self.assertRaises(ValueError): SpeakrAPI({'speakr_url': 'https://example.com'})
        with self.assertRaises(PermissionError):
            LocalRedirect('http://127.0.0.1:8899').redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_http_error_does_not_log_response_or_secret(self):
        error = urllib.error.HTTPError('https://invalid/?token=secret', 401, 'secret', {}, io.BytesIO(b'secret'))
        self.assertEqual(short_error(error), 'HTTP 401')
        error.close()


if __name__ == '__main__': unittest.main()
