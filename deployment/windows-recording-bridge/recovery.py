"""Recover only verified, complete transcripts and summary-only operations."""
import hashlib
import json
from pathlib import Path
import urllib.error

from common import maintenance_paused, recovery_held
from publication import version_hash

TRANSIENT = ('connection error', 'connection refused', 'timed out', 'timeout')
DELAYS = (60, 120, 300, 900, 1800)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def summary_state(detail):
    text = str(detail.get('summary') or '').strip()
    if text.startswith('[Summary generation failed:'):
        return 'pending' if any(x in text.lower() for x in TRANSIENT) else 'manual_required'
    if not text or text.lower().startswith(('summary skipped', '[summary skipped', 'summary not generated', '[summary not generated')):
        return 'skipped'
    return 'ready'


class Recovery:
    def __init__(self, bridge):
        if bridge.cfg.get('legacy_recovery_jobs'):
            raise ValueError('Legacy backlog configuration is unsupported; preserve the old runtime and use a fresh deployment')
        self.b = bridge
        bridge.db.execute('''CREATE TABLE IF NOT EXISTS recovery_jobs(
          job_id TEXT PRIMARY KEY,transcript_status TEXT,summary_status TEXT,
          remote_job_id INTEGER,intent_at REAL,attempts INTEGER DEFAULT 0,
          next_try REAL DEFAULT 0,transcript_fingerprint TEXT,output_fingerprint TEXT,
          previous_jobs TEXT,last_error TEXT,attention_reason TEXT,first_wait REAL)''')
        bridge.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('recovery_schema_version','1')")
        bridge.db.commit()

    def update(self, jid, **fields):
        names = ','.join(k + '=?' for k in fields)
        self.b.db.execute(f'UPDATE recovery_jobs SET {names} WHERE job_id=?', (*fields.values(), jid))
        self.b.db.commit()

    def process(self, row, detail):
        b, jid = self.b, row['id']
        if maintenance_paused(b.root) or recovery_held(b.db, row):
            return
        expected = Path(row['stage'] or '').name
        if not expected or detail.get('original_filename') != expected:
            b.db.execute("UPDATE jobs SET state='identity_mismatch',error='Recording identity differs; manual reconciliation required' WHERE id=?", (jid,))
            b.db.commit()
            return
        state = summary_state(detail)
        remote_status = detail.get('status')
        summary_failed = str(detail.get('summary') or '').startswith('[Summary generation failed:')
        complete = remote_status == 'COMPLETED' or (remote_status == 'FAILED' and summary_failed)
        if not complete:
            if remote_status == 'FAILED':
                b.db.execute("UPDATE jobs SET state='failed',error='Unknown processing failure; retry manually',last_poll=? WHERE id=?", (b.clock(), jid))
                b.db.commit()
            return
        transcript = b.api.transcript(row['recording_id'])
        if remote_status == 'FAILED' and not (transcript.get('segments') or transcript.get('raw')):
            return
        tf = digest(transcript)
        rec = b.db.execute('SELECT * FROM recovery_jobs WHERE job_id=?', (jid,)).fetchone()
        sources = b.source_rows(jid)
        legacy = version_hash(detail, transcript, sources)
        out = dict(detail, _summary_status=state)
        fingerprint = version_hash(out, transcript, sources)
        if not rec:
            # Adopt existing immutable notes, without a version merely for the new format.
            baseline = b.db.execute("SELECT 1 FROM publications WHERE job_id=? AND version=? AND state='published'", (jid, legacy)).fetchone()
            b.db.execute('INSERT INTO recovery_jobs(job_id,transcript_status,summary_status,transcript_fingerprint,output_fingerprint,first_wait) VALUES(?,?,?,?,?,?)',
                         (jid, 'completed', state, tf, fingerprint if baseline and remote_status == 'COMPLETED' else None, b.clock()))
            b.db.commit()
            rec = b.db.execute('SELECT * FROM recovery_jobs WHERE job_id=?', (jid,)).fetchone()
        if state == 'pending' and (rec['summary_status'] == 'manual_required' or
                                   (rec['summary_status'] == 'skipped' and rec['last_error'] == 'Auto summary disabled')):
            state = rec['summary_status']
            out['_summary_status'] = state
            fingerprint = version_hash(out, transcript, sources)
        # A transcript edited after a summary intent must not receive a stale automatic retry.
        drift = rec['intent_at'] is not None and rec['transcript_fingerprint'] != tf
        if drift and state == 'pending':
            state = 'manual_required'
            out['_summary_status'] = state
            fingerprint = version_hash(out, transcript, sources)
            self.update(jid, summary_status=state, attention_reason='Transcript changed during recovery')
        current = b.db.execute("SELECT path FROM publications WHERE job_id=? AND state='published' ORDER BY created DESC LIMIT 1", (jid,)).fetchone()
        if current and not Path(current['path']).exists():
            b.db.execute("UPDATE jobs SET state='missing_output',error='Published note was removed; use --re-export' WHERE id=?", (jid,))
            b.db.commit()
            return
        if rec['output_fingerprint'] != fingerprint:
            published = b.publish(row, out, transcript)
            fresh_job = b.db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()
            if not published or fresh_job['state'] == 'missing_output' or maintenance_paused(b.root):
                return
            self.update(jid, output_fingerprint=fingerprint, transcript_fingerprint=tf)
        self.update(jid, transcript_status='completed')
        b.db.execute("UPDATE jobs SET state=?,error=NULL,next_try=0,last_poll=? WHERE id=?",
                     ('completed' if state in ('ready', 'skipped') else 'summary_pending', b.clock(), jid))
        b.db.commit()
        if state in ('ready', 'skipped'):
            self.update(jid, summary_status=state,
                        last_error='Auto summary disabled' if state == 'skipped' and rec['last_error'] == 'Auto summary disabled' else None,
                        attention_reason=None)
            Path(row['stage']).unlink(missing_ok=True)
            return
        rec = b.db.execute('SELECT * FROM recovery_jobs WHERE job_id=?', (jid,)).fetchone()
        if drift or rec['summary_status'] == 'manual_required':
            if rec['summary_status'] == 'manual_required' and not rec['attention_reason']:
                self.update(jid, attention_reason='Summary requires manual review')
            return
        if state == 'manual_required':
            self.update(jid, summary_status='manual_required', attention_reason='Summary failed with a non-transient error')
            return
        if not hasattr(b.api, 'jobs'):
            self.update(jid, summary_status='manual_required', attention_reason='Job queue contract unavailable')
            return
        jobs = b.api.jobs()
        if rec['intent_at'] is not None:
            previous = json.loads(rec['previous_jobs'] or '[]')
            matching = [j for j in jobs if j['recording_id'] == row['recording_id'] and j['job_type'] == 'reprocess_summary'
                        and (j['id'] == rec['remote_job_id'] if rec['remote_job_id'] else j['id'] not in previous)]
            if len(matching) != 1:
                self.update(jid, summary_status='manual_required', attention_reason='Summary intent cannot be reconciled')
                return
            job = matching[0]
            self.update(jid, remote_job_id=job['id'])
            if job['job_status'] in ('queued', 'processing'):
                self.update(jid, summary_status='running')
                return
            attempts = rec['attempts'] + 1
            self.update(jid, attempts=attempts, intent_at=None, remote_job_id=None,
                        summary_status='manual_required' if attempts >= len(DELAYS) else 'pending',
                        next_try=b.clock() + DELAYS[min(attempts - 1, len(DELAYS) - 1)],
                        attention_reason='Summary retry limit reached' if attempts >= len(DELAYS) else None)
            return
        if b.clock() < rec['next_try']:
            return
        if any(j['job_status'] in ('queued', 'processing') for j in jobs):
            return
        if b.cfg.get('llm_url'):
            from health import probe_llm
            ready = probe_llm(b.cfg)['ready']
        else:
            ready = b.cfg.get('llm_ready', False)
        if not ready or maintenance_paused(b.root):
            return
        # Fresh identity and content check immediately before the side effect.
        fresh = b.api.detail(row['recording_id'])
        if fresh.get('original_filename') != expected or summary_state(fresh) != 'pending':
            return
        if digest(b.api.transcript(row['recording_id'])) != tf:
            self.update(jid, summary_status='manual_required', attention_reason='Transcript changed before submit')
            return
        # Current Speakr's summary reprocess clears extracted events server-side.
        # A failed summary may still have events edited by a human; never clear those.
        if hasattr(b.api, 'events'):
            events = b.api.events(row['recording_id'])
            if events:
                self.update(jid, summary_status='manual_required', attention_reason='Existing events would be cleared by summary reprocess')
                return
        if hasattr(b.api, 'summary_allowed') and not b.api.summary_allowed():
            self.update(jid, summary_status='skipped', last_error='Auto summary disabled', attention_reason=None)
            return
        if maintenance_paused(b.root):
            return
        self.update(jid, summary_status='intent', intent_at=b.clock(), previous_jobs=json.dumps([j['id'] for j in jobs]))
        try:
            result = b.api.summarize(row['recording_id'])
            if not result.get('job_id'):
                raise ValueError('Summary reply has no job_id')
            self.update(jid, remote_job_id=result['job_id'], summary_status='running')
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                delay = max(60, min(1800, int(retry_after))) if retry_after.isdigit() else 60
                self.update(jid, intent_at=None, remote_job_id=None, summary_status='pending',
                            next_try=b.clock() + delay, last_error='Rate limited (HTTP 429)')
            elif exc.code in (401, 403):
                self.update(jid, intent_at=None, remote_job_id=None, summary_status='manual_required',
                            last_error='Authentication rejected', attention_reason='HTTP authentication failure')
            else:
                self.update(jid, last_error='Summary response uncertain; reconciling queue')
            exc.close()
        except Exception:
            # Intent survives crashes/unknown replies; only queue read-back can clear it.
            self.update(jid, last_error='Summary submission uncertain; reconciling queue')

    def status(self):
        rows = [dict(r) for r in self.b.db.execute('SELECT summary_status,first_wait,attention_reason FROM recovery_jobs')]
        pending = [r for r in rows if r['summary_status'] not in ('ready', 'skipped')]
        counts = {}
        for row in rows:
            counts[row['summary_status']] = counts.get(row['summary_status'], 0) + 1
        last = self.b.db.execute("SELECT max(created) FROM publications WHERE state='published'").fetchone()[0]
        failures = self.b.db.execute("SELECT count(*) FROM jobs WHERE state IN ('failed','identity_mismatch','remote_missing','missing_output','reconciliation_required')").fetchone()[0]
        return {'pending_by_stage': counts, 'oldest_wait_seconds': max([self.b.clock() - r['first_wait'] for r in pending] or [0]),
                'attention_required': bool(failures) or any(r['attention_reason'] for r in rows), 'last_successful_publication_at': last}
