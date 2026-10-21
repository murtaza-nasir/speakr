"""Durable Windows folder -> Speakr -> Obsidian bridge; no writes to source media."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from common import (ROOT, CREATE_NO_WINDOW, atomic_json, config, enqueue, enqueue_control,
                    media_paths, path_key, run, sha256, signature, start_worker, maintenance_paused)
from common import migration_guard, recovery_held
from backfill import candidates, preview
from publication import (NoteConflict, backup_original, choose_path, is_our_complete_file, owned_note,
                         render, version_hash, with_user_notes, write_new, write_replace)
from provenance import is_acr, snapshot, speakr_notes, verified_snapshot
from speakr_api import SpeakrAPI


class WorkerLock:
    def __init__(self, root):
        self.path = Path(root) / 'worker.lock'
        self.file = None

    def __enter__(self):
        self.file = self.path.open('a+b')
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Bridge is already running') from None
        return self

    def __exit__(self, *args):
        self.file.close()


def short_error(exc):
    # Never include HTTP response bodies, environment dictionaries or command output.
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}'
    if isinstance(exc, subprocess.CalledProcessError):
        return f'{Path(str(exc.cmd[0])).name}: exit {exc.returncode}'
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'Operation timeout'
    return type(exc).__name__ + ': ' + str(exc)[:220]


class Bridge:
    def __init__(self, cfg, root=ROOT, api=None, clock=time.time):
        migration_guard(root)
        if cfg.get('legacy_recovery_jobs'):
            raise ValueError('Legacy backlog configuration is unsupported; preserve the old runtime and use a fresh deployment')
        self.cfg, self.root, self.clock = cfg, Path(root), clock
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('staging', 'requests', 'reports', 'logs', 'controls'):
            (self.root / name).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'state.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('schema_version','1');
            INSERT OR IGNORE INTO meta VALUES('watch_enabled','true');
            CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,initialized INTEGER DEFAULT 0,error TEXT);
            CREATE TABLE IF NOT EXISTS observed(
              key TEXT PRIMARY KEY,path TEXT NOT NULL,sig TEXT,stable_since REAL,
              kind TEXT,origin TEXT,job_id TEXT,error TEXT,next_try REAL DEFAULT 0,attempts INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY,sha256 TEXT UNIQUE NOT NULL,source_path TEXT,mtime REAL,
              state TEXT,stage TEXT,recording_id INTEGER,error TEXT,attempts INTEGER DEFAULT 0,
              next_try REAL DEFAULT 0,submitted_at REAL,last_poll REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS job_source_provenance(
              job_id TEXT NOT NULL,normalized_source_path TEXT NOT NULL,
              source_path TEXT,original_filename TEXT,source_category TEXT,relative_path TEXT,
              source_mtime_utc TEXT,filesystem_created_utc TEXT,media_creation_raw TEXT,
              media_creation_source TEXT,filename_epoch_utc TEXT,selected_date_utc TEXT,
              selected_date_basis TEXT,source_status TEXT,captured_at_utc TEXT,
              PRIMARY KEY(job_id,normalized_source_path));
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY,paths TEXT,state TEXT,created REAL,error TEXT);
            CREATE TABLE IF NOT EXISTS request_items(
              request_id TEXT,key TEXT,path TEXT,reason TEXT,job_id TEXT,
              PRIMARY KEY(request_id,key));
            CREATE TABLE IF NOT EXISTS publications(
              job_id TEXT,version TEXT,path TEXT,content_hash TEXT,state TEXT,
              created REAL,PRIMARY KEY(job_id,version));
        ''')
        for source in cfg['sources']:
            self.db.execute('INSERT OR IGNORE INTO sources(path) VALUES(?)', (source,))
        self.db.commit()
        self.api = api or SpeakrAPI(cfg)
        self.last_services = 0
        self.error = None
        self.heartbeat = None
        self.recovery = None
        if cfg.get('recovery_enabled'):
            from recovery import Recovery
            self.recovery = Recovery(self)
        self.log = logging.getLogger('bridge.' + str(self.root))
        if not self.log.handlers:
            handler = RotatingFileHandler(self.root / 'logs' / 'bridge.log', maxBytes=2_000_000,
                                          backupCount=4, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self.log.addHandler(handler)
            self.log.setLevel(logging.INFO)

    def close(self):
        self.db.close()
        for handler in self.log.handlers[:]:
            handler.close()
            self.log.removeHandler(handler)

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def watch(self, enabled):
        self.db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(enabled), 'watch_enabled'))
        self.db.commit()

    def source_rows(self, job_id):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM job_source_provenance WHERE job_id=? ORDER BY rowid', (job_id,))]

    def publication_sources(self, job):
        """Check links against present bytes without rewriting historical snapshots."""
        sources = self.source_rows(job['id'])
        for source in sources:
            if source['source_status'] != 'verified':
                continue
            path = Path(source['source_path'])
            try:
                if not path.is_file():
                    source['source_status'] = 'missing'
                    continue
                before = signature(path)
                if sha256(path) != job['sha256'] or signature(path) != before:
                    source['source_status'] = 'source_changed'
            except FileNotFoundError:
                source['source_status'] = 'missing'
            except OSError:
                source['source_status'] = 'source_changed'
        return sources

    def apply_provenance_backfill(self):
        """Explicit migration only. Existing Speakr metadata is never updated."""
        planned = candidates(self.db, self.cfg, self.capture_verified_source)
        for item in planned:
            job = self.db.execute('SELECT * FROM jobs WHERE id=?', (item['job_id'],)).fetchone()
            if recovery_held(self.db, job):
                continue
            for source in item['sources']:
                self.add_source_snapshot(item['job_id'], source)
            self.db.commit()
        published = []
        errors = []
        for row in self.db.execute("SELECT * FROM jobs WHERE state='completed' ORDER BY rowid").fetchall():
            if not self.source_rows(row['id']):
                continue
            try:
                before = self.db.execute('SELECT COUNT(*) FROM publications WHERE job_id=?', (row['id'],)).fetchone()[0]
                detail = self.api.detail(row['recording_id'])
                transcript = self.api.transcript(row['recording_id'])
                self.publish(row, detail, transcript)
                after = self.db.execute('SELECT COUNT(*) FROM publications WHERE job_id=?', (row['id'],)).fetchone()[0]
                if after > before:
                    published.append(row['id'])
            except Exception as exc:
                errors.append({'job_id': row['id'], 'error': short_error(exc)})
        return {'captured': len(planned), 'new_note_jobs': published, 'errors': errors,
                'speakr_metadata_updated': False}

    def add_source_snapshot(self, job_id, source):
        columns = tuple(source)
        self.db.execute(
            'INSERT OR IGNORE INTO job_source_provenance(job_id,' + ','.join(columns) + ') VALUES (' +
            ','.join('?' for _ in range(len(columns) + 1)) + ')',
            (job_id,) + tuple(source[column] for column in columns))

    def capture_verified_source(self, path, mtime, digest):
        return verified_snapshot(path, mtime, digest, self.cfg, run)

    def observe(self, path, origin, baseline=False):
        key, sig = path_key(path), signature(path)
        row = self.db.execute('SELECT * FROM observed WHERE key=?', (key,)).fetchone()
        if not row:
            self.db.execute('INSERT INTO observed(key,path,sig,stable_since,kind,origin) VALUES(?,?,?,?,?,?)',
                (key, str(path), sig, self.clock(), 'baseline' if baseline else 'candidate', origin))
        elif row['sig'] != sig:
            self.db.execute("UPDATE observed SET sig=?,stable_since=?,kind='candidate',origin=?,"
                            'job_id=NULL,error=NULL,next_try=0,attempts=0 WHERE key=?',
                            (sig, self.clock(), origin, key))
        elif origin == 'manual':
            self.db.execute("UPDATE observed SET origin='manual' WHERE key=?", (key,))
            if row['kind'] in ('baseline', 'failed'):
                self.db.execute("UPDATE observed SET kind='candidate',origin='manual',error=NULL,next_try=0 WHERE key=?", (key,))
            if row['job_id']:
                job = self.db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
                if job and job['state'] == 'failed':
                    self.db.execute("UPDATE jobs SET state=?,next_try=0,error=NULL WHERE id=?",
                                    ('retry' if job['recording_id'] else 'ready', job['id']))
        self.db.commit()
        return key

    def scan_sources(self):
        if self.meta('watch_enabled') != 'true':
            return
        for source in self.cfg['sources']:
            row = self.db.execute('SELECT * FROM sources WHERE path=?', (source,)).fetchone()
            entries = list(media_paths(source))
            errors = [r for p, r in entries if r and (r.startswith('unavailable') or
                      (r == 'reparse_point' and path_key(p) == path_key(source)))]
            if errors:
                self.db.execute('UPDATE sources SET error=? WHERE path=?', ('; '.join(errors[:3]), source))
                self.db.commit()
                continue  # An incomplete baseline must never turn into a valid empty baseline.
            try:
                for path, reason in entries:
                    if reason is None:
                        self.observe(path, 'auto', baseline=not row['initialized'])
                self.db.execute('UPDATE sources SET initialized=1,error=NULL WHERE path=?', (source,))
                self.db.commit()
            except OSError as exc:
                self.db.execute('UPDATE sources SET error=? WHERE path=?', (short_error(exc), source))
                self.db.commit()

    def ingest_requests(self):
        for path in sorted((self.root / 'requests').glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                request_id = data['id']
                if request_id != path.stem or not all(c in '0123456789abcdef' for c in request_id) or len(request_id) != 32:
                    raise ValueError('Invalid request id')
                if not isinstance(data['paths'], list) or not all(isinstance(p, str) for p in data['paths']):
                    raise ValueError('Invalid request paths')
                row = self.db.execute('SELECT state FROM requests WHERE id=?', (request_id,)).fetchone()
                if row and row['state'] == 'expanded':
                    continue
                self.db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,NULL)',
                                (request_id, json.dumps(data['paths']), 'expanding', data['created']))
                self.db.commit()
                for selected in data['paths']:
                    found = False
                    for media, reason in media_paths(selected):
                        found = True
                        key = path_key(media)
                        if reason is None:
                            try:
                                key = self.observe(media, 'manual')
                            except OSError as exc:
                                reason = short_error(exc)
                        self.db.execute('INSERT OR IGNORE INTO request_items VALUES(?,?,?,?,NULL)',
                                        (request_id, key, str(media), reason))
                    if not found:
                        self.db.execute('INSERT OR IGNORE INTO request_items VALUES(?,?,?,?,NULL)',
                                        (request_id, path_key(selected), str(selected), 'empty_folder'))
                self.db.execute("UPDATE requests SET state='expanded' WHERE id=?", (request_id,))
                self.db.commit()
            except (ValueError, KeyError, OSError) as exc:
                self.log.warning('Request %s: %s', path.stem, short_error(exc))

    def prepare(self):
        rows = self.db.execute("SELECT * FROM observed WHERE kind='candidate' AND next_try<=? ORDER BY stable_since",
                               (self.clock(),)).fetchall()
        for row in rows:
            if self.meta('watch_enabled') != 'true' and row['origin'] == 'auto':
                continue
            path = Path(row['path'])
            target = None
            try:
                if signature(path) != row['sig']:
                    self.observe(path, row['origin'])
                    continue
                if self.clock() - row['stable_since'] < self.cfg['stable_seconds']:
                    continue
                # Offline placeholders must hydrate successfully before hashing or probing.
                digest = sha256(path)
                if signature(path) != row['sig']:
                    self.observe(path, row['origin'])
                    continue
                existing = self.db.execute('SELECT * FROM jobs WHERE sha256=?', (digest,)).fetchone()
                if existing:
                    # Only jobs already using provenance get new aliases automatically.
                    if self.source_rows(existing['id']):
                        key = path_key(path)
                        if not any(s['normalized_source_path'] == key for s in self.source_rows(existing['id'])):
                            source = self.capture_verified_source(path, path.stat().st_mtime, digest)
                            self.add_source_snapshot(existing['id'], source)
                    if row['origin'] == 'manual' and existing['state'] == 'failed':
                        self.db.execute("UPDATE jobs SET state=?,error=NULL,next_try=0 WHERE id=?",
                                        ('retry' if existing['recording_id'] else 'ready', existing['id']))
                    self.attach(row['key'], existing['id'])
                    continue
                probe = json.loads(run([self.cfg['ffprobe'], '-v', 'error', '-show_streams',
                                        '-show_format', '-of', 'json', path], timeout=90).stdout)
                if not any(s.get('codec_type') == 'audio' for s in probe.get('streams', [])):
                    self.db.execute("UPDATE observed SET kind='ignored',error='no_audio_stream' WHERE key=?", (row['key'],))
                    self.db.commit()
                    continue
                source_mtime = path.stat().st_mtime
                source_fact = snapshot(path, source_mtime, self.cfg, probe)
                duration = float(probe.get('format', {}).get('duration') or 0)
                required = max(64 * 1024 * 1024, int(duration * 8000 * 3))
                if shutil.disk_usage(self.root).free < required:
                    raise OSError('Insufficient space for audio and upload staging')
                jid = uuid.uuid4().hex
                target = self.root / 'staging' / f'bridge_{jid}.m4a'
                temp = target.with_suffix('.partial.m4a')
                try:
                    run([self.cfg['ffmpeg'], '-nostdin', '-v', 'error', '-y', '-i', path,
                         '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'aac',
                         '-b:a', '64k', '-movflags', '+faststart', temp], timeout=max(600, int(duration * 2)))
                    if signature(path) != row['sig']:
                        self.observe(path, row['origin'])
                        continue
                    os.rename(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
                if target.stat().st_size > self.cfg.get('max_audio_bytes', 990_000_000):
                    target.unlink()
                    raise ValueError('Prepared audio exceeds upload limit; split the source')
                self.db.execute('INSERT INTO jobs(id,sha256,source_path,mtime,state,stage) VALUES(?,?,?,?,?,?)',
                                (jid, digest, str(path), source_mtime, 'ready', str(target)))
                self.add_source_snapshot(jid, source_fact)
                self.attach(row['key'], jid)
                self.log.info('Prepared job %s', jid)
                return  # Bound work per cycle so status and other sources remain observable.
            except Exception as exc:
                self.db.rollback()
                if target and target.exists() and not self.db.execute(
                        'SELECT 1 FROM jobs WHERE stage=?', (str(target),)).fetchone():
                    target.unlink(missing_ok=True)
                attempts = row['attempts'] + 1
                terminal = isinstance(exc, ValueError) or (isinstance(exc, subprocess.CalledProcessError) and attempts >= 3)
                self.db.execute('UPDATE observed SET kind=?,error=?,attempts=?,next_try=? WHERE key=?',
                    ('failed' if terminal else 'candidate', short_error(exc), attempts,
                     self.clock() + min(900, 30 * 2 ** attempts), row['key']))
                self.db.commit()
                self.log.warning('Preparation failed: %s', short_error(exc))

    def controls(self):
        for path in (self.root / 'controls').glob('*.json'):
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('status') in ('done', 'failed'):
                continue
            try:
                jid = data['job_id']
                row = self.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
                if not row:
                    raise ValueError('Unknown job id')
                if recovery_held(self.db, row):
                    raise ValueError('Historical recovery hold; explicit identity reconciliation required')
                if data['action'] == 'retry':
                    if row['state'] not in ('failed', 'reconciliation_required', 'submitting'):
                        # A replay after a crash may already have advanced this job.
                        if row['state'] not in ('ready', 'retry', 'accepted', 'completed'):
                            raise ValueError('Job is not eligible for retry')
                    elif row['recording_id']:
                        self.db.execute("UPDATE jobs SET state='retry',next_try=0,error=NULL WHERE id=?", (jid,))
                    else:
                        found = self.api.find(Path(row['stage']).name)
                        if found:
                            self.db.execute("UPDATE jobs SET state='accepted',recording_id=?,error=NULL,next_try=0 WHERE id=?", (found['id'], jid))
                        else:
                            self.db.execute("UPDATE jobs SET state='ready',error=NULL,next_try=0 WHERE id=?", (jid,))
                elif data['action'] == 're-export':
                    if not row['recording_id']:
                        raise ValueError('No remote result to export')
                    for pub in self.db.execute('SELECT * FROM publications WHERE job_id=?', (jid,)).fetchall():
                        if not Path(pub['path']).exists():
                            # Keep the original path pinned across an explicit re-export.
                            self.db.execute("UPDATE publications SET state='publishing' WHERE job_id=? AND version=?", (jid, pub['version']))
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_jobs'").fetchone():
                        self.db.execute('UPDATE recovery_jobs SET output_fingerprint=NULL WHERE job_id=?', (jid,))
                    self.db.execute("UPDATE jobs SET state='accepted',next_try=0,last_poll=0,error=NULL WHERE id=?", (jid,))
                else:
                    raise ValueError('Unknown control action')
                self.db.commit()
                data['status'] = 'done'
                data.pop('error', None)
            except ValueError as exc:
                data.update(status='failed', error=short_error(exc))
            except Exception as exc:
                data.update(status='pending', error=short_error(exc))
            atomic_json(path, data)

    def attach(self, key, jid):
        self.db.execute("UPDATE observed SET kind='attached',job_id=?,error=NULL WHERE key=?", (jid, key))
        self.db.execute('UPDATE request_items SET job_id=? WHERE key=? AND job_id IS NULL AND reason IS NULL', (jid, key))
        self.db.commit()

    def reconcile(self, row):
        if recovery_held(self.db, row):
            return
        found = self.api.find(Path(row['stage']).name)
        if found:
            self.db.execute("UPDATE jobs SET state='accepted',recording_id=?,error=NULL,next_try=0 WHERE id=?",
                            (found['id'], row['id']))
        else:
            self.db.execute("UPDATE jobs SET state='reconciliation_required',error=?,next_try=? WHERE id=?",
                            ('Upload result uncertain; not resubmitted', self.clock() + 120, row['id']))
        self.db.commit()

    def submit(self):
        if maintenance_paused(self.root):
            return
        if self.cfg.get('recovery_enabled') and self.cfg.get('asr_health_url'):
            from health import probe_http
            if not probe_http(self.cfg['asr_health_url'])['ready']:
                return
        for row in self.db.execute("SELECT * FROM jobs WHERE state IN ('submitting','reconciliation_required') AND next_try<=?",
                                   (self.clock(),)).fetchall():
            self.reconcile(row)
        # Only one remote active job at once, including jobs submitted outside this bridge.
        pending = any(not recovery_held(self.db, job) for job in self.db.execute("SELECT * FROM jobs WHERE state IN ('accepted','submitting')"))
        if pending:
            return
        row = next((job for job in self.db.execute("SELECT * FROM jobs WHERE state IN ('ready','retry') AND next_try<=? ORDER BY rowid",
                               (self.clock(),)) if not recovery_held(self.db, job)), None)
        if not row:
            return
        sources = self.source_rows(row['id'])
        if not sources:
            # Legacy pending jobs can be upgraded before upload; completed jobs wait for backfill.
            source = self.capture_verified_source(row['source_path'], row['mtime'], row['sha256'])
            self.add_source_snapshot(row['id'], source)
            self.db.commit()
            sources = [source]
        source = sources[0]
        speaker_options = {'min_speakers': 2, 'max_speakers': 2} if is_acr(row['source_path'], self.cfg) else {}
        if any(r['status'] in ('QUEUED', 'PROCESSING', 'SUMMARIZING') for r in self.api.recordings()):
            return
        if row['state'] == 'retry' and row['recording_id']:
            detail = self.api.detail(row['recording_id'])
            if detail['status'] == 'FAILED':
                if maintenance_paused(self.root):
                    return
                self.api.retry(row['recording_id'], **speaker_options)
            self.db.execute("UPDATE jobs SET state='accepted',error=NULL,next_try=0 WHERE id=?", (row['id'],))
            self.db.commit()
            return
        # Login failure before intent is safe to retry; after intent all failures reconcile.
        if isinstance(self.api, SpeakrAPI) and not self.api.csrf:
            self.api.login()
        self.db.execute("UPDATE jobs SET state='submitting',submitted_at=?,attempts=attempts+1 WHERE id=?",
                        (self.clock(), row['id']))
        self.db.commit()
        try:
            if maintenance_paused(self.root):
                return
            response = self.api.upload(row['stage'], Path(row['stage']).name,
                                       source['original_filename'][:200], row['mtime'],
                                       meeting_date=source['selected_date_utc'],
                                       notes=speakr_notes(source), **speaker_options)
            rid = response.get('id') or response.get('recording', {}).get('id')
            if not rid:
                raise ValueError('Upload returned no recording id')
            self.db.execute("UPDATE jobs SET state='accepted',recording_id=?,error=NULL WHERE id=?", (rid, row['id']))
            self.db.commit()
        except Exception as exc:
            self.db.execute('UPDATE jobs SET error=? WHERE id=?', (short_error(exc), row['id']))
            self.db.commit()
            raise

    def publish(self, row, detail, transcript):
        if maintenance_paused(self.root) or recovery_held(self.db, row):
            return False
        job = dict(row)
        sources = self.source_rows(row['id'])
        version = version_hash(detail, transcript, sources)
        pubs = self.db.execute('SELECT * FROM publications WHERE job_id=? ORDER BY created DESC', (row['id'],)).fetchall()
        published = [p for p in pubs if p['state'] == 'published']
        if len(published) > 1:
            raise ValueError('Multiple note versions require single-note migration')
        current = published[0] if published else None
        pending = next((p for p in pubs if p['state'] == 'publishing'), None)
        if pending and current and pending['path'] != current['path']:
            raise ValueError('Publication journal path differs from the pinned note')
        if current and not Path(current['path']).exists():
            self.db.execute("UPDATE jobs SET state='missing_output',error='Published note was removed; use --re-export' WHERE id=?", (row['id'],))
            self.db.commit()
            return False
        if current and current['version'] == version and not pending:
            return True
        vault = Path(self.cfg['vault'])
        # Do not recreate an absent vault root (e.g. a disconnected drive).
        if not Path(self.cfg['vault_root']).is_dir():
            raise OSError('Obsidian vault unavailable')
        if pending and pending['version'] == version and is_our_complete_file(pending['path'], pending['content_hash']):
            owned_note(pending['path'], row['id'])
            self.db.execute('DELETE FROM publications WHERE job_id=? AND version<>?', (row['id'], version))
            self.db.execute("UPDATE publications SET state='published' WHERE job_id=? AND version=?", (row['id'], version))
            self.db.commit()
            return True
        sources = self.publication_sources(row)
        generated = render(job, detail, transcript, version, self.cfg['speakr_url'], provenance=sources)
        target = Path(current['path'] if current else pending['path']) if current or pending else choose_path(vault, job, version, provenance=sources)
        while True:
            replacing = target.exists()
            if replacing and pending and not current:
                try:
                    owned_note(target, row['id'])
                except ValueError:
                    # A first-publication collision may survive a worker restart.
                    target = target.with_name(target.stem[:145] + '-' + uuid.uuid4().hex[:8] + '.md')
                    continue
            if replacing and not (current or pending):
                target = target.with_name(target.stem[:145] + '-' + uuid.uuid4().hex[:8] + '.md')
                continue
            if replacing:
                original, notes = owned_note(target, row['id'])
                text = with_user_notes(generated, notes)
                backup_original(target, self.root, row['id'], original)
            else:
                text = generated
            content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
            self.db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?) ON CONFLICT(job_id,version) '
                'DO UPDATE SET path=excluded.path,content_hash=excluded.content_hash,state=excluded.state',
                (row['id'], version, str(target), content_hash, 'publishing', self.clock()))
            self.db.commit()
            try:
                if maintenance_paused(self.root):
                    return False
                if replacing:
                    write_replace(target, text, expected=original,
                                  backup_dir=self.root / 'backups' / 'note-revisions' / row['id'])
                else:
                    write_new(target, text)
                break
            except FileExistsError:
                if replacing:
                    raise
                target = target.with_name(target.stem[:145] + '-' + uuid.uuid4().hex[:8] + '.md')
                continue
        if not is_our_complete_file(target, content_hash):
            raise OSError('Published note failed content hash check')
        self.db.execute('DELETE FROM publications WHERE job_id=? AND version<>?', (row['id'], version))
        self.db.execute("UPDATE publications SET state='published',path=?,content_hash=? WHERE job_id=? AND version=?",
                        (str(target), content_hash, row['id'], version))
        self.db.commit()
        self.log.info('Updated note for job %s version %s', row['id'], version[:10])
        return True

    def poll(self):
        if self.cfg.get('recovery_enabled') and self.recovery is None:
            from recovery import Recovery
            self.recovery = Recovery(self)
        rows = self.db.execute("SELECT * FROM jobs WHERE recording_id IS NOT NULL AND state IN ('accepted','completed','failed','summary_pending') AND next_try<=?",
                               (self.clock(),)).fetchall()
        for row in rows:
            if recovery_held(self.db, row):
                continue
            if row['state'] in ('completed', 'failed') and self.clock() - row['last_poll'] < self.cfg.get('completed_poll_seconds', 300):
                continue
            try:
                detail = self.api.detail(row['recording_id'])
                if self.recovery:
                    self.recovery.process(row, detail)
                    continue
                if detail['status'] == 'COMPLETED':
                    transcript = self.api.transcript(row['recording_id'])
                    self.publish(row, detail, transcript)
                    fresh = self.db.execute('SELECT state FROM jobs WHERE id=?', (row['id'],)).fetchone()
                    if fresh['state'] not in ('missing_output', 'note_conflict'):
                        self.db.execute("UPDATE jobs SET state='completed',error=NULL,next_try=0,last_poll=? WHERE id=?", (self.clock(), row['id']))
                        Path(row['stage']).unlink(missing_ok=True)
                elif detail['status'] == 'FAILED':
                    self.db.execute("UPDATE jobs SET state='failed',error='Speakr processing failed; retry manually',last_poll=? WHERE id=?",
                                    (self.clock(), row['id']))
                elif row['state'] == 'failed' and detail['status'] in ('QUEUED', 'PROCESSING', 'SUMMARIZING'):
                    self.db.execute("UPDATE jobs SET state='accepted',error=NULL,next_try=0 WHERE id=?", (row['id'],))
                self.db.commit()
            except Exception as exc:
                state = ('note_conflict' if isinstance(exc, NoteConflict) else
                         'remote_missing' if isinstance(exc, urllib.error.HTTPError) and exc.code == 404 else row['state'])
                self.db.execute('UPDATE jobs SET state=?,error=?,next_try=? WHERE id=?',
                                (state, short_error(exc), self.clock() + 60, row['id']))
                self.db.commit()
                self.log.warning('Poll job %s: %s', row['id'], short_error(exc))

    def request_reports(self):
        for req in self.db.execute('SELECT * FROM requests').fetchall():
            result = []
            for item in self.db.execute('SELECT * FROM request_items WHERE request_id=?', (req['id'],)).fetchall():
                obs = self.db.execute('SELECT * FROM observed WHERE key=?', (item['key'],)).fetchone()
                jid = item['job_id'] or (obs['job_id'] if obs else None)
                if jid and not item['job_id']:
                    self.db.execute('UPDATE request_items SET job_id=? WHERE request_id=? AND key=?', (jid, req['id'], item['key']))
                job = self.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone() if jid else None
                pub = self.db.execute("SELECT path FROM publications WHERE job_id=? AND state='published' ORDER BY created DESC LIMIT 1", (jid,)).fetchone() if jid else None
                result.append({'path': item['path'], 'status': 'skipped' if item['reason'] else (job['state'] if job else obs['kind'] if obs else 'pending'),
                               'reason': item['reason'] or (job['error'] if job else obs['error'] if obs else None),
                               'job_id': jid, 'recording_id': job['recording_id'] if job else None,
                               'note': pub['path'] if pub else None})
            data = {'request_id': req['id'], 'state': req['state'], 'updated': self.clock(), 'items': result}
            atomic_json(self.root / 'reports' / (req['id'] + '.json'), data)
            title = 'Запрос на расшифровку ' + req['id'][:8]
            rows = []
            names = {'completed': 'Готово', 'accepted': 'В Speakr', 'ready': 'В очереди',
                     'candidate': 'Ожидает готовности файла', 'failed': 'Ошибка', 'skipped': 'Пропущено',
                     'attached': 'В очереди', 'ignored': 'Нет аудиодорожки', 'missing_output': 'Заметка удалена',
                     'note_conflict': 'Правка заметки требует сверки',
                     'reconciliation_required': 'Проверяется приём в Speakr', 'remote_missing': 'Запись удалена в Speakr'}
            for entry in result:
                link = f'<a href="{html.escape(Path(entry["note"]).as_uri(), quote=True)}">Заметка</a>' if entry['note'] else ''
                rows.append('<tr><td>' + html.escape(entry['path']) + '</td><td>' + html.escape(names.get(entry['status'], entry['status'])) +
                            '</td><td>' + html.escape(entry['reason'] or '') + '</td><td>' + link + '</td></tr>')
            document = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>' + title + '</title>' + \
                '<style>body{font:16px Segoe UI,sans-serif;max-width:1200px;margin:36px auto;padding:0 20px}td,th{padding:12px;border-bottom:1px solid #ccc;text-align:left;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}</style>' + \
                '<h1>' + title + '</h1><p>Обновите страницу, чтобы увидеть текущие статусы. После окончания файлы появятся в Obsidian.</p>' + \
                '<table><tr><th>Источник</th><th>Статус</th><th>Причина</th><th>Результат</th></tr>' + ''.join(rows) + '</table></html>'
            dest = self.root / 'reports' / (req['id'] + '.html')
            tmp = dest.with_suffix('.tmp')
            tmp.write_text(document, encoding='utf-8')
            os.replace(tmp, dest)
        self.db.commit()

    def status(self):
        data = {'updated': self.clock(), 'pid': os.getpid(), 'watch_enabled': self.meta('watch_enabled') == 'true',
                'sources': [dict(r) for r in self.db.execute('SELECT * FROM sources')],
                'observed': dict(Counter(r['kind'] for r in self.db.execute('SELECT kind FROM observed'))),
                'jobs': [dict(r) for r in self.db.execute('SELECT id,source_path,state,recording_id,error FROM jobs')],
                'last_error': self.error, 'maintenance': maintenance_paused(self.root)}
        if self.recovery:
            data.update(self.recovery.status())
        return data

    def ensure_services(self):
        if self.cfg.get('recovery_enabled'):
            return  # Watchdog is the single owner of runtime start/restart.
        if not self.cfg.get('auto_start_docker', False) or self.clock() - self.last_services < 120:
            return
        self.last_services = self.clock()
        try:
            run([self.cfg['docker'], 'info', '--format', '{{.ServerVersion}}'], timeout=15)
        except Exception:
            startup = None
            if os.name == 'nt':
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
            subprocess.Popen([self.cfg['docker_desktop']], creationflags=CREATE_NO_WINDOW, startupinfo=startup,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        try:
            # Compose output is kept out of logs; only its exit code is relevant.
            run([self.cfg['docker'], 'compose', '-f', self.cfg['compose_file'], 'up', '-d', 'app', 'whisperx-asr'], timeout=90)
        except Exception as exc:
            self.error = short_error(exc)

    def cycle(self, process=True):
        self.error = None
        if maintenance_paused(self.root):
            atomic_json(self.root / 'status.json', self.status())
            return
        self.scan_sources()
        self.ingest_requests()
        if process:
            self.ensure_services()
            self.controls()
            self.prepare()
            try:
                if self.heartbeat:
                    self.heartbeat.begin('poll-and-submit', 7200)
                self.poll()
                self.submit()
            except Exception as exc:
                self.error = short_error(exc)
                self.log.warning('Service: %s', self.error)
        self.request_reports()
        atomic_json(self.root / 'status.json', self.status())


def task_toggle(enabled, cfg):
    verb = 'Enable-ScheduledTask' if enabled else 'Disable-ScheduledTask'
    name = cfg['bridge_task'].replace("'", "''")
    return run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                f"{verb} -TaskName '{name}' -ErrorAction Stop | Out-Null"], 30)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config')
    p.add_argument('--loop', action='store_true')
    p.add_argument('--once', action='store_true')
    p.add_argument('--baseline', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--status', action='store_true')
    p.add_argument('--pause-watch', action='store_true')
    p.add_argument('--resume-watch', action='store_true')
    p.add_argument('--stop', action='store_true')
    p.add_argument('--retry')
    p.add_argument('--re-export')
    p.add_argument('--preview-provenance-backfill', action='store_true')
    p.add_argument('--apply-provenance-backfill', action='store_true')
    args = p.parse_args()
    cfg = config(args.config)
    root = Path(cfg.get('state_dir', ROOT))
    if args.preview_provenance_backfill:
        print(json.dumps(preview(cfg, root, SpeakrAPI(cfg)), ensure_ascii=False, indent=2))
        return
    if args.dry_run:
        print(json.dumps({r: dict(Counter(reason or 'media' for _, reason in media_paths(r)))
                          for r in cfg['sources']}, ensure_ascii=False, indent=2))
        return
    if args.status:
        print((root / 'status.json').read_text(encoding='utf-8') if (root / 'status.json').exists() else 'Not started')
        return
    if args.stop:
        atomic_json(root / 'stop.json', {'at': time.time()})
        return
    if args.retry or args.re_export:
        control_id = enqueue_control('retry' if args.retry else 're-export', args.retry or args.re_export, root)
        print(json.dumps({'control_id': control_id, 'status': 'queued',
                          'report': str(root / 'controls' / (control_id + '.json'))}))
        start_worker(cfg, ROOT)
        return
    bridge = Bridge(cfg, root)
    if args.apply_provenance_backfill:
        try:
            with WorkerLock(root):
                print(json.dumps(bridge.apply_provenance_backfill(), ensure_ascii=False, indent=2))
        finally:
            bridge.close()
        return
    if args.pause_watch or args.resume_watch:
        bridge.watch(args.resume_watch)
        task_toggle(args.resume_watch, cfg)
        bridge.close()
        return
    started = time.time()
    try:
        with WorkerLock(root):
            if cfg.get('recovery_enabled'):
                from health import Heartbeat
                bridge.heartbeat = Heartbeat(root)
                bridge.heartbeat.start()
            while True:
                stop = root / 'stop.json'
                if stop.exists() and json.loads(stop.read_text(encoding='utf-8-sig'))['at'] >= started:
                    break
                try:
                    if bridge.heartbeat:
                        bridge.heartbeat.begin('scan-and-media', 86400)
                    bridge.cycle(process=not args.baseline)
                except Exception as exc:
                    bridge.error = short_error(exc)
                    bridge.log.error('Cycle failed: %s', bridge.error)
                    atomic_json(root / 'status.json', bridge.status())
                finally:
                    if bridge.heartbeat:
                        bridge.heartbeat.finish()
                if not args.loop:
                    break
                time.sleep(cfg.get('scan_seconds', 30))
    except RuntimeError as exc:
        if str(exc) != 'Bridge is already running':
            raise
    finally:
        if bridge.heartbeat:
            bridge.heartbeat.stop()
        bridge.close()


if __name__ == '__main__':
    main()
