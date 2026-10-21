import json
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

import test_bridge as fixtures
from common import enqueue, enqueue_control


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.BridgeTests.setUp
    tearDown = fixtures.BridgeTests.tearDown
    run_media = fixtures.BridgeTests.run_media
    file = fixtures.BridgeTests.file
    rows = fixtures.BridgeTests.rows
    notes = fixtures.BridgeTests.notes
    cycle_ready = fixtures.BridgeTests.cycle_ready

    def uploaded(self, summary='[Summary generation failed: Connection error.]'):
        self.cfg['recovery_enabled'] = True
        self.api.jobs = lambda: self.queue
        self.queue = []
        self.posts = []
        self.api.summarize = self.summarize
        self.api.events = lambda rid: []
        self.api.summary_allowed = lambda: True
        enqueue([self.file(self.outside)], self.root)
        self.cycle_ready()
        row = self.rows()[0]
        self.api.rows[row['recording_id']].update(status='FAILED', summary=summary)
        self.cfg['llm_ready'] = False
        return row

    def summarize(self, rid):
        self.posts.append(rid)
        job = {'id': 100 + len(self.posts), 'recording_id': rid, 'job_type': 'reprocess_summary', 'job_status': 'queued'}
        self.queue.append(job)
        return {'job_id': job['id']}

    def test_failed_summary_exports_text_then_summary_only_once(self):
        row = self.uploaded()
        self.bridge.poll()
        notes = self.notes()
        pending = next(p for p in notes if 'summary_status: "pending"' in p.read_text(encoding='utf8'))
        before = pending.read_bytes()
        self.assertIn('Резюме ожидается', before.decode())
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.assertEqual(self.posts, [row['recording_id']])
        self.bridge.poll()
        self.assertEqual(len(self.posts), 1)
        self.queue[0]['job_status'] = 'completed'
        self.api.rows[row['recording_id']].update(status='COMPLETED', summary='Полезное резюме')
        self.bridge.poll()
        count = len(self.notes())
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)
        self.assertNotEqual(pending.read_bytes(), before)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.api.retries, 0)
        self.assertTrue(any('Полезное резюме' in p.read_text(encoding='utf8') for p in self.notes()))

    def test_reexport_with_recovery_restores_pinned_path(self):
        row = self.uploaded()
        self.bridge.poll()
        note = self.notes()[0]
        self.assertIsNotNone(self.bridge.db.execute('SELECT output_fingerprint FROM recovery_jobs WHERE job_id=?',
                                                    (row['id'],)).fetchone()[0])
        note.unlink()
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'missing_output')
        enqueue_control('re-export', row['id'], self.root)
        self.bridge.controls()
        self.bridge.poll()
        self.assertTrue(note.is_file())
        self.assertEqual(self.bridge.db.execute('SELECT path FROM publications WHERE job_id=?',
                                                (row['id'],)).fetchone()[0], str(note))

    def test_reused_recording_id_never_published_or_summarized(self):
        row = self.uploaded()
        count = len(self.notes())
        self.api.rows[row['recording_id']]['original_filename'] = 'another-bridge-id.m4a'
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)
        self.assertEqual(self.rows()[0]['state'], 'identity_mismatch')
        self.assertEqual(self.posts, [])

    def test_incomplete_asr_or_unknown_failure_is_not_exported(self):
        row = self.uploaded('CUDA out of memory')
        count = len(self.notes())
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)
        self.assertEqual(self.posts, [])
        self.api.rows[row['recording_id']].update(status='PROCESSING', summary='')
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)

    def test_uncertain_post_after_enqueue_reconciles_without_resending(self):
        row = self.uploaded()
        self.cfg['llm_ready'] = True
        def timeout(rid):
            self.summarize(rid)
            raise TimeoutError('lost response')
        self.api.summarize = timeout
        self.bridge.poll()
        self.bridge.poll()
        self.assertEqual(self.posts, [row['recording_id']])
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertEqual(rec['remote_job_id'], 101)

    def test_uncertain_post_without_history_fails_closed(self):
        self.uploaded()
        self.cfg['llm_ready'] = True
        self.api.summarize = lambda rid: (_ for _ in ()).throw(TimeoutError('lost'))
        self.bridge.poll()
        self.bridge.poll()
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertEqual(rec['summary_status'], 'manual_required')

    def test_maintenance_blocks_existing_worker_side_effects(self):
        self.uploaded()
        count = len(self.notes())
        self.cfg['llm_ready'] = True
        (self.root / 'maintenance.json').write_text(json.dumps({'paused': True}))
        self.bridge.cycle()
        self.assertEqual(len(self.notes()), count)
        self.assertEqual(self.posts, [])
        self.assertTrue(self.bridge.status()['maintenance'])

    def test_deleted_note_is_not_recreated_by_partial_export(self):
        self.uploaded()
        for p in self.notes():
            p.unlink()
        self.bridge.poll()
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.rows()[0]['state'], 'missing_output')

    def test_successful_summary_drift_cancels_retry_and_preserves_edits(self):
        row = self.uploaded()
        self.bridge.poll()
        self.api.rows[row['recording_id']].update(status='COMPLETED', summary='Написано человеком')
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.assertEqual(self.posts, [])

    def test_completed_without_summary_is_terminal_skipped(self):
        row = self.uploaded()
        self.api.rows[row['recording_id']].update(status='COMPLETED', summary='')
        self.bridge.poll()
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertEqual(rec['summary_status'], 'skipped')
        self.assertEqual(self.posts, [])

    def test_existing_events_are_never_cleared_by_summary_retry(self):
        self.uploaded()
        self.api.events = lambda rid: [{'id': 1}]
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.assertEqual(self.posts, [])
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertEqual(rec['summary_status'], 'manual_required')

    def test_disabled_auto_summary_preference_is_honoured(self):
        self.uploaded()
        self.api.summary_allowed = lambda: False
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.bridge.db.execute('SELECT summary_status FROM recovery_jobs').fetchone()[0], 'skipped')
        self.bridge.poll()
        count = len(self.notes())
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)

    def test_nontransient_summary_failure_still_publishes_text(self):
        self.uploaded('[Summary generation failed: Invalid API key.]')
        count = len(self.notes())
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)
        self.assertEqual(self.posts, [])
        self.assertTrue(self.bridge.status()['attention_required'])

    def test_publication_returning_missing_never_advances_recovery(self):
        self.uploaded()
        publish = self.bridge.publish
        def removed(*args):
            for p in self.notes(): p.unlink()
            return publish(*args)
        self.bridge.publish = removed
        self.bridge.poll()
        self.assertEqual(self.rows()[0]['state'], 'missing_output')
        self.assertEqual(self.posts, [])

    def test_paused_publication_never_records_output_fingerprint(self):
        self.uploaded()
        self.bridge.publish = lambda *args: False
        self.bridge.poll()
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertEqual(rec['summary_status'], 'skipped')  # Last successfully exported legacy output.
        self.assertEqual(self.posts, [])

    def test_definite_http_429_backs_off_without_uncertain_intent(self):
        self.uploaded()
        self.cfg['llm_ready'] = True
        calls = []
        def rate_limit(rid):
            calls.append(rid)
            raise urllib.error.HTTPError('http://127.0.0.1', 429, 'rate limit', {'Retry-After': '120'}, None)
        self.api.summarize = rate_limit
        self.bridge.poll()
        self.bridge.poll()
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertIsNone(rec['intent_at'])
        self.assertEqual(rec['next_try'], self.clock() + 120)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rec['attempts'], 0)

    def test_http_auth_failure_requires_attention_without_restart(self):
        self.auth_case(401)

    def test_http_forbidden_requires_attention_without_restart(self):
        self.auth_case(403)

    def auth_case(self, code):
        self.uploaded()
        self.cfg['llm_ready'] = True
        self.api.summarize = lambda rid: (_ for _ in ()).throw(urllib.error.HTTPError('http://127.0.0.1', code, 'auth', {}, None))
        self.bridge.poll()
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertIsNone(rec['intent_at'])
        self.assertEqual(rec['summary_status'], 'manual_required')
        self.assertTrue(self.bridge.status()['attention_required'])

    def test_legacy_backlog_configuration_is_rejected(self):
        self.cfg['legacy_recovery_jobs'] = ['unapproved']
        with self.assertRaisesRegex(ValueError, 'Legacy backlog configuration'):
            fixtures.Bridge(self.cfg, self.base / 'other-runtime', self.api, self.clock)

    def test_backoff_retry_limit_persists_after_restart(self):
        self.uploaded()
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.queue[0]['job_status'] = 'failed'
        self.bridge.poll()
        rec = self.bridge.db.execute('SELECT * FROM recovery_jobs').fetchone()
        self.assertEqual(rec['attempts'], 1)
        self.bridge.poll()
        self.assertEqual(len(self.posts), 1)
        self.clock.advance(61)
        self.bridge.poll()
        self.assertEqual(len(self.posts), 2)

    def test_job_history_expired_after_reboot_never_resends(self):
        self.uploaded()
        self.cfg['llm_ready'] = True
        self.bridge.poll()
        self.queue.clear()
        self.clock.advance(7200)
        self.bridge.poll()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.bridge.db.execute('SELECT summary_status FROM recovery_jobs').fetchone()[0], 'manual_required')

    def test_multi_chunk_partial_is_not_published_until_terminal_summary_failure(self):
        row = self.uploaded()
        self.api.rows[row['recording_id']].update(status='PROCESSING', summary='')
        self.api.result['segments'] *= 3
        count = len(self.notes())
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)
        self.api.rows[row['recording_id']].update(status='FAILED', summary='[Summary generation failed: Connection error.]')
        self.bridge.poll()
        self.assertEqual(len(self.notes()), count)


if __name__ == '__main__':
    unittest.main()
