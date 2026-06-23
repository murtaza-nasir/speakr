#!/usr/bin/env python3
"""Normalize legacy absolute local recording paths into local:// locators."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.app import app  # noqa: E402
from src.database import db  # noqa: E402
from src.models import Recording  # noqa: E402
from src.services.storage import get_storage_service  # noqa: E402


def _is_absolute_path(value: str) -> bool:
    if not value:
        return False
    if len(value) >= 3 and value[1] == ':' and value[2] in ('\\', '/'):
        return True
    return os.path.isabs(value)


def parse_args():
    p = argparse.ArgumentParser(description='Normalize legacy absolute recording paths to local:// locators')
    p.add_argument('--dry-run', action='store_true', help='Preview changes without writing to DB')
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--recording-id', type=int, default=None)
    p.add_argument('--only-user', type=int, default=None)
    p.add_argument('--allow-missing-file', action='store_true')
    p.add_argument('--report-jsonl', type=str, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    storage = get_storage_service()

    stats = {
        'scanned': 0,
        'normalized': 0,
        'skipped_non_legacy': 0,
        'skipped_missing_file': 0,
        'errors': 0,
    }

    report_fp = open(args.report_jsonl, 'a', encoding='utf-8') if args.report_jsonl else None

    with app.app_context():
        query = Recording.query
        if args.recording_id:
            query = query.filter(Recording.id == args.recording_id)
        if args.only_user:
            query = query.filter(Recording.user_id == args.only_user)
        query = query.order_by(Recording.id.asc())
        if args.limit:
            query = query.limit(args.limit)

        for recording in query.all():
            stats['scanned'] += 1
            old_value = recording.audio_path or ''

            if not old_value or not _is_absolute_path(old_value):
                stats['skipped_non_legacy'] += 1
                continue

            try:
                if (not args.allow_missing_file) and (not os.path.exists(old_value)):
                    stats['skipped_missing_file'] += 1
                    _write_report(report_fp, recording.id, 'skip_missing_file', old_value, None)
                    continue

                new_locator = storage.build_local_locator_from_path(old_value)
                if old_value == new_locator:
                    stats['skipped_non_legacy'] += 1
                    continue

                _write_report(report_fp, recording.id, 'normalize', old_value, new_locator)
                if not args.dry_run:
                    recording.audio_path = new_locator
                    db.session.add(recording)
                    db.session.commit()
                stats['normalized'] += 1
            except Exception as exc:
                db.session.rollback()
                stats['errors'] += 1
                _write_report(report_fp, recording.id, 'error', old_value, None, str(exc))

    if report_fp:
        report_fp.close()

    print(json.dumps({'timestamp': datetime.utcnow().isoformat(), **stats}, ensure_ascii=False, indent=2))
    return 0 if stats['errors'] == 0 else 1


def _write_report(fp, recording_id, action, old_value, new_value=None, error=None):
    if not fp:
        return
    row = {
        'ts': datetime.utcnow().isoformat(),
        'recording_id': recording_id,
        'action': action,
        'old_audio_path': old_value,
        'new_audio_path': new_value,
        'error': error,
    }
    fp.write(json.dumps(row, ensure_ascii=False) + '\n')
    fp.flush()


if __name__ == '__main__':
    raise SystemExit(main())
