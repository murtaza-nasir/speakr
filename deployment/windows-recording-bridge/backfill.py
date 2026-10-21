"""Read-only provenance backfill planning; application stays in Bridge under worker lock."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from common import path_key
from publication import choose_path, version_hash
from provenance import verified_snapshot


def candidates(db, cfg, capture=None, api=None):
    capture = capture or (lambda path, mtime, digest: verified_snapshot(path, mtime, digest, cfg))
    result = []
    has_provenance = bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_source_provenance'").fetchone())
    jobs = db.execute('SELECT * FROM jobs ORDER BY rowid').fetchall()
    for row in jobs:
        job = dict(row)
        existing = db.execute('SELECT 1 FROM job_source_provenance WHERE job_id=? LIMIT 1',
                              (job['id'],)).fetchone() if has_provenance else None
        if existing:
            continue
        paths = [(job['source_path'], job['mtime'])]
        seen = {path_key(job['source_path'])}
        for observed in db.execute('SELECT path FROM observed WHERE job_id=? ORDER BY rowid', (job['id'],)):
            key = path_key(observed['path'])
            if key not in seen:
                seen.add(key)
                paths.append((observed['path'], None))
        sources = [capture(path, mtime, job['sha256']) for path, mtime in paths]
        publication = db.execute("SELECT path,content_hash FROM publications WHERE job_id=? AND state='published' ORDER BY created DESC LIMIT 1", (job['id'],)).fetchone()
        previous = publication['path'] if publication else None
        edited = None
        if publication:
            note = Path(publication['path'])
            edited = note.is_file() and hashlib.sha256(note.read_bytes()).hexdigest() != publication['content_hash']
        proposed = None
        proposed_exact = False
        proposed_error = None
        if job['state'] == 'completed':
            version = 'preview'
            if api is not None:
                try:
                    version = version_hash(api.detail(job['recording_id']),
                                           api.transcript(job['recording_id']), sources)
                    proposed_exact = True
                except Exception as exc:
                    proposed_error = type(exc).__name__
            proposed = Path(previous) if previous else choose_path(cfg['vault'], job, version, provenance=sources)
        result.append({'job_id': job['id'], 'recording_id': job['recording_id'], 'state': job['state'],
                       'sources': sources, 'previous_note': previous, 'previous_note_edited': edited,
                       'proposed_note_path': str(proposed) if proposed else None,
                       'proposed_note_path_exact': proposed_exact,
                       'proposed_note_error': proposed_error,
                       'suggested_speakr_title': sources[0]['original_filename'][:200],
                       'suggested_speakr_date': sources[0]['selected_date_utc']})
    return result


def preview(cfg, root, api=None):
    uri = (Path(root) / 'state.sqlite3').resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = candidates(db, cfg, api=api)
        return {'count': len(rows), 'jobs': rows, 'read_only': True,
                'speakr_metadata_updated': False}
    finally:
        db.close()
