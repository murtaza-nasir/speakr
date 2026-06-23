#!/usr/bin/env python3
"""Migrate recordings from local:// locators to s3:// locators."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.app import app  # noqa: E402
from src.database import db  # noqa: E402
from src.models import Recording  # noqa: E402
from src.services.storage import get_storage_service  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description='Migrate recording audio from local:// to s3://')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--recording-id', type=int, default=None)
    p.add_argument('--only-user', type=int, default=None)
    p.add_argument('--verify-size', action='store_true', default=True)
    p.add_argument('--delete-local-after-success', action='store_true')
    p.add_argument('--report-jsonl', type=str, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    storage = get_storage_service()

    if storage.default_backend_kind() != 's3':
        print('ERROR: FILE_STORAGE_BACKEND must be s3 to run this migration script', file=sys.stderr)
        return 2

    stats = {
        'scanned': 0,
        'migrated': 0,
        'skipped_already_s3': 0,
        'skipped_not_local': 0,
        'skipped_missing_local': 0,
        'skipped_in_progress': 0,
        'errors': 0,
    }
    report_fp = open(args.report_jsonl, 'a', encoding='utf-8') if args.report_jsonl else None

    try:
        with app.app_context():
            query = Recording.query.filter(Recording.audio_path.isnot(None))
            if args.recording_id:
                query = query.filter(Recording.id == args.recording_id)
            if args.only_user:
                query = query.filter(Recording.user_id == args.only_user)
            query = query.order_by(Recording.id.asc())
            if args.limit:
                query = query.limit(args.limit)

            for recording in query.all():
                stats['scanned'] += 1
                old_locator = (recording.audio_path or '').strip()

                if old_locator.startswith('s3://'):
                    stats['skipped_already_s3'] += 1
                    _report(report_fp, recording.id, 'skip_already_s3', old_locator)
                    continue

                if not old_locator.startswith('local://'):
                    stats['skipped_not_local'] += 1
                    _report(report_fp, recording.id, 'skip_not_local_locator', old_locator)
                    continue

                if recording.status in ('PROCESSING', 'QUEUED'):
                    stats['skipped_in_progress'] += 1
                    _report(report_fp, recording.id, 'skip_in_progress', old_locator)
                    continue

                try:
                    local_path = storage.resolve_local_filesystem_path(old_locator)
                    if not os.path.exists(local_path):
                        stats['skipped_missing_local'] += 1
                        _report(report_fp, recording.id, 'skip_missing_local_file', old_locator, error=local_path)
                        continue

                    key = storage.build_recording_key(recording.original_filename or os.path.basename(local_path), recording.id)

                    if args.dry_run:
                        preview_locator = storage.build_default_locator(key)
                        _report(report_fp, recording.id, 'dry_run', old_locator, preview_locator)
                        continue

                    old_size = os.path.getsize(local_path) if args.verify_size else None
                    stored = storage.upload_local_file(local_path, key, content_type=recording.mime_type, delete_source=False)

                    if args.verify_size and old_size is not None and stored.size is not None and int(stored.size) != int(old_size):
                        raise ValueError(f'size mismatch local={old_size} s3={stored.size}')

                    prev_locator = recording.audio_path
                    recording.audio_path = stored.locator
                    db.session.add(recording)
                    db.session.commit()

                    if args.delete_local_after_success:
                        try:
                            get_storage_service().delete(prev_locator, missing_ok=True)
                        except Exception as cleanup_exc:
                            _report(report_fp, recording.id, 'warning_delete_local_failed', prev_locator, stored.locator, str(cleanup_exc))

                    stats['migrated'] += 1
                    _report(report_fp, recording.id, 'migrated', prev_locator, stored.locator)
                except Exception as exc:
                    db.session.rollback()
                    stats['errors'] += 1
                    _report(report_fp, recording.id, 'error', old_locator, error=str(exc))
    finally:
        if report_fp:
            report_fp.close()

    print(json.dumps({'timestamp': datetime.utcnow().isoformat(), **stats}, ensure_ascii=False, indent=2))
    return 0 if stats['errors'] == 0 else 1


def _report(fp, recording_id, action, old_locator, new_locator=None, error=None):
    if not fp:
        return
    row = {
        'ts': datetime.utcnow().isoformat(),
        'recording_id': recording_id,
        'action': action,
        'old_audio_path': old_locator,
        'new_audio_path': new_locator,
        'error': error,
    }
    fp.write(json.dumps(row, ensure_ascii=False) + '\n')
    fp.flush()


if __name__ == '__main__':
    raise SystemExit(main())
